"""The @cycles decorator for budget-guarded function calls."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, TypeVar

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


def _get_effective_client(explicit_client: CyclesClient | AsyncCyclesClient | None, is_async: bool) -> CyclesClient | AsyncCyclesClient:
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
    action_kind: str | None = None,
    action_name: str | None = None,
    action_tags: list[str] | None = None,
    unit: Unit | str = Unit.USD_MICROCENTS,
    ttl_ms: int = 60_000,
    grace_period_ms: int | None = None,
    overage_policy: str = "REJECT",
    dry_run: bool = False,
    tenant: str | None = None,
    workspace: str | None = None,
    app: str | None = None,
    workflow: str | None = None,
    agent: str | None = None,
    toolset: str | None = None,
    dimensions: dict[str, str] | None = None,
    client: CyclesClient | AsyncCyclesClient | None = None,
    use_estimate_if_actual_not_provided: bool = True,
) -> Callable[[F], F]:
    """Decorator that wraps a function with the Cycles reserve/execute/commit lifecycle.

    Args:
        estimate: Estimated cost. Either an int constant or a callable that receives
            the decorated function's ``*args, **kwargs`` and returns an int.
        actual: Actual cost. Either an int constant or a callable that receives
            the function's return value and returns an int. Defaults to the estimate.
        action_kind: Action category (e.g. "llm.completion").
        action_name: Action identifier (e.g. "gpt-4").
        action_tags: Optional tags for filtering/reporting.
        unit: Cost unit. Default: USD_MICROCENTS.
        ttl_ms: Reservation TTL in milliseconds. Default: 60000.
        grace_period_ms: Grace period after TTL expiry in milliseconds.
        overage_policy: REJECT, ALLOW_IF_AVAILABLE, or ALLOW_WITH_OVERDRAFT.
        dry_run: If True, evaluate without persisting (method won't execute).
        tenant: Subject tenant override.
        workspace: Subject workspace override.
        app: Subject app override.
        workflow: Subject workflow override.
        agent: Subject agent override.
        toolset: Subject toolset override.
        dimensions: Custom dimensions for the subject.
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

    def decorator(fn: F) -> F:
        is_async = inspect.iscoroutinefunction(fn)

        if is_async:

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                effective_client = _get_effective_client(client, is_async=True)
                if not isinstance(effective_client, AsyncCyclesClient):
                    raise TypeError("Async function requires an AsyncCyclesClient")

                config = effective_client._config
                default_subject = {
                    "tenant": config.tenant,
                    "workspace": config.workspace,
                    "app": config.app,
                    "workflow": config.workflow,
                    "agent": config.agent,
                    "toolset": config.toolset,
                }
                retry_engine = AsyncCommitRetryEngine(config)
                lifecycle = AsyncCyclesLifecycle(effective_client, retry_engine, default_subject)
                return await lifecycle.execute(fn, args, kwargs, cfg)

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                effective_client = _get_effective_client(client, is_async=False)
                if not isinstance(effective_client, CyclesClient):
                    raise TypeError("Sync function requires a CyclesClient")

                config = effective_client._config
                default_subject = {
                    "tenant": config.tenant,
                    "workspace": config.workspace,
                    "app": config.app,
                    "workflow": config.workflow,
                    "agent": config.agent,
                    "toolset": config.toolset,
                }
                retry_engine = CommitRetryEngine(config)
                lifecycle = CyclesLifecycle(effective_client, retry_engine, default_subject)
                return lifecycle.execute(fn, args, kwargs, cfg)

            return sync_wrapper  # type: ignore[return-value]

    return decorator
