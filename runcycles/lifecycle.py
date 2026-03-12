"""Lifecycle orchestration: reserve → execute → commit/release."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.context import CyclesContext, _clear_context, _set_context
from runcycles.exceptions import (
    BudgetExceededError,
    CyclesProtocolError,
    DebtOutstandingError,
    OverdraftLimitExceededError,
    ReservationExpiredError,
    ReservationFinalizedError,
)
from runcycles.models import (
    Action,
    Amount,
    Caps,
    CyclesMetrics,
    Decision,
    DryRunResult,
    ErrorCode,
    ReservationResult,
    Subject,
    Unit,
)
from runcycles.response import CyclesResponse
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine
from runcycles._validation import validate_positive, validate_subject, validate_ttl_ms

logger = logging.getLogger(__name__)


@dataclass
class DecoratorConfig:
    """Configuration extracted from the @cycles decorator parameters."""

    estimate: int | Callable[..., int]
    actual: int | Callable[..., int] | None = None
    action_kind: str | None = None
    action_name: str | None = None
    action_tags: list[str] | None = None
    unit: str = "USD_MICROCENTS"
    ttl_ms: int = 60_000
    grace_period_ms: int | None = None
    overage_policy: str = "REJECT"
    dry_run: bool = False
    tenant: str | None = None
    workspace: str | None = None
    app: str | None = None
    workflow: str | None = None
    agent: str | None = None
    toolset: str | None = None
    dimensions: dict[str, str] | None = None
    use_estimate_if_actual_not_provided: bool = True


def _evaluate_amount(expr: int | Callable[..., int], args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    """Evaluate an estimate/actual expression, which may be a constant or a callable."""
    if callable(expr):
        return expr(*args, **kwargs)
    return int(expr)


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


def _build_reservation_body(cfg: DecoratorConfig, estimate: int, default_subject_fields: dict[str, str | None]) -> dict[str, Any]:
    """Build the reservation create request body."""
    validate_positive(estimate, "estimate")
    validate_ttl_ms(cfg.ttl_ms)

    subject: dict[str, Any] = {}
    for field_name in ("tenant", "workspace", "app", "workflow", "agent", "toolset"):
        val = getattr(cfg, field_name, None) or default_subject_fields.get(field_name)
        if val:
            subject[field_name] = val
    if cfg.dimensions:
        subject["dimensions"] = cfg.dimensions

    subject_model = Subject(**subject)
    validate_subject(subject_model)

    action: dict[str, Any] = {
        "kind": cfg.action_kind or "unknown",
        "name": cfg.action_name or "unknown",
    }
    if cfg.action_tags:
        action["tags"] = cfg.action_tags

    body: dict[str, Any] = {
        "idempotency_key": str(uuid.uuid4()),
        "subject": subject,
        "action": action,
        "estimate": {"unit": cfg.unit, "amount": estimate},
        "ttl_ms": cfg.ttl_ms,
        "overage_policy": cfg.overage_policy,
    }

    if cfg.grace_period_ms is not None:
        body["grace_period_ms"] = cfg.grace_period_ms
    if cfg.dry_run:
        body["dry_run"] = True

    return body


def _build_commit_body(actual: int, unit: str, metrics: CyclesMetrics | None, metadata: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": str(uuid.uuid4()),
        "actual": {"unit": unit, "amount": actual},
    }
    if metrics and not metrics.is_empty():
        body["metrics"] = metrics.model_dump(exclude_none=True)
    if metadata:
        body["metadata"] = metadata
    return body


def _build_release_body(reason: str) -> dict[str, Any]:
    return {"idempotency_key": str(uuid.uuid4()), "reason": reason}


def _build_extend_body(ttl_ms: int) -> dict[str, Any]:
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

    retry_raw = response.get_body_attribute("retry_after_ms")
    if retry_raw is not None:
        retry_after_ms = int(retry_raw)

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

    def __init__(self, client: CyclesClient, retry_engine: CommitRetryEngine, default_subject: dict[str, str | None]) -> None:
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
        create_body = _build_reservation_body(cfg, estimate, self._default_subject)
        logger.debug("Creating reservation: body=%s", create_body)

        res_t1 = time.monotonic()
        res_response = self._client.create_reservation(create_body)

        if not res_response.is_success:
            logger.error("Reservation failed: response=%s", res_response)
            raise _build_protocol_exception("Failed to create reservation", res_response)

        res_result = ReservationResult.model_validate(res_response.body)
        res_t2 = time.monotonic()

        decision = res_result.decision
        reservation_id = res_result.reservation_id
        reason_code = res_result.reason_code

        if decision is None:
            raise CyclesProtocolError("Unrecognized decision value from server", status=res_response.status, error_code="INTERNAL_ERROR")

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
            reservation_id, decision, int((res_t2 - res_t1) * 1000),
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
        heartbeat_thread = self._start_heartbeat(reservation_id, cfg.ttl_ms, ctx, heartbeat_stop)

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

            commit_body = _build_commit_body(actual_amount, cfg.unit, metrics, ctx.commit_metadata)
            self._handle_commit(reservation_id, commit_body)

            return result

        except Exception as ex:
            logger.error("Guarded action failed, releasing: id=%s", reservation_id, exc_info=True)
            self._handle_release(reservation_id, "guarded_method_failed")
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=1.0)
            _clear_context()

    def _handle_commit(self, reservation_id: str, commit_body: dict[str, Any]) -> None:
        try:
            logger.debug("Committing: id=%s", reservation_id)
            response = self._client.commit_reservation(reservation_id, commit_body)
            if response.is_success:
                logger.info("Commit successful: id=%s", reservation_id)
            elif response.is_transport_error or response.is_server_error:
                logger.warning("Commit failed (retryable): id=%s, status=%d", reservation_id, response.status)
                self._retry_engine.schedule(reservation_id, commit_body)
            else:
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if error_code in ("RESERVATION_FINALIZED", "RESERVATION_EXPIRED"):
                    logger.warning("Reservation already finalized/expired: id=%s", reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", reservation_id)
                elif response.is_client_error:
                    self._handle_release(reservation_id, f"commit_rejected_{error_code}")
                else:
                    logger.warning("Unrecognized commit response: id=%s, response=%s", reservation_id, response)
        except Exception:
            logger.exception("Failed to commit: id=%s", reservation_id)
            self._retry_engine.schedule(reservation_id, commit_body)

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
        self, reservation_id: str, ttl_ms: int, ctx: CyclesContext, stop_event: threading.Event,
    ) -> threading.Thread | None:
        if ttl_ms <= 0:
            return None
        interval_s = max(ttl_ms / 2, 1000) / 1000.0

        def heartbeat_loop() -> None:
            while not stop_event.wait(timeout=interval_s):
                try:
                    body = _build_extend_body(ttl_ms)
                    response = self._client.extend_reservation(reservation_id, body)
                    if response.is_success:
                        new_expires = response.get_body_attribute("expires_at_ms")
                        if new_expires is not None:
                            ctx.update_expires_at_ms(int(new_expires))
                        logger.debug("Heartbeat extend ok: id=%s", reservation_id)
                    else:
                        logger.warning("Heartbeat extend failed: id=%s, status=%d", reservation_id, response.status)
                except Exception:
                    logger.warning("Heartbeat extend error: id=%s", reservation_id, exc_info=True)

        t = threading.Thread(target=heartbeat_loop, daemon=True, name=f"cycles-heartbeat-{reservation_id[:12]}")
        t.start()
        return t


class AsyncCyclesLifecycle:
    """Asynchronous lifecycle orchestrator: reserve → execute → commit/release."""

    def __init__(self, client: AsyncCyclesClient, retry_engine: AsyncCommitRetryEngine, default_subject: dict[str, str | None]) -> None:
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

        create_body = _build_reservation_body(cfg, estimate, self._default_subject)
        res_t1 = time.monotonic()
        res_response = await self._client.create_reservation(create_body)

        if not res_response.is_success:
            raise _build_protocol_exception("Failed to create reservation", res_response)

        res_result = ReservationResult.model_validate(res_response.body)
        res_t2 = time.monotonic()

        decision = res_result.decision
        reservation_id = res_result.reservation_id
        reason_code = res_result.reason_code

        if decision is None:
            raise CyclesProtocolError("Unrecognized decision value from server", status=res_response.status, error_code="INTERNAL_ERROR")

        if cfg.dry_run:
            elapsed_ms = int((res_t2 - res_t1) * 1000)
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

        heartbeat_task = self._start_heartbeat(reservation_id, cfg.ttl_ms, ctx)

        try:
            result = await fn(*args, **kwargs)
            method_elapsed = int((time.monotonic() - res_t2) * 1000)

            actual_amount = _evaluate_actual(cfg.actual, result, estimate, cfg.use_estimate_if_actual_not_provided)

            metrics = ctx.metrics
            if metrics is None:
                metrics = CyclesMetrics()
            if metrics.latency_ms is None:
                metrics.latency_ms = method_elapsed

            commit_body = _build_commit_body(actual_amount, cfg.unit, metrics, ctx.commit_metadata)
            await self._handle_commit(reservation_id, commit_body)

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

    async def _handle_commit(self, reservation_id: str, commit_body: dict[str, Any]) -> None:
        try:
            response = await self._client.commit_reservation(reservation_id, commit_body)
            if response.is_success:
                logger.info("Commit successful: id=%s", reservation_id)
            elif response.is_transport_error or response.is_server_error:
                self._retry_engine.schedule(reservation_id, commit_body)
            else:
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if error_code in ("RESERVATION_FINALIZED", "RESERVATION_EXPIRED"):
                    logger.warning("Reservation already finalized/expired: id=%s", reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", reservation_id)
                elif response.is_client_error:
                    await self._handle_release(reservation_id, f"commit_rejected_{error_code}")
        except Exception:
            logger.exception("Failed to commit: id=%s", reservation_id)
            self._retry_engine.schedule(reservation_id, commit_body)

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

    def _start_heartbeat(self, reservation_id: str, ttl_ms: int, ctx: CyclesContext) -> asyncio.Task[None] | None:
        if ttl_ms <= 0:
            return None
        interval_s = max(ttl_ms / 2, 1000) / 1000.0

        async def heartbeat_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        body = _build_extend_body(ttl_ms)
                        response = await self._client.extend_reservation(reservation_id, body)
                        if response.is_success:
                            new_expires = response.get_body_attribute("expires_at_ms")
                            if new_expires is not None:
                                ctx.update_expires_at_ms(int(new_expires))
                        else:
                            logger.warning("Heartbeat extend failed: id=%s", reservation_id)
                    except Exception:
                        logger.warning("Heartbeat extend error: id=%s", reservation_id, exc_info=True)
            except asyncio.CancelledError:
                return

        return asyncio.create_task(heartbeat_loop())
