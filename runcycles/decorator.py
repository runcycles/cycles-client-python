"""The @cycles decorator for budget-guarded function calls."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.lifecycle import AsyncCyclesLifecycle, CyclesLifecycle, DecoratorConfig
from runcycles.models import Unit
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine

F = TypeVar("F", bound=Callable[..., Any])

# Module-level default client
_default_client: CyclesClient | AsyncCyclesClient | None = None
_default_config: CyclesConfig | None = None


def set_default_client(client: CyclesClient | AsyncCyclesClient) -> None:
    """Set the module-level default client used by @cycles decorators without an explicit client."""
    global _default_client
    _default_client = client


def set_default_config(config: CyclesConfig) -> None:
    """Set the module-level default config. A client will be created lazily when needed."""
    global _default_config
    _default_config = config


def _get_effective_client(
    explicit_client: CyclesClient | AsyncCyclesClient | None, is_async: bool,
) -> CyclesClient | AsyncCyclesClient:
    global _default_client
    if explicit_client is not None:
        return explicit_client
    if _default_client is not None:
        return _default_client
    if _default_config is not None:
        if is_async:
            _default_client = AsyncCyclesClient(_default_config)
        else:
            _default_client = CyclesClient(_default_config)
        return _default_client
    raise ValueError(
        "No Cycles client available. Either pass client= to @cycles, "
        "call runcycles.set_default_client(), or call runcycles.set_default_config()."
    )


def cycles(
    estimate: int | Callable[..., int],
    *,
    actual: int | Callable[..., int] | None = None,
    action_kind: str | Callable[..., str | None] | None = None,
    action_name: str | Callable[..., str | None] | None = None,
    action_tags: list[str] | Callable[..., list[str] | None] | None = None,
    unit: Unit | str = Unit.USD_MICROCENTS,
    ttl_ms: int = 60_000,
    grace_period_ms: int | None = None,
    overage_policy: str = "ALLOW_IF_AVAILABLE",
    dry_run: bool = False,
    tenant: str | Callable[..., str | None] | None = None,
    workspace: str | Callable[..., str | None] | None = None,
    app: str | Callable[..., str | None] | None = None,
    workflow: str | Callable[..., str | None] | None = None,
    agent: str | Callable[..., str | None] | None = None,
    toolset: str | Callable[..., str | None] | None = None,
    dimensions: dict[str, str] | Callable[..., dict[str, str] | None] | None = None,
    client: CyclesClient | AsyncCyclesClient | None = None,
    use_estimate_if_actual_not_provided: bool = True,
) -> Callable[[F], F]:
    """Decorator that wraps a function with the Cycles reserve/execute/commit lifecycle.

    Subject and action fields accept either a constant or a callable. When given a
    callable, it is invoked with the decorated function's ``*args, **kwargs`` at
    reservation time. Subject callables returning ``None`` fall through to the
    client-config default; ``action_kind`` / ``action_name`` returning ``None`` fall
    through to ``"unknown"``; ``action_tags`` / ``dimensions`` returning ``None`` are
    omitted.

    Args:
        estimate: Estimated cost. Either an int constant or a callable that receives
            the decorated function's ``*args, **kwargs`` and returns an int.
        actual: Actual cost. Either an int constant or a callable that receives
            the function's return value and returns an int. Defaults to the estimate.
        action_kind: Action category (e.g. "llm.completion"). Constant or callable
            receiving ``*args, **kwargs``.
        action_name: Action identifier (e.g. "gpt-4"). Constant or callable
            receiving ``*args, **kwargs``.
        action_tags: Optional tags for filtering/reporting. Constant list or callable
            receiving ``*args, **kwargs`` returning a list.
        unit: Cost unit. Default: USD_MICROCENTS.
        ttl_ms: Reservation TTL in milliseconds. Default: 60000.
        grace_period_ms: Grace period after TTL expiry in milliseconds.
        overage_policy: REJECT, ALLOW_IF_AVAILABLE (default), or ALLOW_WITH_OVERDRAFT.
        dry_run: If True, evaluate without persisting (method won't execute).
        tenant: Subject tenant override. Constant or callable receiving ``*args, **kwargs``.
        workspace: Subject workspace override. Constant or callable receiving ``*args, **kwargs``.
        app: Subject app override. Constant or callable receiving ``*args, **kwargs``.
        workflow: Subject workflow override. Constant or callable receiving ``*args, **kwargs``.
        agent: Subject agent override. Constant or callable receiving ``*args, **kwargs``.
        toolset: Subject toolset override. Constant or callable receiving ``*args, **kwargs``.
        dimensions: Custom dimensions for the subject. Constant dict or callable
            receiving ``*args, **kwargs`` returning a dict.
        client: Explicit Cycles client to use. Falls back to module-level default.
        use_estimate_if_actual_not_provided: If True and actual is None, use estimate as actual.

    Returns:
        A decorator that wraps the function with budget enforcement.

    Example::

        @cycles(estimate=1000, client=my_client)
        def call_llm(prompt: str) -> str:
            return openai.complete(prompt)

        @cycles(
            estimate=lambda prompt, tokens: tokens * 10,
            actual=lambda result: len(result) * 5,
            action_kind="llm.completion",
            client=my_client,
        )
        def call_llm(prompt: str, tokens: int) -> str:
            return openai.complete(prompt, max_tokens=tokens)
    """
    unit_str = unit.value if isinstance(unit, Unit) else str(unit)

    cfg = DecoratorConfig(
        estimate=estimate,
        actual=actual,
        action_kind=action_kind,
        action_name=action_name,
        action_tags=action_tags,
        unit=unit_str,
        ttl_ms=ttl_ms,
        grace_period_ms=grace_period_ms,
        overage_policy=overage_policy,
        dry_run=dry_run,
        tenant=tenant,
        workspace=workspace,
        app=app,
        workflow=workflow,
        agent=agent,
        toolset=toolset,
        dimensions=dimensions,
        use_estimate_if_actual_not_provided=use_estimate_if_actual_not_provided,
    )

    def _build_default_subject(
        effective_client: CyclesClient | AsyncCyclesClient,
    ) -> dict[str, str | None]:
        config = effective_client._config
        return {
            "tenant": config.tenant,
            "workspace": config.workspace,
            "app": config.app,
            "workflow": config.workflow,
            "agent": config.agent,
            "toolset": config.toolset,
        }

    def decorator(fn: F) -> F:
        is_async = inspect.iscoroutinefunction(fn)

        if is_async:
            _cached_async: list[AsyncCyclesLifecycle | None] = [None]

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                lifecycle = _cached_async[0]
                if lifecycle is None:
                    effective_client = _get_effective_client(client, is_async=True)
                    if not isinstance(effective_client, AsyncCyclesClient):
                        raise TypeError("Async function requires an AsyncCyclesClient")
                    subject = _build_default_subject(effective_client)
                    engine = AsyncCommitRetryEngine(effective_client._config)
                    lifecycle = AsyncCyclesLifecycle(effective_client, engine, subject)
                    _cached_async[0] = lifecycle
                return await lifecycle.execute(fn, args, kwargs, cfg)

            return async_wrapper  # type: ignore[return-value]
        else:
            _cached_sync: list[CyclesLifecycle | None] = [None]

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                lifecycle = _cached_sync[0]
                if lifecycle is None:
                    effective_client = _get_effective_client(client, is_async=False)
                    if not isinstance(effective_client, CyclesClient):
                        raise TypeError("Sync function requires a CyclesClient")
                    subject = _build_default_subject(effective_client)
                    engine = CommitRetryEngine(effective_client._config)
                    lifecycle = CyclesLifecycle(effective_client, engine, subject)
                    _cached_sync[0] = lifecycle
                return lifecycle.execute(fn, args, kwargs, cfg)

            return sync_wrapper  # type: ignore[return-value]

    return decorator
