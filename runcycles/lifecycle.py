"""Lifecycle orchestration: reserve → execute → commit/release."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from runcycles._validation import (
    validate_extend_by_ms,
    validate_grace_period_ms,
    validate_non_negative,
    validate_subject,
    validate_ttl_ms,
)
from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.context import CyclesContext, _clear_context, _set_context
from runcycles.exceptions import (
    BudgetExceededError,
    CyclesProtocolError,
    DebtOutstandingError,
    OverdraftLimitExceededError,
    ReservationExpiredError,
    ReservationFinalizedError,
    TenantClosedError,
)
from runcycles.models import (
    CyclesMetrics,
    Decision,
    DryRunResult,
    ReservationCreateResponse,
    ReservationExtendResponse,
    Subject,
)
from runcycles.response import CyclesResponse
from runcycles.retry import (
    AsyncCommitRetryEngine,
    CommitRetryEngine,
    _extract_error_code,
    _is_recognized_rejection,
    _is_schema_valid_commit_success,
)

logger = logging.getLogger(__name__)


@dataclass
class DecoratorConfig:
    """Configuration extracted from the @cycles decorator parameters."""

    estimate: int | Callable[..., int]
    actual: int | Callable[..., int] | None = None
    action_kind: str | Callable[..., str | None] | None = None
    action_name: str | Callable[..., str | None] | None = None
    action_tags: list[str] | Callable[..., list[str] | None] | None = None
    unit: str = "USD_MICROCENTS"
    ttl_ms: int = 60_000
    grace_period_ms: int | None = None
    overage_policy: str = "ALLOW_IF_AVAILABLE"
    dry_run: bool = False
    tenant: str | Callable[..., str | None] | None = None
    workspace: str | Callable[..., str | None] | None = None
    app: str | Callable[..., str | None] | None = None
    workflow: str | Callable[..., str | None] | None = None
    agent: str | Callable[..., str | None] | None = None
    toolset: str | Callable[..., str | None] | None = None
    dimensions: dict[str, str] | Callable[..., dict[str, str] | None] | None = None
    use_estimate_if_actual_not_provided: bool = True


def _evaluate_amount(expr: int | Callable[..., int], args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    """Evaluate an estimate/actual expression, which may be a constant or a callable."""
    if callable(expr):
        return expr(*args, **kwargs)
    return int(expr)


def _resolve_value(val: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Resolve a decorator value: invoke if callable, else return as-is."""
    if callable(val):
        return val(*args, **kwargs)
    return val


def _evaluate_actual(
    expr: int | Callable[..., int] | None,
    result: Any,
    estimate: int,
    use_estimate_fallback: bool,
) -> int:
    """Evaluate the actual amount from the return value."""
    if expr is not None:
        if callable(expr):
            return expr(result)
        return int(expr)
    if use_estimate_fallback:
        return estimate
    raise ValueError("actual expression is required when use_estimate_if_actual_not_provided is False")


def _build_reservation_body(
    cfg: DecoratorConfig,
    estimate: int,
    default_subject_fields: dict[str, str | None],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build the reservation create request body."""
    validate_non_negative(estimate, "estimate")
    validate_ttl_ms(cfg.ttl_ms)

    subject: dict[str, Any] = {}
    for field_name in ("tenant", "workspace", "app", "workflow", "agent", "toolset"):
        val = _resolve_value(getattr(cfg, field_name, None), args, kwargs)
        if not val:
            val = default_subject_fields.get(field_name)
        if val:
            subject[field_name] = val
    dims = _resolve_value(cfg.dimensions, args, kwargs)
    if dims:
        subject["dimensions"] = dims

    subject_model = Subject(**subject)
    validate_subject(subject_model)

    kind = _resolve_value(cfg.action_kind, args, kwargs)
    name = _resolve_value(cfg.action_name, args, kwargs)
    tags = _resolve_value(cfg.action_tags, args, kwargs)
    action: dict[str, Any] = {
        "kind": kind or "unknown",
        "name": name or "unknown",
    }
    if tags:
        action["tags"] = tags

    body: dict[str, Any] = {
        "idempotency_key": str(uuid.uuid4()),
        "subject": subject,
        "action": action,
        "estimate": {"unit": cfg.unit, "amount": estimate},
        "ttl_ms": cfg.ttl_ms,
        "overage_policy": cfg.overage_policy,
    }

    validate_grace_period_ms(cfg.grace_period_ms)
    if cfg.grace_period_ms is not None:
        body["grace_period_ms"] = cfg.grace_period_ms
    if cfg.dry_run:
        body["dry_run"] = True

    return body


def _build_commit_body(
    actual: int,
    unit: str,
    metrics: CyclesMetrics | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": str(uuid.uuid4()),
        "actual": {"unit": unit, "amount": actual},
    }
    if metrics and not metrics.is_empty():
        body["metrics"] = metrics.model_dump(exclude_none=True)
    if metadata:
        body["metadata"] = metadata
    return body


def _build_event_fallback_body(
    reservation_id: str,
    subject: dict[str, Any],
    action: dict[str, Any],
    commit_body: dict[str, Any],
) -> dict[str, Any]:
    """Build a POST /v1/events body that records the spend of a commit whose
    reservation expired before the commit landed (the server has already
    returned the reserved budget to the pool at that point).

    Reuses the commit's idempotency key — the event idempotency namespace is
    separate, so replays across process restarts stay exactly-once. Omits
    overage_policy: the spec default ALLOW_IF_AVAILABLE never rejects, which
    is the right bias when the spend has already happened.
    """
    metadata = dict(commit_body.get("metadata") or {})
    metadata["recovered_reservation_id"] = reservation_id
    metadata["recovery_reason"] = "commit_after_reservation_expired"
    body: dict[str, Any] = {
        "idempotency_key": commit_body["idempotency_key"],
        "subject": subject,
        "action": action,
        "actual": commit_body["actual"],
        "metadata": metadata,
    }
    if "metrics" in commit_body:
        body["metrics"] = commit_body["metrics"]
    return body


def _build_release_body(reason: str) -> dict[str, Any]:
    return {"idempotency_key": str(uuid.uuid4()), "reason": reason}


def _now_mono_ms() -> float:
    """Monotonic milliseconds — the heartbeat's only clock (test seam)."""
    return time.monotonic() * 1000.0


# Extend failures that can never succeed again — the heartbeat stops on them.
_PERMANENT_EXTEND_CODES = frozenset(
    {
        "RESERVATION_EXPIRED",
        "RESERVATION_FINALIZED",
        "MAX_EXTENSIONS_EXCEEDED",
        "TENANT_CLOSED",  # closure is irreversible per cascade semantics
        "NOT_FOUND",  # a purged reservation never comes back
    }
)
# Skip an extension while lead_min is at least this multiple of the last
# measured grant; below it, extend. Attempts then carry enough margin to
# tolerate failed beats on the success path.
_LEAD_TARGET_FACTOR = 1.5


def _timeout_budget_ms(config: Any) -> float:
    """Outer deadline enforced around one complete create/extend attempt.

    HTTPX's connect/read/write/pool settings are phase or inactivity
    timeouts, not a whole-request deadline. The lifecycle therefore wraps
    each lease-bearing attempt in this total deadline. Pool acquisition and
    response-body parsing are inside the same bound.
    """
    parts = (float(config.connect_timeout), float(config.read_timeout), 5.0)
    if any(not math.isfinite(part) or part <= 0 for part in parts):
        return math.inf
    return sum(parts) * 1000.0


def _remaining_at_schedule_start(
    remaining_ms: int,
    received_ms: float | None,
    now_ms: float,
) -> int:
    """Deduct local setup time elapsed after the create response arrived."""
    if received_ms is None:
        return remaining_ms
    elapsed_ms = now_ms - received_ms
    if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
        return 0
    return max(0, math.floor(remaining_ms - elapsed_ms))


def _run_sync_attempt(call: Callable[[], CyclesResponse], timeout_budget_ms: float) -> CyclesResponse:
    """Run one synchronous HTTP attempt under a real whole-attempt deadline.

    HTTPX cannot impose a total deadline over all pool/connect/write/read
    phases. A daemon worker lets the heartbeat regain control at the
    configured deadline. A timed-out request may still finish in the
    background, which is why every recovery reuses the same idempotency key.
    """
    if not math.isfinite(timeout_budget_ms):
        return call()

    result: queue.Queue[tuple[bool, CyclesResponse | Exception]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, call()))
        except Exception as exc:  # propagate the original client failure
            result.put((False, exc))

    worker = threading.Thread(target=invoke, daemon=True, name="cycles-http-attempt")
    worker.start()
    worker.join(timeout_budget_ms / 1000.0)
    if worker.is_alive():
        raise TimeoutError(f"Cycles HTTP attempt exceeded {timeout_budget_ms:g}ms")
    ok, value = result.get_nowait()
    if ok:
        return value  # type: ignore[return-value]
    raise value  # type: ignore[misc]


