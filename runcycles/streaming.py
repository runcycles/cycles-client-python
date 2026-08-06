"""Streaming convenience: reserve on enter, commit/release on exit."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from runcycles import lifecycle as _lifecycle
from runcycles._validation import (
    validate_grace_period_ms,
    validate_non_negative,
    validate_subject,
    validate_ttl_ms,
)
from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.context import CyclesContext, _clear_context, _set_context
from runcycles.exceptions import CyclesProtocolError
from runcycles.lifecycle import (
    _LEAD_TARGET_FACTOR,
    _PERMANENT_EXTEND_CODES,
    _AuthoritativeScheduler,
    _build_commit_body,
    _build_event_fallback_body,
    _build_extend_body,
    _build_protocol_exception,
    _build_release_body,
    _create_reservation_with_recovery,
    _create_reservation_with_recovery_async,
    _remaining_at_schedule_start,
    _run_async_attempt,
    _run_sync_attempt,
    _schema_valid_extend,
    _timeout_budget_ms,
)
from runcycles.models import (
    Action,
    Amount,
    Caps,
    CommitRequest,
    CyclesMetrics,
    Decision,
    ReleaseRequest,
    ReservationCreateRequest,
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
class StreamUsage:
    """Mutable accumulator for streaming usage.

    Update fields during streaming; the context manager reads them at commit time.
    """

    tokens_input: int = 0
    tokens_output: int = 0
    actual_cost: int | None = None
    model_version: str | None = None
    custom: dict[str, Any] = field(default_factory=dict)

    def add_input_tokens(self, count: int) -> None:
        self.tokens_input += count

    def add_output_tokens(self, count: int) -> None:
        self.tokens_output += count

    def set_actual_cost(self, amount: int) -> None:
        self.actual_cost = amount


def _build_streaming_reservation_body(
    subject: Subject,
    action: Action,
    estimate: Amount,
    ttl_ms: int,
    overage_policy: str,
    grace_period_ms: int | None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    validate_subject(subject)
    validate_ttl_ms(ttl_ms)
    validate_grace_period_ms(grace_period_ms)

    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 256:
        raise ValueError("idempotency_key must be between 1 and 256 characters")

    body: dict[str, Any] = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "subject": subject.model_dump(exclude_none=True),
        "action": action.model_dump(exclude_none=True),
        "estimate": estimate.model_dump(exclude_none=True),
        "ttl_ms": ttl_ms,
        "overage_policy": overage_policy,
    }
    if grace_period_ms is not None:
        body["grace_period_ms"] = grace_period_ms
    return body


def _resolve_actual_cost(
    usage: StreamUsage,
    cost_fn: Callable[[StreamUsage], int] | None,
    estimate_amount: int,
) -> tuple[int, bool]:
    """Resolve the actual cost: explicit > cost_fn > estimate fallback.

    Returns ``(amount, from_estimate)`` — the flag is True when the estimate
    was substituted for an unmeasured actual, so the commit can carry an
    ``actual_source`` marker for audit honesty.
    """
    if usage.actual_cost is not None:
        try:
            validate_non_negative(usage.actual_cost, "actual_cost")
            return usage.actual_cost, False
        except (TypeError, ValueError):
            logger.warning("actual_cost is invalid, falling back to estimate", exc_info=True)
            return estimate_amount, True
    if cost_fn is not None:
        try:
            actual = cost_fn(usage)
            validate_non_negative(actual, "cost_fn result")
            return actual, False
        except Exception:
            logger.warning("cost_fn raised or returned an invalid amount, falling back to estimate", exc_info=True)
            return estimate_amount, True
    return estimate_amount, True


def _derived_stream_key(prefix: str, reservation_key: str | None) -> str | None:
    """Derive a bounded operation key from a caller-supplied reservation key."""
    if reservation_key is None:
        return None
    candidate = f"{prefix}-{reservation_key}"
    if len(candidate) <= 256:
        return candidate
    digest = hashlib.sha256(reservation_key.encode("utf-8")).hexdigest()
    return f"{prefix}-sha256-{digest}"


def _build_stream_metrics(
    usage: StreamUsage,
    elapsed_ms: int,
    ctx_metrics: CyclesMetrics | None,
) -> CyclesMetrics:
    """Build commit metrics, merging user-set ctx.metrics with stream usage."""
    if ctx_metrics is not None:
        # User set metrics on context during streaming — respect them,
        # but fill in latency if not already set.
        if ctx_metrics.latency_ms is None:
            ctx_metrics.latency_ms = elapsed_ms
        return ctx_metrics

    return CyclesMetrics(
        tokens_input=usage.tokens_input if usage.tokens_input else None,
        tokens_output=usage.tokens_output if usage.tokens_output else None,
        latency_ms=elapsed_ms,
        model_version=usage.model_version,
        custom=usage.custom or None,
    )


class StreamReservation:
    """Sync context manager: reserve on ``__enter__``, commit/release on ``__exit__``.

    Usage::

        with client.stream_reservation(
            action=Action(kind="llm.completion", name="gpt-4o"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1_000_000),
            cost_fn=lambda u: u.tokens_input * 250 + u.tokens_output * 1000,
        ) as reservation:
            for chunk in stream:
                reservation.usage.tokens_input = chunk.usage.prompt_tokens
                reservation.usage.tokens_output = chunk.usage.completion_tokens
        # Auto-committed on success, auto-released on exception.
    """

    def __init__(
        self,
        client: CyclesClient,
        *,
        subject: Subject,
        action: Action,
        estimate: Amount,
        ttl_ms: int = 120_000,
        grace_period_ms: int | None = None,
        overage_policy: str = "ALLOW_IF_AVAILABLE",
        cost_fn: Callable[[StreamUsage], int] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        raise_on_commit_failure: bool = False,
    ) -> None:
        self._client = client
        self._subject = subject
        self._action = action
        self._estimate = estimate
        self._ttl_ms = ttl_ms
        self._grace_period_ms = grace_period_ms
        self._overage_policy = overage_policy
        self._cost_fn = cost_fn
        self._metadata = metadata
        self._idempotency_key = idempotency_key
        self._raise_on_commit_failure = raise_on_commit_failure

        self._usage = StreamUsage()
        self._reservation_id: str | None = None
        self._initial_remaining: int | None = None
        self._create_rtt: float | None = None
        self._create_received_ms: float | None = None
        self._caps: Caps | None = None
        self._decision: Decision = Decision.ALLOW
        self._ctx: CyclesContext | None = None
        self._start_time: float = 0.0
        self._settlement_error: CyclesProtocolError | None = None
        self._release_error: CyclesProtocolError | None = None

        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        self._retry_engine = CommitRetryEngine(client._config)
        self._retry_engine.set_client(client)

    @property
    def usage(self) -> StreamUsage:
        return self._usage

    @property
    def reservation_id(self) -> str:
        if self._reservation_id is None:
            raise RuntimeError("reservation_id not available outside context manager")
        return self._reservation_id

    @property
    def caps(self) -> Caps | None:
        return self._caps

    @property
    def decision(self) -> Decision:
        return self._decision

    @property
    def settlement_error(self) -> CyclesProtocolError | None:
        """The last synchronous commit failure, after recovery was queued."""
        return self._settlement_error

    @property
    def release_error(self) -> CyclesProtocolError | None:
        """The last best-effort release failure, if any."""
        return self._release_error

    def __enter__(self) -> StreamReservation:
        body = _build_streaming_reservation_body(
            self._subject,
            self._action,
            self._estimate,
            self._ttl_ms,
            self._overage_policy,
            self._grace_period_ms,
            self._idempotency_key,
        )

        response, result, _create_rtt_ms, _create_received_ms = _create_reservation_with_recovery(
            self._client,
            ReservationCreateRequest.model_validate(body),
        )

        if result.decision == Decision.DENY:
            exc = _build_protocol_exception("Reservation denied", response)
            if exc.reason_code is None:
                exc.reason_code = result.reason_code
            if exc.retry_after_ms is None:
                exc.retry_after_ms = result.retry_after_ms
            raise exc

        if result.reservation_id is None:
            raise CyclesProtocolError(
                "Reservation successful but reservation_id missing",
                status=response.status,
            )

        self._reservation_id = result.reservation_id
        self._initial_remaining = result.remaining_ttl_ms
        self._create_rtt = _create_rtt_ms
        self._create_received_ms = _create_received_ms
        self._decision = result.decision
        self._caps = result.caps

        self._ctx = CyclesContext(
            reservation_id=result.reservation_id,
            estimate=self._estimate.amount,
            decision=result.decision,
            caps=result.caps,
            expires_at_ms=result.expires_at_ms,
            affected_scopes=result.affected_scopes,
            scope_path=result.scope_path,
            reserved=result.reserved,
            balances=result.balances,
        )
        _set_context(self._ctx)

        self._start_time = time.monotonic()
        self._heartbeat_thread = self._start_heartbeat()

        logger.info(
            "Stream reservation created: id=%s, decision=%s",
            self._reservation_id,
            self._decision,
        )

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)

        assert self._reservation_id is not None

        try:
            if exc_type is not None:
                self._handle_release("stream_failed")
            else:
                self._handle_commit()
        finally:
            _clear_context()

    def _handle_commit(self) -> None:
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        actual, actual_from_estimate = _resolve_actual_cost(self._usage, self._cost_fn, self._estimate.amount)
        ctx_metrics = self._ctx.metrics if self._ctx else None
        metrics = _build_stream_metrics(self._usage, elapsed_ms, ctx_metrics)
        unit = self._estimate.unit if isinstance(self._estimate.unit, str) else self._estimate.unit.value
        commit_metadata = self._metadata
        if actual_from_estimate:
            # Estimate recorded as actual — mark the evidence (audit honesty).
            commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
        commit_key = _derived_stream_key("commit", self._idempotency_key)
        commit_body = _build_commit_body(actual, unit, metrics, commit_metadata, commit_key)

        assert self._reservation_id is not None
        event_fallback = _build_event_fallback_body(
            self._reservation_id,
            self._subject.model_dump(exclude_none=True),
            self._action.model_dump(exclude_none=True),
            commit_body,
        )
        self._retry_engine.persist_pending(self._reservation_id, commit_body, event_fallback)
        failure_response = None
        try:
            response = self._client.commit_reservation(
                self._reservation_id,
                CommitRequest.model_validate(commit_body),
            )
            if _is_schema_valid_commit_success(response):
                self._retry_engine.discard_pending(self._reservation_id)
                logger.info("Stream commit successful: id=%s", self._reservation_id)
            elif response.is_success:
                failure_response = response
                logger.warning(
                    "Stream commit returned ambiguous protocol-invalid 2xx; "
                    "scheduling same-key retry: id=%s, status=%d",
                    self._reservation_id,
                    response.status,
                )
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            elif response.is_transport_error or response.is_server_error:
                failure_response = response
                logger.warning("Stream commit failed (retryable): id=%s", self._reservation_id)
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            else:
                failure_response = response
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                    # Rate-limited, not rejected: releasing here would return
                    # budget for spend that already happened. Retry instead,
                    # honoring the server's Retry-After.
                    logger.warning("Stream commit rate-limited; scheduling retry: id=%s", self._reservation_id)
                    self._retry_engine.schedule(
                        self._reservation_id,
                        commit_body,
                        event_fallback,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Stream commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status,
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule_event(self._reservation_id, event_fallback)
                elif error_code == "RESERVATION_FINALIZED":
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.warning("Reservation already finalized: id=%s", self._reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", self._reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.error(
                        "Stream commit was rejected after spend was recorded locally "
                        "(error=%s); not releasing known spend: id=%s",
                        error_code,
                        self._reservation_id,
                    )
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Stream commit got unclassifiable client error (status=%d, error=%s); "
                        "journaling for replay: id=%s",
                        response.status,
                        error_code,
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                else:
                    logger.warning(
                        "Unrecognized commit response; scheduling same-key retry: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
        except Exception as exc:
            logger.exception("Failed to commit stream: id=%s", self._reservation_id)
            self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            failure_response = CyclesResponse.transport_error(exc)

        if failure_response is not None:
            self._settlement_error = _build_protocol_exception(
                "Stream commit did not settle synchronously; known spend was not released",
                failure_response,
            )
            if self._raise_on_commit_failure:
                raise self._settlement_error

    def _handle_release(self, reason: str) -> None:
        assert self._reservation_id is not None
        try:
            release_key = _derived_stream_key("release", self._idempotency_key)
            body = _build_release_body(reason, release_key)
            response = self._client.release_reservation(
                self._reservation_id,
                ReleaseRequest.model_validate(body),
            )
            if response.is_success:
                logger.info("Stream released: id=%s", self._reservation_id)
            else:
                self._release_error = _build_protocol_exception("Stream release failed", response)
                logger.warning("Stream release failed: id=%s, status=%d", self._reservation_id, response.status)
        except Exception as exc:
            self._release_error = _build_protocol_exception(
                "Stream release failed",
                CyclesResponse.transport_error(exc),
            )
            logger.exception("Failed to release stream: id=%s", self._reservation_id)

    def _start_heartbeat(self) -> threading.Thread | None:
        if self._ttl_ms <= 0:
            return None
        assert self._reservation_id is not None
        reservation_id: str = self._reservation_id
        ctx = self._ctx

        def heartbeat_loop() -> None:
            # Conservative-lead heartbeat (v2.3) — see CyclesLifecycle heartbeat.
            ttl_ms = self._ttl_ms
            prev_expiry = ctx.expires_at_ms if ctx is not None else None
            anchor_ms = _lifecycle._now_mono_ms()
            grants_sum = 0.0
            last_grant: float | None = None
            pending_body: dict[str, Any] | None = None
            initial_remaining_ms = self._initial_remaining
            initial_rtt_ms = self._create_rtt
            initial_received_ms = self._create_received_ms
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
            while not self._heartbeat_stop.wait(timeout=delay_ms / 1000.0):
                if not authoritative:
                    # After the primed (delay-0) first beat, the baseline
                    # cadence is the held delay — a transient failure must
                    # not hot-loop. (Authoritative zero delays are meaningful:
                    # the one-immediate-attempt guards bound them.)
                    delay_ms = delay_ms or held_delay_ms
                if not authoritative:
                    lead_min = grants_sum - (_lifecycle._now_mono_ms() - anchor_ms)
                    if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                        continue
                try:
                    body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                    pending_body = body
                    sent_ms = _lifecycle._now_mono_ms()
                    response = _run_sync_attempt(
                        lambda: self._client.extend_reservation(reservation_id, body),
                        timeout_budget_ms,
                    )
                    parsed_extend = _schema_valid_extend(response)
                    recv_ms = _lifecycle._now_mono_ms()
                    if response.is_success and parsed_extend is None:
                        logger.warning(
                            "Heartbeat ambiguous response (status=%d): id=%s",
                            response.status,
                            reservation_id,
                        )
                        if authoritative:
                            nxt = sched.on_transient_failure(_lifecycle._now_mono_ms())
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
                        if ctx is not None:
                            ctx.update_expires_at_ms(new_expires)
                        grant = float(new_expires - prev_expiry) if prev_expiry is not None else float(ttl_ms)
                        prev_expiry = new_expires
                        grant = max(grant, 0.0)
                        now_ms = _lifecycle._now_mono_ms()
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
                                "Stream heartbeat stopping permanently (%s, status=%d): id=%s",
                                code,
                                response.status,
                                reservation_id,
                            )
                            return
                        logger.warning("Stream heartbeat failed: id=%s", reservation_id)
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
                            if not _lifecycle._extend_error_is_recoverable(response):
                                logger.warning(
                                    "Heartbeat stopping on unexpected HTTP status %d: id=%s",
                                    response.status,
                                    reservation_id,
                                )
                                return
                            retry_after = response.retry_after_ms_header if rate_limited else None
                            nxt = sched.on_transient_failure(
                                _lifecycle._now_mono_ms(),
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
                        nxt = sched.on_transient_failure(_lifecycle._now_mono_ms())
                        if nxt is None:
                            logger.warning(
                                "Stream heartbeat transport error; stopping because no safe recovery "
                                "window remains: id=%s",
                                reservation_id,
                                exc_info=True,
                            )
                            return
                        delay_ms = nxt
                    logger.warning(
                        "Stream heartbeat transport error; retrying with the same idempotency key in %.0fms: id=%s",
                        delay_ms,
                        reservation_id,
                        exc_info=True,
                    )

        t = threading.Thread(
            target=heartbeat_loop,
            daemon=True,
            name=f"cycles-stream-hb-{reservation_id[:12] if reservation_id else 'unknown'}",
        )
        t.start()
        return t


class AsyncStreamReservation:
    """Async context manager: reserve on ``__aenter__``, commit/release on ``__aexit__``.

    Usage::

        async with client.stream_reservation(
            action=Action(kind="llm.completion", name="gpt-4o"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1_000_000),
            cost_fn=lambda u: u.tokens_input * 250 + u.tokens_output * 1000,
        ) as reservation:
            async for chunk in stream:
                reservation.usage.tokens_input = chunk.usage.prompt_tokens
                reservation.usage.tokens_output = chunk.usage.completion_tokens
        # Auto-committed on success, auto-released on exception.
    """

    def __init__(
        self,
        client: AsyncCyclesClient,
        *,
        subject: Subject,
        action: Action,
        estimate: Amount,
        ttl_ms: int = 120_000,
        grace_period_ms: int | None = None,
        overage_policy: str = "ALLOW_IF_AVAILABLE",
        cost_fn: Callable[[StreamUsage], int] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        raise_on_commit_failure: bool = False,
    ) -> None:
        self._client = client
        self._subject = subject
        self._action = action
        self._estimate = estimate
        self._ttl_ms = ttl_ms
        self._grace_period_ms = grace_period_ms
        self._overage_policy = overage_policy
        self._cost_fn = cost_fn
        self._metadata = metadata
        self._idempotency_key = idempotency_key
        self._raise_on_commit_failure = raise_on_commit_failure

        self._usage = StreamUsage()
        self._reservation_id: str | None = None
        self._initial_remaining: int | None = None
        self._create_rtt: float | None = None
        self._create_received_ms: float | None = None
        self._caps: Caps | None = None
        self._decision: Decision = Decision.ALLOW
        self._ctx: CyclesContext | None = None
        self._start_time: float = 0.0
        self._settlement_error: CyclesProtocolError | None = None
        self._release_error: CyclesProtocolError | None = None

        self._heartbeat_task: asyncio.Task[None] | None = None

        self._retry_engine = AsyncCommitRetryEngine(client._config)
        self._retry_engine.set_client(client)

    @property
    def usage(self) -> StreamUsage:
        return self._usage

    @property
    def reservation_id(self) -> str:
        if self._reservation_id is None:
            raise RuntimeError("reservation_id not available outside context manager")
        return self._reservation_id

    @property
    def caps(self) -> Caps | None:
        return self._caps

    @property
    def decision(self) -> Decision:
        return self._decision

    @property
    def settlement_error(self) -> CyclesProtocolError | None:
        """The last synchronous commit failure, after recovery was queued."""
        return self._settlement_error

    @property
    def release_error(self) -> CyclesProtocolError | None:
        """The last best-effort release failure, if any."""
        return self._release_error

    async def __aenter__(self) -> AsyncStreamReservation:
        body = _build_streaming_reservation_body(
            self._subject,
            self._action,
            self._estimate,
            self._ttl_ms,
            self._overage_policy,
            self._grace_period_ms,
            self._idempotency_key,
        )

        response, result, _create_rtt_ms, _create_received_ms = await _create_reservation_with_recovery_async(
            self._client, ReservationCreateRequest.model_validate(body)
        )

        if result.decision == Decision.DENY:
            exc = _build_protocol_exception("Reservation denied", response)
            if exc.reason_code is None:
                exc.reason_code = result.reason_code
            if exc.retry_after_ms is None:
                exc.retry_after_ms = result.retry_after_ms
            raise exc

        if result.reservation_id is None:
            raise CyclesProtocolError(
                "Reservation successful but reservation_id missing",
                status=response.status,
            )

        self._reservation_id = result.reservation_id
        self._initial_remaining = result.remaining_ttl_ms
        self._create_rtt = _create_rtt_ms
        self._create_received_ms = _create_received_ms
        self._decision = result.decision
        self._caps = result.caps

        self._ctx = CyclesContext(
            reservation_id=result.reservation_id,
            estimate=self._estimate.amount,
            decision=result.decision,
            caps=result.caps,
            expires_at_ms=result.expires_at_ms,
            affected_scopes=result.affected_scopes,
            scope_path=result.scope_path,
            reserved=result.reserved,
            balances=result.balances,
        )
        _set_context(self._ctx)

        self._start_time = time.monotonic()
        self._heartbeat_task = self._start_heartbeat()

        logger.info(
            "Async stream reservation created: id=%s, decision=%s",
            self._reservation_id,
            self._decision,
        )

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        assert self._reservation_id is not None

        try:
            if exc_type is not None:
                await self._handle_release("stream_failed")
            else:
                await self._handle_commit()
        finally:
            _clear_context()

    async def _handle_commit(self) -> None:
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        actual, actual_from_estimate = _resolve_actual_cost(self._usage, self._cost_fn, self._estimate.amount)
        ctx_metrics = self._ctx.metrics if self._ctx else None
        metrics = _build_stream_metrics(self._usage, elapsed_ms, ctx_metrics)
        unit = self._estimate.unit if isinstance(self._estimate.unit, str) else self._estimate.unit.value
        commit_metadata = self._metadata
        if actual_from_estimate:
            # Estimate recorded as actual — mark the evidence (audit honesty).
            commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
        commit_key = _derived_stream_key("commit", self._idempotency_key)
        commit_body = _build_commit_body(actual, unit, metrics, commit_metadata, commit_key)

        assert self._reservation_id is not None
        event_fallback = _build_event_fallback_body(
            self._reservation_id,
            self._subject.model_dump(exclude_none=True),
            self._action.model_dump(exclude_none=True),
            commit_body,
        )
        self._retry_engine.persist_pending(self._reservation_id, commit_body, event_fallback)
        failure_response = None
        try:
            response = await self._client.commit_reservation(
                self._reservation_id,
                CommitRequest.model_validate(commit_body),
            )
            if _is_schema_valid_commit_success(response):
                self._retry_engine.discard_pending(self._reservation_id)
                logger.info("Async stream commit successful: id=%s", self._reservation_id)
            elif response.is_success:
                failure_response = response
                logger.warning(
                    "Async stream commit returned ambiguous protocol-invalid 2xx; "
                    "scheduling same-key retry: id=%s, status=%d",
                    self._reservation_id,
                    response.status,
                )
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            elif response.is_transport_error or response.is_server_error:
                failure_response = response
                logger.warning("Async stream commit failed (retryable): id=%s", self._reservation_id)
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            else:
                failure_response = response
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                    # Rate-limited, not rejected: releasing here would return
                    # budget for spend that already happened. Retry instead,
                    # honoring the server's Retry-After.
                    logger.warning("Async stream commit rate-limited; scheduling retry: id=%s", self._reservation_id)
                    self._retry_engine.schedule(
                        self._reservation_id,
                        commit_body,
                        event_fallback,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Stream commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status,
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule_event(self._reservation_id, event_fallback)
                elif error_code == "RESERVATION_FINALIZED":
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.warning("Reservation already finalized: id=%s", self._reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", self._reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    self._retry_engine.discard_pending(self._reservation_id)
                    logger.error(
                        "Async stream commit was rejected after spend was recorded locally "
                        "(error=%s); not releasing known spend: id=%s",
                        error_code,
                        self._reservation_id,
                    )
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Async stream commit got unclassifiable client error (status=%d, error=%s); "
                        "journaling for replay: id=%s",
                        response.status,
                        error_code,
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                else:
                    logger.warning(
                        "Unrecognized commit response; scheduling same-key retry: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
        except Exception as exc:
            logger.exception("Failed to commit async stream: id=%s", self._reservation_id)
            self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            failure_response = CyclesResponse.transport_error(exc)

        if failure_response is not None:
            self._settlement_error = _build_protocol_exception(
                "Async stream commit did not settle synchronously; known spend was not released",
                failure_response,
            )
            if self._raise_on_commit_failure:
                raise self._settlement_error

    async def _handle_release(self, reason: str) -> None:
        assert self._reservation_id is not None
        try:
            release_key = _derived_stream_key("release", self._idempotency_key)
            body = _build_release_body(reason, release_key)
            response = await self._client.release_reservation(
                self._reservation_id,
                ReleaseRequest.model_validate(body),
            )
            if response.is_success:
                logger.info("Async stream released: id=%s", self._reservation_id)
            else:
                self._release_error = _build_protocol_exception("Async stream release failed", response)
                logger.warning("Async stream release failed: id=%s, status=%d", self._reservation_id, response.status)
        except Exception as exc:
            self._release_error = _build_protocol_exception(
                "Async stream release failed",
                CyclesResponse.transport_error(exc),
            )
            logger.exception("Failed to release async stream: id=%s", self._reservation_id)

    def _start_heartbeat(self) -> asyncio.Task[None] | None:
        if self._ttl_ms <= 0:
            return None
        assert self._reservation_id is not None
        reservation_id: str = self._reservation_id
        ctx = self._ctx
        client = self._client
        ttl_ms = self._ttl_ms

        async def heartbeat_loop() -> None:
            # Conservative-lead heartbeat (v2.3) — see CyclesLifecycle heartbeat.
            prev_expiry = ctx.expires_at_ms if ctx is not None else None
            anchor_ms = _lifecycle._now_mono_ms()
            grants_sum = 0.0
            last_grant: float | None = None
            pending_body: dict[str, Any] | None = None
            initial_remaining_ms = self._initial_remaining
            initial_rtt_ms = self._create_rtt
            initial_received_ms = self._create_received_ms
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
                        lead_min = grants_sum - (_lifecycle._now_mono_ms() - anchor_ms)
                        if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                            continue
                    try:
                        body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                        pending_body = body
                        sent_ms = _lifecycle._now_mono_ms()
                        response = await _run_async_attempt(
                            lambda: client.extend_reservation(reservation_id, body),
                            timeout_budget_ms,
                        )
                        parsed_extend = _schema_valid_extend(response)
                        recv_ms = _lifecycle._now_mono_ms()
                        if response.is_success and parsed_extend is None:
                            logger.warning(
                                "Heartbeat ambiguous response (status=%d): id=%s",
                                response.status,
                                reservation_id,
                            )
                            if authoritative:
                                nxt = sched.on_transient_failure(_lifecycle._now_mono_ms())
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
                            if ctx is not None:
                                ctx.update_expires_at_ms(new_expires)
                            grant = float(new_expires - prev_expiry) if prev_expiry is not None else float(ttl_ms)
                            prev_expiry = new_expires
                            grant = max(grant, 0.0)
                            now_ms = _lifecycle._now_mono_ms()
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
                                    "Async stream heartbeat stopping permanently (%s, status=%d): id=%s",
                                    code,
                                    response.status,
                                    reservation_id,
                                )
                                return
                            logger.warning("Async stream heartbeat failed: id=%s", reservation_id)
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
                                if not _lifecycle._extend_error_is_recoverable(response):
                                    logger.warning(
                                        "Heartbeat stopping on unexpected HTTP status %d: id=%s",
                                        response.status,
                                        reservation_id,
                                    )
                                    return
                                retry_after = response.retry_after_ms_header if rate_limited else None
                                nxt = sched.on_transient_failure(
                                    _lifecycle._now_mono_ms(),
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
                            nxt = sched.on_transient_failure(_lifecycle._now_mono_ms())
                            if nxt is None:
                                logger.warning(
                                    "Async stream heartbeat transport error; stopping because no safe "
                                    "recovery window remains: id=%s",
                                    reservation_id,
                                    exc_info=True,
                                )
                                return
                            delay_ms = nxt
                        logger.warning(
                            "Async stream heartbeat transport error; retrying with the same "
                            "idempotency key in %.0fms: id=%s",
                            delay_ms,
                            reservation_id,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                return

        return asyncio.create_task(heartbeat_loop())