async def _run_async_attempt(
    call: Callable[[], Awaitable[CyclesResponse]],
    timeout_budget_ms: float,
) -> CyclesResponse:
    """Run one asynchronous HTTP attempt under a whole-attempt deadline."""
    if not math.isfinite(timeout_budget_ms):
        return await call()
    return await asyncio.wait_for(call(), timeout=timeout_budget_ms / 1000.0)


_CREATE_RESPONSE_FIELDS = frozenset(
    {
        "decision",
        "reservation_id",
        "affected_scopes",
        "expires_at_ms",
        "remaining_ttl_ms",
        "scope_path",
        "reserved",
        "caps",
        "reason_code",
        "retry_after_ms",
        "balances",
        "cycles_evidence",
    }
)


def _contains_json_null(value: Any) -> bool:
    """Whether a lease response contains an OpenAPI-non-nullable JSON null."""
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_json_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_json_null(item) for item in value)
    return False


def _schema_valid_create(response: CyclesResponse) -> ReservationCreateResponse | None:
    """Return a fully validated create body only for exact HTTP 200."""
    if response.status != 200 or not isinstance(response.body, dict):
        return None
    body = response.body
    if set(body) - _CREATE_RESPONSE_FIELDS:
        return None
    if _contains_json_null(body):
        return None
    remaining = body.get("remaining_ttl_ms")
    if remaining is not None and (not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0):
        return None
    expires = body.get("expires_at_ms")
    if expires is not None and (not isinstance(expires, int) or isinstance(expires, bool) or expires < 0):
        return None
    affected = body.get("affected_scopes")
    if not isinstance(affected, list) or any(not isinstance(scope, str) for scope in affected):
        return None
    try:
        return ReservationCreateResponse.model_validate_json(json.dumps(body), strict=True)
    except Exception:
        return None


def _schema_valid_extend(response: CyclesResponse) -> ReservationExtendResponse | None:
    """Return a fully validated extend body only for exact HTTP 200."""
    if response.status != 200 or not isinstance(response.body, dict):
        return None
    body = response.body
    if set(body) - {"status", "expires_at_ms", "remaining_ttl_ms", "balances"}:
        return None
    if _contains_json_null(body):
        return None
    for name in ("expires_at_ms", "remaining_ttl_ms"):
        value = body.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            return None
    try:
        return ReservationExtendResponse.model_validate_json(json.dumps(body), strict=True)
    except Exception:
        return None


def _create_is_recoverable(response: CyclesResponse) -> bool:
    return response.status < 0 or response.status >= 500 or 200 <= response.status < 300


def _extend_error_is_recoverable(response: CyclesResponse) -> bool:
    """Whether a non-2xx extend outcome is recoverable in field mode."""
    return response.is_transport_error or response.is_server_error or response.status == 429


def _ambiguous_create_error(response: CyclesResponse) -> CyclesProtocolError:
    return CyclesProtocolError(
        "Create reservation did not produce a schema-valid HTTP 200 response",
        status=response.status,
    )


def _create_reservation_with_recovery(
    client: CyclesClient,
    body: dict[str, Any],
) -> tuple[CyclesResponse, ReservationCreateResponse, float, float]:
    """Create with at most one immediate same-key ambiguity recovery."""
    timeout_budget_ms = _timeout_budget_ms(client._config)
    last_exception: Exception | None = None
    for attempt in range(2):
        sent_ms = _now_mono_ms()
        try:
            response = _run_sync_attempt(
                lambda: client.create_reservation(body),
                timeout_budget_ms,
            )
        except Exception as exc:
            last_exception = exc
            if attempt == 0:
                continue
            raise CyclesProtocolError(
                f"Create reservation remained ambiguous after same-key retry: {exc}",
            ) from exc
        parsed = _schema_valid_create(response)
        if parsed is not None:
            received_ms = _now_mono_ms()
            return response, parsed, received_ms - sent_ms, received_ms
        if attempt == 0 and _create_is_recoverable(response):
            continue
        if response.status < 200 or response.status >= 300:
            raise _build_protocol_exception("Failed to create reservation", response)
        raise _ambiguous_create_error(response)
    raise AssertionError(last_exception)  # pragma: no cover


async def _create_reservation_with_recovery_async(
    client: AsyncCyclesClient,
    body: dict[str, Any],
) -> tuple[CyclesResponse, ReservationCreateResponse, float, float]:
    """Async create with at most one immediate same-key ambiguity recovery."""
    timeout_budget_ms = _timeout_budget_ms(client._config)
    last_exception: Exception | None = None
    for attempt in range(2):
        sent_ms = _now_mono_ms()
        try:
            response = await _run_async_attempt(
                lambda: client.create_reservation(body),
                timeout_budget_ms,
            )
        except Exception as exc:
            last_exception = exc
            if attempt == 0:
                continue
            raise CyclesProtocolError(
                f"Create reservation remained ambiguous after same-key retry: {exc}",
            ) from exc
        parsed = _schema_valid_create(response)
        if parsed is not None:
            received_ms = _now_mono_ms()
            return response, parsed, received_ms - sent_ms, received_ms
        if attempt == 0 and _create_is_recoverable(response):
            continue
        if response.status < 200 or response.status >= 300:
            raise _build_protocol_exception("Failed to create reservation", response)
        raise _ambiguous_create_error(response)
    raise AssertionError(last_exception)  # pragma: no cover


class _AuthoritativeScheduler:
    """Field-mode heartbeat scheduling per the NORMATIVE algorithm in the
    spec's HEARTBEAT GUIDANCE (v0.1.25.16). All methods return the next
    delay in ms, or ``None`` when the spec requires the heartbeat to stop
    and surface. State: max observed rtt, the lead floor established by the
    last schema-valid response, a zero-delay streak (a success that cannot
    hold the retry reserve permits ONE immediate fresh attempt, then stop),
    and the last failure's retry window (recovery may repeat with the same
    idempotency key only while the freshly recomputed window shrinks)."""

    def __init__(self, timeout_budget_ms: float) -> None:
        self._timeout_budget_ms = timeout_budget_ms
        self._rtt_max_ms = 0.0
        self._lead_floor_ms: float | None = None
        self._lead_anchor_ms: float | None = None
        self._zero_streak = 0
        self._prev_fail_window: float | None = None

    def _attempt_budget_ms(self) -> float:
        return max(self._timeout_budget_ms, 1000.0, 2.0 * self._rtt_max_ms)

    def _safety_margin_ms(self) -> float:
        return max(1000.0, 2.0 * self._rtt_max_ms)

    def observe_rtt(self, rtt_ms: float) -> None:
        """Retain every reliable schema-valid create/extend RTT sample."""
        if math.isfinite(rtt_ms) and rtt_ms >= 0:
            self._rtt_max_ms = max(self._rtt_max_ms, rtt_ms)

    def on_valid_success(
        self,
        remaining_ms: int,
        rtt_ms: float,
        now_ms: float,
    ) -> float | None:
        """Schema-valid HTTP 200 carrying remaining_ttl_ms. retry_reserve =
        2×attempt_budget + safety_margin covers one failed attempt, one
        same-key retry, and margin."""
        if not math.isfinite(rtt_ms) or rtt_ms < 0:
            # Unknown/unreliable timing cannot be treated as zero elapsed.
            self._rtt_max_ms = math.inf
            lead_floor_ms = 0.0
        else:
            self.observe_rtt(rtt_ms)
            lead_floor_ms = max(0.0, float(remaining_ms) - rtt_ms)
        self._prev_fail_window = None
        self._lead_floor_ms = lead_floor_ms
        self._lead_anchor_ms = now_ms
        reserve = 2.0 * self._attempt_budget_ms() + self._safety_margin_ms()
        delay = self._lead_floor_ms - reserve
        if delay > 0:
            self._zero_streak = 0
            return delay
        # The lease cannot hold the retry reserve: one immediate fresh
        # attempt (new key) is permitted — an additive-delta server may
        # establish positive lead — then the client must stop rather than
        # burn a maximum-lead server's extension budget in a tight loop.
        self._zero_streak += 1
        if self._zero_streak >= 2:
            return None
        return 0.0

    def lead_estimate_ms(self, now_ms: float) -> float:
        if self._lead_floor_ms is None or self._lead_anchor_ms is None:
            return 0.0
        elapsed_ms = now_ms - self._lead_anchor_ms
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            return 0.0
        return max(0.0, self._lead_floor_ms - elapsed_ms)

    def on_transient_failure(
        self,
        now_ms: float,
        rate_limited: bool = False,
        retry_after_ms: int | None = None,
    ) -> float | None:
        """Timeout / connection error / 5xx / 429 / ambiguous 2xx. The
        retry_window is recomputed from the same last schema-valid response;
        recovery repeats only while it shrinks (progress guard), and a 429
        may only be honored inside the window — never re-invented earlier."""
        lead_est = self.lead_estimate_ms(now_ms)
        window = lead_est - self._attempt_budget_ms() - self._safety_margin_ms()
        if window < 0:
            return None
        if self._prev_fail_window is not None and window >= self._prev_fail_window:
            return None  # no progress between consecutive failures
        self._prev_fail_window = window
        if rate_limited:
            if retry_after_ms is None or retry_after_ms < 0 or retry_after_ms > window:
                return None
            return float(retry_after_ms)
        return min(30_000.0, lead_est / 4.0, window)


def _build_extend_body(ttl_ms: int) -> dict[str, Any]:
    validate_extend_by_ms(ttl_ms)
    return {"idempotency_key": str(uuid.uuid4()), "extend_by_ms": ttl_ms}


def _build_protocol_exception(prefix: str, response: CyclesResponse) -> CyclesProtocolError:
    error_resp = response.get_error_response()
    error_code = None
    reason_code = None
    message = prefix
    request_id = None
    retry_after_ms = None

    details = None

    if error_resp:
        ec = error_resp.error_code
        error_code = ec.value if ec else None
        request_id = error_resp.request_id
        details = error_resp.details
        if error_resp.message:
            message = f"{prefix}: {error_resp.message}"
    else:
        raw_error = response.get_body_attribute("error")
        if raw_error:
            error_code = raw_error
        if response.error_message:
            message = f"{prefix}: {response.error_message}"

    # Extract reason_code from body (present in ReservationCreateResponse/DecisionResponse
    # for DENY cases); fall back to error_code for error responses
    reason_code = response.get_body_attribute("reason_code")
    if reason_code is None and error_code is not None:
        reason_code = error_code

    # Body field wins; otherwise fall back to the HTTP Retry-After header
    # (seconds → ms), which is how 429 LIMIT_EXCEEDED responses carry the
    # delay per runtime spec v0.1.25.12.
    retry_raw = response.get_body_attribute("retry_after_ms")
    if retry_raw is not None:
        retry_after_ms = int(retry_raw)
    else:
        retry_after_ms = response.retry_after_ms_header

    exc_class = CyclesProtocolError
    if error_code == "BUDGET_EXCEEDED":
        exc_class = BudgetExceededError
    elif error_code == "OVERDRAFT_LIMIT_EXCEEDED":
        exc_class = OverdraftLimitExceededError
    elif error_code == "DEBT_OUTSTANDING":
        exc_class = DebtOutstandingError
    elif error_code == "RESERVATION_EXPIRED":
        exc_class = ReservationExpiredError
    elif error_code == "RESERVATION_FINALIZED":
        exc_class = ReservationFinalizedError
    elif error_code == "TENANT_CLOSED":
        exc_class = TenantClosedError

    return exc_class(
        message,
        status=response.status,
        error_code=error_code,
        reason_code=reason_code,
        retry_after_ms=retry_after_ms,
        request_id=request_id,
        details=details,
    )


class CyclesLifecycle:
    """Synchronous lifecycle orchestrator: reserve → execute → commit/release."""

    def __init__(
        self,
        client: CyclesClient,
        retry_engine: CommitRetryEngine,
        default_subject: dict[str, str | None],
    ) -> None:
        self._client = client
        self._retry_engine = retry_engine
        self._retry_engine.set_client(client)
        self._default_subject = default_subject

    def execute(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        cfg: DecoratorConfig,
    ) -> Any:
        # Evaluate estimate
        estimate = _evaluate_amount(cfg.estimate, args, kwargs)
        logger.debug("Estimated usage: estimate=%d", estimate)

        # Create reservation
        create_body = _build_reservation_body(cfg, estimate, self._default_subject, args, kwargs)
        logger.debug("Creating reservation: body=%s", create_body)

        res_t1 = time.monotonic()
        res_response, res_result, _create_rtt_ms, _create_received_ms = _create_reservation_with_recovery(
            self._client,
            create_body,
        )
        res_t2 = time.monotonic()

        decision = res_result.decision
        reservation_id = res_result.reservation_id
        reason_code = res_result.reason_code

        # Handle dry-run
        if cfg.dry_run:
            elapsed_ms = int((res_t2 - res_t1) * 1000)
            if decision == Decision.DENY:
                logger.info("Dry-run denied: elapsed=%dms, reason=%s", elapsed_ms, reason_code)
                raise _build_protocol_exception("Dry-run denied", res_response)
            logger.info("Dry-run evaluated: elapsed=%dms, decision=%s", elapsed_ms, decision)
            return DryRunResult(
                decision=decision,
                caps=res_result.caps,
                affected_scopes=res_result.affected_scopes,
                scope_path=res_result.scope_path,
                reserved=res_result.reserved,
                balances=res_result.balances,
                reason_code=reason_code,
                retry_after_ms=res_result.retry_after_ms,
            )

        # Handle DENY
        if decision == Decision.DENY:
            logger.error("Reservation denied: reason=%s", reason_code)
            raise _build_protocol_exception("Reservation denied", res_response)

        if reservation_id is None:
            raise CyclesProtocolError("Reservation successful but reservation_id missing", status=res_response.status)

        logger.info(
            "Reservation created: id=%s, decision=%s, elapsed=%dms",
            reservation_id,
            decision,
            int((res_t2 - res_t1) * 1000),
        )

        # Set context
        ctx = CyclesContext(
            reservation_id=reservation_id,
            estimate=estimate,
            decision=decision,
            caps=res_result.caps,
            expires_at_ms=res_result.expires_at_ms,
            affected_scopes=res_result.affected_scopes,
            scope_path=res_result.scope_path,
            reserved=res_result.reserved,
            balances=res_result.balances,
        )
        _set_context(ctx)

        # Start heartbeat
        heartbeat_stop = threading.Event()
        heartbeat_thread = self._start_heartbeat(
            reservation_id,
            cfg.ttl_ms,
            ctx,
            heartbeat_stop,
            res_result.remaining_ttl_ms,
            _create_rtt_ms,
            _create_received_ms,
        )

        try:
            result = fn(*args, **kwargs)
            method_elapsed = int((time.monotonic() - res_t2) * 1000)
            logger.debug("Guarded action finished: id=%s, elapsed=%dms", reservation_id, method_elapsed)

            # Resolve actual
            actual_amount = _evaluate_actual(cfg.actual, result, estimate, cfg.use_estimate_if_actual_not_provided)

            # Build commit
            metrics = ctx.metrics
            if metrics is None:
                metrics = CyclesMetrics()
            if metrics.latency_ms is None:
                metrics.latency_ms = method_elapsed

            commit_metadata = ctx.commit_metadata
            if cfg.actual is None:
                # The estimate is being recorded as the actual (documented
                # fallback). Mark the evidence so auditors can distinguish
                # measured spend from assumed spend.
                logger.debug("No actual expression; committing estimate as actual: id=%s", reservation_id)
                commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
            commit_body = _build_commit_body(actual_amount, cfg.unit, metrics, commit_metadata)
            event_fallback = _build_event_fallback_body(
                reservation_id,
                create_body["subject"],
                create_body["action"],
                commit_body,
            )
            self._handle_commit(reservation_id, commit_body, event_fallback)

            return result

        except Exception:
            logger.error("Guarded action failed, releasing: id=%s", reservation_id, exc_info=True)
            self._handle_release(reservation_id, "guarded_method_failed")
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=1.0)
            _clear_context()

    def _handle_commit(
        self,
        reservation_id: str,
        commit_body: dict[str, Any],
        event_fallback_body: dict[str, Any],
    ) -> None:
        self._retry_engine.persist_pending(
            reservation_id, commit_body, event_fallback_body
        )
        try:
            logger.debug("Committing: id=%s", reservation_id)
            response = self._client.commit_reservation(reservation_id, commit_body)
            if _is_schema_valid_commit_success(response):
                self._retry_engine.discard_pending(reservation_id)
                logger.info("Commit successful: id=%s", reservation_id)
            elif response.is_success:
                logger.warning(
                    "Commit returned ambiguous protocol-invalid 2xx; scheduling same-key retry: "
                    "id=%s, status=%d",
                    reservation_id,
                    response.status,
                )
                self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
            elif response.is_transport_error or response.is_server_error:
                logger.warning("Commit failed (retryable): id=%s, status=%d", reservation_id, response.status)
                self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
            else:
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                    # Rate-limited, not rejected: releasing here would return
                    # budget for spend that already happened. Retry instead,
                    # honoring the server's Retry-After.
                    logger.warning("Commit rate-limited; scheduling retry: id=%s", reservation_id)
                    self._retry_engine.schedule(
                        reservation_id,
                        commit_body,
                        event_fallback_body,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status,
                        reservation_id,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        reservation_id,
                    )
                    self._retry_engine.schedule_event(reservation_id, event_fallback_body)
                elif error_code == "RESERVATION_FINALIZED":
                    self._retry_engine.discard_pending(reservation_id)
                    logger.warning("Reservation already finalized: id=%s", reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    self._retry_engine.discard_pending(reservation_id)
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    self._retry_engine.discard_pending(reservation_id)
                    self._handle_release(reservation_id, f"commit_rejected_{error_code}")
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Commit got unclassifiable client error (status=%d, error=%s); journaling for replay: id=%s",
                        response.status,
                        error_code,
                        reservation_id,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
                else:
                    logger.warning(
                        "Unrecognized commit response; scheduling same-key retry: id=%s, response=%s",
                        reservation_id,
                        response,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
        except Exception:
            logger.exception("Failed to commit: id=%s", reservation_id)
            self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)

    def _handle_release(self, reservation_id: str, reason: str) -> None:
        try:
            logger.info("Releasing: id=%s, reason=%s", reservation_id, reason)
            body = _build_release_body(reason)
            response = self._client.release_reservation(reservation_id, body)
            if response.is_success:
                logger.info("Released: id=%s", reservation_id)
            else:
                logger.warning("Release failed: id=%s, status=%d", reservation_id, response.status)
        except Exception:
            logger.exception("Failed to release: id=%s", reservation_id)

    def _start_heartbeat(
        self,
        reservation_id: str,
        ttl_ms: int,
        ctx: CyclesContext,
        stop_event: threading.Event,
        initial_remaining_ms: int | None = None,
        initial_rtt_ms: float | None = None,
        initial_received_ms: float | None = None,
    ) -> threading.Thread | None:
        if ttl_ms <= 0:
            return None

        def heartbeat_loop() -> None:
            # Conservative-lead heartbeat (v2.3): the only rigorous,
            # cross-clock-free quantity a client can maintain is a LOWER
            # BOUND on its remaining lead:
            #   lead_min = sum(measured grants) - monotonic elapsed
            # where each grant is the difference of successive returned
            # expires_at_ms values (same server frame). lead_min starts at
            # 0, so the FIRST extension fires immediately — establishing
            # real measured margin and revealing the actual per-extend
            # grant. Cadence then splits by regime: a grant that tracks the
            # lease (grant ≫ elapsed) drives cadence at grant/2; a grant
            # that merely mirrors elapsed time (maximum-lead clamping)
            # carries no cadence signal, so the loop holds a bounded
            # cadence instead of tightening. Skip when lead_min >=
            # 1.5*last_grant. Failed extends retry with the SAME body (same
            # idempotency key); permanent rejections stop the heartbeat.
            prev_expiry = ctx.expires_at_ms
            anchor_ms = _now_mono_ms()
            grants_sum = 0.0
            last_grant: float | None = None
            pending_body: dict[str, Any] | None = None
            last_success_ms = anchor_ms
            held_delay_ms = min(ttl_ms / 2, 30_000.0)
            clamp_warned = False
            # Authoritative scheduling (spec v0.1.25.16 PRIMARY ALGORITHM):
            # a schema-valid 200 carrying remaining_ttl_ms drives scheduling
            # exactly; the measured-grant heuristic below is the
            # NON-NORMATIVE fallback for servers that omit the field.
            timeout_budget_ms = _timeout_budget_ms(self._client._config)
            sched = _AuthoritativeScheduler(timeout_budget_ms)
            if initial_rtt_ms is not None:
                sched.observe_rtt(initial_rtt_ms)
            authoritative = initial_remaining_ms is not None
            if initial_remaining_ms is not None:
                remaining_at_start = _remaining_at_schedule_start(
                    initial_remaining_ms,
                    initial_received_ms,
                    anchor_ms,
                )
                first_delay = sched.on_valid_success(
                    remaining_at_start,
                    initial_rtt_ms or 0.0,
                    anchor_ms,
                )
                # Unreachable on the first scheduler call (the zero-delay
                # streak cannot be exhausted yet); kept as a typed guard.
                if first_delay is None:  # pragma: no cover
                    logger.warning(
                        "Heartbeat not started: lease shorter than the retry-safety budget: id=%s",
                        reservation_id,
                    )
                    return
                delay_ms = first_delay
            else:
                # Immediate first extension (fallback): with lead_min starting
                # at 0 and no lease signal on the wire, any bounded first
                # delay can outlive a policy-capped lease. Priming costs one
                # extension; total protected runtime is unchanged.
                delay_ms = 0.0
            while not stop_event.wait(timeout=delay_ms / 1000.0):
                if not authoritative:
                    # After the primed (delay-0) first beat, the baseline
                    # cadence is the held delay — a transient failure must
                    # not hot-loop. (Authoritative zero delays are meaningful:
                    # the one-immediate-attempt guards bound them.)
                    delay_ms = delay_ms or held_delay_ms
                if not authoritative:
                    lead_min = grants_sum - (_now_mono_ms() - anchor_ms)
                    if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                        continue
                try:
                    body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                    pending_body = body
                    sent_ms = _now_mono_ms()
                    response = _run_sync_attempt(
                        lambda: self._client.extend_reservation(reservation_id, body),
                        timeout_budget_ms,
                    )
                    parsed_extend = _schema_valid_extend(response)
                    recv_ms = _now_mono_ms()
                    if response.is_success and parsed_extend is None:
                        # Any non-200 or schema-invalid 2xx is ambiguous: it is
                        # never an observed success and the key stays pending.
                        logger.warning(
                            "Heartbeat ambiguous response (status=%d): id=%s",
                            response.status,
                            reservation_id,
                        )
                        if authoritative:
                            nxt = sched.on_transient_failure(_now_mono_ms())
                            if nxt is None:
                                logger.warning(
                                    "Heartbeat stopping: no safe recovery window remains: id=%s",
                                    reservation_id,
                                )
                                return
                            delay_ms = nxt
                        else:
                            delay_ms = held_delay_ms
                    elif parsed_extend is not None:
                        pending_body = None
                        new_expires = parsed_extend.expires_at_ms
                        ctx.update_expires_at_ms(new_expires)
                        grant = float(new_expires - prev_expiry) if prev_expiry is not None else float(ttl_ms)
                        prev_expiry = new_expires
                        grant = max(grant, 0.0)
                        now_ms = _now_mono_ms()
                        elapsed_since_success = now_ms - last_success_ms
                        last_success_ms = now_ms
                        grants_sum += grant
                        last_grant = grant
                        rtt_ms = recv_ms - sent_ms
                        sched.observe_rtt(rtt_ms)
                        remaining = parsed_extend.remaining_ttl_ms
                        if remaining is not None:
                            # Server-authoritative lease (spec v0.1.25.16
                            # PRIMARY ALGORITHM): schedule from this response
                            # alone; the heuristic arms below only serve
                            # servers that omit the field.
                            authoritative = True
                            nxt = sched.on_valid_success(remaining, rtt_ms, recv_ms)
                            if nxt is None:
                                logger.warning(
                                    "Heartbeat stopping: lease shorter than the retry-safety budget: id=%s",
                                    reservation_id,
                                )
                                return
                            delay_ms = nxt
                        elif grant <= 0 or (
                            grant < 0.9 * ttl_ms
                            and 0.75 * elapsed_since_success <= grant <= 1.25 * elapsed_since_success
                        ):
                            # Lead-clamping server: the grant mirrors elapsed
                            # time, not lease size — no cadence signal exists
                            # on the wire. Hold a bounded cadence; never
                            # tighten toward the floor (that would burn the
                            # max_extensions budget in seconds). The lower
                            # band keeps this non-sticky: a real small grant
                            # seen across a skip-doubled gap lands here once,
                            # but at the held cadence its grant/elapsed ratio
                            # falls below 0.75 and cadence re-tightens.
                            authoritative = False
                            delay_ms = held_delay_ms
                            if not clamp_warned:
                                clamp_warned = True
                                logger.warning(
                                    "Server appears to clamp lease lead; extension budget will deplete: id=%s",
                                    reservation_id,
                                )
                        else:
                            authoritative = False
                            delay_ms = min(max(grant / 2, 500.0), ttl_ms / 2)
                        logger.debug("Heartbeat extend ok: id=%s", reservation_id)
                    else:
                        code = _extract_error_code(response)
                        if response.status in (404, 410) or code in _PERMANENT_EXTEND_CODES:
                            logger.warning(
                                "Heartbeat stopping permanently (%s, status=%d): id=%s",
                                code,
                                response.status,
                                reservation_id,
                            )
                            return
                        logger.warning("Heartbeat extend failed: id=%s, status=%d", reservation_id, response.status)
                        if authoritative:
                            rate_limited = response.status == 429
                            if not rate_limited and 400 <= response.status < 500:
                                # Unrecoverable request/auth failure (spec): never
                                # rotate the key on an unchanged request.
                                logger.warning(
                                    "Heartbeat stopping on client error (%s, status=%d): id=%s",
                                    code,
                                    response.status,
                                    reservation_id,
                                )
                                return
                            if not _extend_error_is_recoverable(response):
                                logger.warning(
                                    "Heartbeat stopping on unexpected HTTP status %d: id=%s",
                                    response.status,
                                    reservation_id,
                                )
                                return
                            retry_after = response.retry_after_ms_header if rate_limited else None
                            nxt = sched.on_transient_failure(
                                _now_mono_ms(),
                                rate_limited=rate_limited,
                                retry_after_ms=retry_after,
                            )
                            if nxt is None:
                                logger.warning(
                                    "Heartbeat stopping: no safe recovery window remains: id=%s",
                                    reservation_id,
                                )
                                return
                            delay_ms = nxt
                except Exception:
                    if authoritative:
                        nxt = sched.on_transient_failure(_now_mono_ms())
                        if nxt is None:
                            logger.warning(
                                "Heartbeat extend transport error; stopping because no safe recovery "
                                "window remains: id=%s",
                                reservation_id,
                                exc_info=True,
                            )
                            return
                        delay_ms = nxt
                    logger.warning(
                        "Heartbeat extend transport error; retrying with the same idempotency key "
                        "in %.0fms: id=%s",
                        delay_ms,
                        reservation_id,
                        exc_info=True,
                    )

        t = threading.Thread(target=heartbeat_loop, daemon=True, name=f"cycles-heartbeat-{reservation_id[:12]}")
        t.start()
        return t


class AsyncCyclesLifecycle:
    """Asynchronous lifecycle orchestrator: reserve → execute → commit/release."""

    def __init__(
        self,
        client: AsyncCyclesClient,
        retry_engine: AsyncCommitRetryEngine,
        default_subject: dict[str, str | None],
    ) -> None:
        self._client = client
        self._retry_engine = retry_engine
        self._retry_engine.set_client(client)
        self._default_subject = default_subject

    async def execute(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        cfg: DecoratorConfig,
    ) -> Any:
        estimate = _evaluate_amount(cfg.estimate, args, kwargs)
        logger.debug("Estimated usage: estimate=%d", estimate)

        create_body = _build_reservation_body(cfg, estimate, self._default_subject, args, kwargs)
        res_response, res_result, _create_rtt_ms, _create_received_ms = await _create_reservation_with_recovery_async(
            self._client, create_body
        )
        res_t2 = time.monotonic()

        decision = res_result.decision
        reservation_id = res_result.reservation_id
        reason_code = res_result.reason_code

        if cfg.dry_run:
            if decision == Decision.DENY:
                raise _build_protocol_exception("Dry-run denied", res_response)
            return DryRunResult(
                decision=decision,
                caps=res_result.caps,
                affected_scopes=res_result.affected_scopes,
                scope_path=res_result.scope_path,
                reserved=res_result.reserved,
                balances=res_result.balances,
                reason_code=reason_code,
                retry_after_ms=res_result.retry_after_ms,
            )

        if decision == Decision.DENY:
            raise _build_protocol_exception("Reservation denied", res_response)

        if reservation_id is None:
            raise CyclesProtocolError("Reservation successful but reservation_id missing", status=res_response.status)

        logger.info("Reservation created: id=%s, decision=%s", reservation_id, decision)

        ctx = CyclesContext(
            reservation_id=reservation_id,
            estimate=estimate,
            decision=decision,
            caps=res_result.caps,
            expires_at_ms=res_result.expires_at_ms,
            affected_scopes=res_result.affected_scopes,
            scope_path=res_result.scope_path,
            reserved=res_result.reserved,
            balances=res_result.balances,
        )
        _set_context(ctx)

        heartbeat_task = self._start_heartbeat(
            reservation_id,
            cfg.ttl_ms,
            ctx,
            res_result.remaining_ttl_ms,
            _create_rtt_ms,
            _create_received_ms,
        )

        try:
            result = await fn(*args, **kwargs)
            method_elapsed = int((time.monotonic() - res_t2) * 1000)

            actual_amount = _evaluate_actual(cfg.actual, result, estimate, cfg.use_estimate_if_actual_not_provided)

            metrics = ctx.metrics
            if metrics is None:
                metrics = CyclesMetrics()
            if metrics.latency_ms is None:
                metrics.latency_ms = method_elapsed

            commit_metadata = ctx.commit_metadata
            if cfg.actual is None:
                # See the sync lifecycle: estimate recorded as actual is
                # marked so the evidence stays honest.
                logger.debug("No actual expression; committing estimate as actual: id=%s", reservation_id)
                commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
            commit_body = _build_commit_body(actual_amount, cfg.unit, metrics, commit_metadata)
            event_fallback = _build_event_fallback_body(
                reservation_id,
                create_body["subject"],
                create_body["action"],
                commit_body,
            )
            await self._handle_commit(reservation_id, commit_body, event_fallback)

            return result

        except Exception:
            logger.error("Guarded action failed, releasing: id=%s", reservation_id, exc_info=True)
            await self._handle_release(reservation_id, "guarded_method_failed")
            raise
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            _clear_context()

    async def _handle_commit(
        self,
        reservation_id: str,
        commit_body: dict[str, Any],
        event_fallback_body: dict[str, Any],
    ) -> None:
        self._retry_engine.persist_pending(
            reservation_id, commit_body, event_fallback_body
        )
        try:
            response = await self._client.commit_reservation(reservation_id, commit_body)
            if _is_schema_valid_commit_success(response):
                self._retry_engine.discard_pending(reservation_id)
                logger.info("Commit successful: id=%s", reservation_id)
            elif response.is_success:
                logger.warning(
                    "Commit returned ambiguous protocol-invalid 2xx; scheduling same-key retry: "
                    "id=%s, status=%d",
                    reservation_id,
                    response.status,
                )
                self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
            elif response.is_transport_error or response.is_server_error:
                self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
            else:
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                    # Rate-limited, not rejected: releasing here would return
                    # budget for spend that already happened. Retry instead,
                    # honoring the server's Retry-After.
                    logger.warning("Commit rate-limited; scheduling retry: id=%s", reservation_id)
                    self._retry_engine.schedule(
                        reservation_id,
                        commit_body,
                        event_fallback_body,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status,
                        reservation_id,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        reservation_id,
                    )
                    self._retry_engine.schedule_event(reservation_id, event_fallback_body)
                elif error_code == "RESERVATION_FINALIZED":
                    self._retry_engine.discard_pending(reservation_id)
                    logger.warning("Reservation already finalized: id=%s", reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    self._retry_engine.discard_pending(reservation_id)
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    self._retry_engine.discard_pending(reservation_id)
                    await self._handle_release(reservation_id, f"commit_rejected_{error_code}")
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Commit got unclassifiable client error (status=%d, error=%s); journaling for replay: id=%s",
                        response.status,
                        error_code,
                        reservation_id,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
                else:
                    logger.warning(
                        "Unrecognized commit response; scheduling same-key retry: id=%s, response=%s",
                        reservation_id,
                        response,
                    )
                    self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)
        except Exception:
            logger.exception("Failed to commit: id=%s", reservation_id)
            self._retry_engine.schedule(reservation_id, commit_body, event_fallback_body)

    async def _handle_release(self, reservation_id: str, reason: str) -> None:
        try:
            body = _build_release_body(reason)
            response = await self._client.release_reservation(reservation_id, body)
            if response.is_success:
                logger.info("Released: id=%s", reservation_id)
            else:
                logger.warning("Release failed: id=%s, status=%d", reservation_id, response.status)
        except Exception:
            logger.exception("Failed to release: id=%s", reservation_id)

    def _start_heartbeat(
        self,
        reservation_id: str,
        ttl_ms: int,
        ctx: CyclesContext,
        initial_remaining_ms: int | None = None,
        initial_rtt_ms: float | None = None,
        initial_received_ms: float | None = None,
    ) -> asyncio.Task[None] | None:
        if ttl_ms <= 0:
            return None

        async def heartbeat_loop() -> None:
            # Conservative-lead heartbeat (v2.3) — see the sync heartbeat.
            prev_expiry = ctx.expires_at_ms
            anchor_ms = _now_mono_ms()
            grants_sum = 0.0
            last_grant: float | None = None
            pending_body: dict[str, Any] | None = None
            last_success_ms = anchor_ms
            held_delay_ms = min(ttl_ms / 2, 30_000.0)
            clamp_warned = False
            # Authoritative scheduling (spec v0.1.25.16 PRIMARY ALGORITHM):
            # a schema-valid 200 carrying remaining_ttl_ms drives scheduling
            # exactly; the measured-grant heuristic below is the
            # NON-NORMATIVE fallback for servers that omit the field.
            timeout_budget_ms = _timeout_budget_ms(self._client._config)
            sched = _AuthoritativeScheduler(timeout_budget_ms)
            if initial_rtt_ms is not None:
                sched.observe_rtt(initial_rtt_ms)
            authoritative = initial_remaining_ms is not None
            if initial_remaining_ms is not None:
                remaining_at_start = _remaining_at_schedule_start(
                    initial_remaining_ms,
                    initial_received_ms,
                    anchor_ms,
                )
                first_delay = sched.on_valid_success(
                    remaining_at_start,
                    initial_rtt_ms or 0.0,
                    anchor_ms,
                )
                # Unreachable on the first scheduler call (the zero-delay
                # streak cannot be exhausted yet); kept as a typed guard.
                if first_delay is None:  # pragma: no cover
                    logger.warning(
                        "Heartbeat not started: lease shorter than the retry-safety budget: id=%s",
                        reservation_id,
                    )
                    return
                delay_ms = first_delay
            else:
                # Immediate first extension (fallback): with lead_min starting
                # at 0 and no lease signal on the wire, any bounded first
                # delay can outlive a policy-capped lease. Priming costs one
                # extension; total protected runtime is unchanged.
                delay_ms = 0.0
            try:
                while True:
                    await asyncio.sleep(delay_ms / 1000.0)
                    if not authoritative:
                        # After the primed (delay-0) first beat, the baseline
                        # cadence is the held delay — a transient failure must
                        # not hot-loop. (Authoritative zero delays are
                        # meaningful: the one-immediate-attempt guards bound
                        # them.)
                        delay_ms = delay_ms or held_delay_ms
                    if not authoritative:
                        lead_min = grants_sum - (_now_mono_ms() - anchor_ms)
                        if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                            continue
                    try:
                        body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                        pending_body = body
                        sent_ms = _now_mono_ms()
                        response = await _run_async_attempt(
                            lambda: self._client.extend_reservation(reservation_id, body),
                            timeout_budget_ms,
                        )
                        parsed_extend = _schema_valid_extend(response)
                        recv_ms = _now_mono_ms()
                        if response.is_success and parsed_extend is None:
                            # Any non-200 or schema-invalid 2xx is ambiguous:
                            # never rotate the pending idempotency key.
                            logger.warning(
                                "Heartbeat ambiguous response (status=%d): id=%s",
                                response.status,
                                reservation_id,
                            )
                            if authoritative:
                                nxt = sched.on_transient_failure(_now_mono_ms())
                                if nxt is None:
                                    logger.warning(
                                        "Heartbeat stopping: no safe recovery window remains: id=%s",
                                        reservation_id,
                                    )
                                    return
                                delay_ms = nxt
                            else:
                                delay_ms = held_delay_ms
                        elif parsed_extend is not None:
                            pending_body = None
                            new_expires = parsed_extend.expires_at_ms
                            ctx.update_expires_at_ms(new_expires)
                            grant = float(new_expires - prev_expiry) if prev_expiry is not None else float(ttl_ms)
                            prev_expiry = new_expires
                            grant = max(grant, 0.0)
                            now_ms = _now_mono_ms()
                            elapsed_since_success = now_ms - last_success_ms
                            last_success_ms = now_ms
                            grants_sum += grant
                            last_grant = grant
                            rtt_ms = recv_ms - sent_ms
                            sched.observe_rtt(rtt_ms)
                            remaining = parsed_extend.remaining_ttl_ms
                            if remaining is not None:
                                # Server-authoritative lease (spec v0.1.25.16
                                # PRIMARY ALGORITHM): schedule from this response
                                # alone; the heuristic arms below only serve
                                # servers that omit the field.
                                authoritative = True
                                nxt = sched.on_valid_success(remaining, rtt_ms, recv_ms)
                                if nxt is None:
                                    logger.warning(
                                        "Heartbeat stopping: lease shorter than the retry-safety budget: id=%s",
                                        reservation_id,
                                    )
                                    return
                                delay_ms = nxt
                            elif grant <= 0 or (
                                grant < 0.9 * ttl_ms
                                and 0.75 * elapsed_since_success <= grant <= 1.25 * elapsed_since_success
                            ):
                                # Lead-clamping server: the grant mirrors elapsed
                                # time, not lease size — no cadence signal exists
                                # on the wire. Hold a bounded cadence; never
                                # tighten toward the floor (that would burn the
                                # max_extensions budget in seconds). The lower
                                # band keeps this non-sticky: a real small grant
                                # seen across a skip-doubled gap lands here once,
                                # but at the held cadence its grant/elapsed ratio
                                # falls below 0.75 and cadence re-tightens.
                                authoritative = False
                                delay_ms = held_delay_ms
                                if not clamp_warned:
                                    clamp_warned = True
                                    logger.warning(
                                        "Server appears to clamp lease lead; extension budget will deplete: id=%s",
                                        reservation_id,
                                    )
                            else:
                                authoritative = False
                                delay_ms = min(max(grant / 2, 500.0), ttl_ms / 2)
                        else:
                            code = _extract_error_code(response)
                            if response.status in (404, 410) or code in _PERMANENT_EXTEND_CODES:
                                logger.warning(
                                    "Heartbeat stopping permanently (%s, status=%d): id=%s",
                                    code,
                                    response.status,
                                    reservation_id,
                                )
                                return
                            logger.warning("Heartbeat extend failed: id=%s", reservation_id)
                            if authoritative:
                                rate_limited = response.status == 429
                                if not rate_limited and 400 <= response.status < 500:
                                    # Unrecoverable request/auth failure (spec): never
                                    # rotate the key on an unchanged request.
                                    logger.warning(
                                        "Heartbeat stopping on client error (%s, status=%d): id=%s",
                                        code,
                                        response.status,
                                        reservation_id,
                                    )
                                    return
                                if not _extend_error_is_recoverable(response):
                                    logger.warning(
                                        "Heartbeat stopping on unexpected HTTP status %d: id=%s",
                                        response.status,
                                        reservation_id,
                                    )
                                    return
                                retry_after = response.retry_after_ms_header if rate_limited else None
                                nxt = sched.on_transient_failure(
                                    _now_mono_ms(),
                                    rate_limited=rate_limited,
                                    retry_after_ms=retry_after,
                                )
                                if nxt is None:
                                    logger.warning(
                                        "Heartbeat stopping: no safe recovery window remains: id=%s",
                                        reservation_id,
                                    )
                                    return
                                delay_ms = nxt
                    except Exception:
                        if authoritative:
                            nxt = sched.on_transient_failure(_now_mono_ms())
                            if nxt is None:
                                logger.warning(
                                    "Heartbeat extend transport error; stopping because no safe "
                                    "recovery window remains: id=%s",
                                    reservation_id,
                                    exc_info=True,
                                )
                                return
                            delay_ms = nxt
                        logger.warning(
                            "Heartbeat extend transport error; retrying with the same idempotency "
                            "key in %.0fms: id=%s",
                            delay_ms,
                            reservation_id,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                return

        return asyncio.create_task(heartbeat_loop())
