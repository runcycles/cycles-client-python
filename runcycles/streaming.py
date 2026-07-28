"""Streaming convenience: reserve on enter, commit/release on exit."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from runcycles import lifecycle as _lifecycle
from runcycles._validation import validate_grace_period_ms, validate_subject, validate_ttl_ms
from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.context import CyclesContext, _clear_context, _set_context
from runcycles.exceptions import CyclesProtocolError
from runcycles.lifecycle import (
    _LEAD_TARGET_FACTOR,
    _PERMANENT_EXTEND_CODES,
    _authoritative_delay_ms,
    _build_commit_body,
    _build_event_fallback_body,
    _build_extend_body,
    _build_protocol_exception,
    _build_release_body,
)
from runcycles.models import (
    Action,
    Amount,
    Caps,
    CyclesMetrics,
    Decision,
    ReservationCreateResponse,
    Subject,
)
from runcycles.retry import (
    AsyncCommitRetryEngine,
    CommitRetryEngine,
    _extract_error_code,
    _is_recognized_rejection,
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
) -> dict[str, Any]:
    validate_subject(subject)
    validate_ttl_ms(ttl_ms)
    validate_grace_period_ms(grace_period_ms)

    body: dict[str, Any] = {
        "idempotency_key": str(uuid.uuid4()),
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
        return usage.actual_cost, False
    if cost_fn is not None:
        try:
            return cost_fn(usage), False
        except Exception:
            logger.warning("cost_fn raised, falling back to estimate", exc_info=True)
            return estimate_amount, True
    return estimate_amount, True


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

        self._usage = StreamUsage()
        self._reservation_id: str | None = None
        self._initial_remaining: int | None = None
        self._caps: Caps | None = None
        self._decision: Decision = Decision.ALLOW
        self._ctx: CyclesContext | None = None
        self._start_time: float = 0.0

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

    def __enter__(self) -> StreamReservation:
        body = _build_streaming_reservation_body(
            self._subject,
            self._action,
            self._estimate,
            self._ttl_ms,
            self._overage_policy,
            self._grace_period_ms,
        )

        response = self._client.create_reservation(body)

        if not response.is_success:
            raise _build_protocol_exception("Failed to create reservation", response)

        result = ReservationCreateResponse.model_validate(response.body)

        if result.decision == Decision.DENY:
            raise _build_protocol_exception("Reservation denied", response)

        if result.reservation_id is None:
            raise CyclesProtocolError(
                "Reservation successful but reservation_id missing",
                status=response.status,
            )

        self._reservation_id = result.reservation_id
        self._initial_remaining = result.remaining_ttl_ms
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
        actual, actual_from_estimate = _resolve_actual_cost(
            self._usage, self._cost_fn, self._estimate.amount
        )
        ctx_metrics = self._ctx.metrics if self._ctx else None
        metrics = _build_stream_metrics(self._usage, elapsed_ms, ctx_metrics)
        unit = self._estimate.unit if isinstance(self._estimate.unit, str) else self._estimate.unit.value
        commit_metadata = self._metadata
        if actual_from_estimate:
            # Estimate recorded as actual — mark the evidence (audit honesty).
            commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
        commit_body = _build_commit_body(actual, unit, metrics, commit_metadata)

        assert self._reservation_id is not None
        event_fallback = _build_event_fallback_body(
            self._reservation_id,
            self._subject.model_dump(exclude_none=True),
            self._action.model_dump(exclude_none=True),
            commit_body,
        )
        try:
            response = self._client.commit_reservation(self._reservation_id, commit_body)
            if response.is_success:
                logger.info("Stream commit successful: id=%s", self._reservation_id)
            elif response.is_transport_error or response.is_server_error:
                logger.warning("Stream commit failed (retryable): id=%s", self._reservation_id)
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            else:
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
                        self._reservation_id, commit_body, event_fallback,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Stream commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status, self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule_event(self._reservation_id, event_fallback)
                elif error_code == "RESERVATION_FINALIZED":
                    logger.warning("Reservation already finalized: id=%s", self._reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", self._reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    self._handle_release(f"commit_rejected_{error_code}")
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Stream commit got unclassifiable client error (status=%d, error=%s); "
                        "journaling for replay: id=%s",
                        response.status, error_code, self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                else:
                    logger.warning("Unrecognized commit response: id=%s", self._reservation_id)
        except Exception:
            logger.exception("Failed to commit stream: id=%s", self._reservation_id)
            self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)

    def _handle_release(self, reason: str) -> None:
        assert self._reservation_id is not None
        try:
            body = _build_release_body(reason)
            response = self._client.release_reservation(self._reservation_id, body)
            if response.is_success:
                logger.info("Stream released: id=%s", self._reservation_id)
            else:
                logger.warning("Stream release failed: id=%s, status=%d", self._reservation_id, response.status)
        except Exception:
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
            last_success_ms = anchor_ms
            held_delay_ms = min(ttl_ms / 2, 30_000.0)
            clamp_warned = False
            rtt_max_ms = 0.0
            # Authoritative scheduling (spec v0.1.25.16): a response carrying
            # remaining_ttl_ms is the server's own statement of the live
            # lease, so the beat is scheduled from it directly. When the
            # create response carried it, the first beat derives from it and
            # no primed extension is spent.
            lead_floor_ms: float | None = None
            lead_anchor_ms = anchor_ms
            authoritative = initial_remaining_ms is not None
            if initial_remaining_ms is not None:
                lead_floor_ms = float(initial_remaining_ms)
                delay_ms = _authoritative_delay_ms(lead_floor_ms, rtt_max_ms)
            else:
                # Immediate first extension (fallback): with lead_min starting
                # at 0 and no lease signal on the wire, any bounded first
                # delay can outlive a policy-capped lease. Priming costs one
                # extension; total protected runtime is unchanged.
                delay_ms = 0.0
            while not self._heartbeat_stop.wait(timeout=delay_ms / 1000.0):
                # After the primed (delay-0) first beat, the baseline cadence
                # is the held delay — a transient failure must not hot-loop.
                delay_ms = delay_ms or held_delay_ms
                if not authoritative:
                    lead_min = grants_sum - (_lifecycle._now_mono_ms() - anchor_ms)
                    if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                        continue
                try:
                    body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                    pending_body = body
                    sent_ms = _lifecycle._now_mono_ms()
                    response = self._client.extend_reservation(reservation_id, body)
                    recv_ms = _lifecycle._now_mono_ms()
                    if response.is_success:
                        pending_body = None
                        new_expires = response.get_body_attribute("expires_at_ms")
                        if new_expires is not None:
                            new_expires = int(new_expires)
                            if ctx is not None:
                                ctx.update_expires_at_ms(new_expires)
                            grant = (
                                float(new_expires - prev_expiry)
                                if prev_expiry is not None
                                else float(ttl_ms)
                            )
                            prev_expiry = new_expires
                        else:
                            grant = float(ttl_ms)
                            if prev_expiry is not None:
                                prev_expiry += ttl_ms
                        grant = max(grant, 0.0)
                        now_ms = _lifecycle._now_mono_ms()
                        elapsed_since_success = now_ms - last_success_ms
                        last_success_ms = now_ms
                        grants_sum += grant
                        last_grant = grant
                        rtt_ms = recv_ms - sent_ms
                        rtt_max_ms = max(rtt_max_ms, rtt_ms)
                        remaining = response.get_body_attribute("remaining_ttl_ms")
                        if remaining is not None:
                            # Server-authoritative lease (spec v0.1.25.16):
                            # schedule from it directly; the heuristic arms
                            # below only serve servers that omit the field.
                            authoritative = True
                            lead_floor_ms = max(0.0, float(int(remaining)) - rtt_ms)
                            lead_anchor_ms = recv_ms
                            delay_ms = _authoritative_delay_ms(lead_floor_ms, rtt_max_ms)
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
                                code, response.status, reservation_id,
                            )
                            return
                        logger.warning("Stream heartbeat failed: id=%s", reservation_id)
                        if authoritative and lead_floor_ms is not None:
                            # Retry inside the known lease: the failed extend may
                            # have been applied server-side, so the SAME body (same
                            # idempotency key) is retried at a bounded fraction of
                            # the remaining lead.
                            lead_now = max(0.0, lead_floor_ms - (_lifecycle._now_mono_ms() - lead_anchor_ms))
                            delay_ms = min(max(lead_now / 4, 1000.0), 30_000.0)
                except Exception:
                    logger.warning("Stream heartbeat error: id=%s", reservation_id, exc_info=True)
                    if authoritative and lead_floor_ms is not None:
                        # Retry inside the known lease: the failed extend may
                        # have been applied server-side, so the SAME body (same
                        # idempotency key) is retried at a bounded fraction of
                        # the remaining lead.
                        lead_now = max(0.0, lead_floor_ms - (_lifecycle._now_mono_ms() - lead_anchor_ms))
                        delay_ms = min(max(lead_now / 4, 1000.0), 30_000.0)

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

        self._usage = StreamUsage()
        self._reservation_id: str | None = None
        self._initial_remaining: int | None = None
        self._caps: Caps | None = None
        self._decision: Decision = Decision.ALLOW
        self._ctx: CyclesContext | None = None
        self._start_time: float = 0.0

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

    async def __aenter__(self) -> AsyncStreamReservation:
        body = _build_streaming_reservation_body(
            self._subject,
            self._action,
            self._estimate,
            self._ttl_ms,
            self._overage_policy,
            self._grace_period_ms,
        )

        response = await self._client.create_reservation(body)

        if not response.is_success:
            raise _build_protocol_exception("Failed to create reservation", response)

        result = ReservationCreateResponse.model_validate(response.body)

        if result.decision == Decision.DENY:
            raise _build_protocol_exception("Reservation denied", response)

        if result.reservation_id is None:
            raise CyclesProtocolError(
                "Reservation successful but reservation_id missing",
                status=response.status,
            )

        self._reservation_id = result.reservation_id
        self._initial_remaining = result.remaining_ttl_ms
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
        actual, actual_from_estimate = _resolve_actual_cost(
            self._usage, self._cost_fn, self._estimate.amount
        )
        ctx_metrics = self._ctx.metrics if self._ctx else None
        metrics = _build_stream_metrics(self._usage, elapsed_ms, ctx_metrics)
        unit = self._estimate.unit if isinstance(self._estimate.unit, str) else self._estimate.unit.value
        commit_metadata = self._metadata
        if actual_from_estimate:
            # Estimate recorded as actual — mark the evidence (audit honesty).
            commit_metadata = {**(commit_metadata or {}), "actual_source": "estimate"}
        commit_body = _build_commit_body(actual, unit, metrics, commit_metadata)

        assert self._reservation_id is not None
        event_fallback = _build_event_fallback_body(
            self._reservation_id,
            self._subject.model_dump(exclude_none=True),
            self._action.model_dump(exclude_none=True),
            commit_body,
        )
        try:
            response = await self._client.commit_reservation(self._reservation_id, commit_body)
            if response.is_success:
                logger.info("Async stream commit successful: id=%s", self._reservation_id)
            elif response.is_transport_error or response.is_server_error:
                logger.warning("Async stream commit failed (retryable): id=%s", self._reservation_id)
                self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
            else:
                error_code = None
                error_resp = response.get_error_response()
                if error_resp and error_resp.error_code:
                    error_code = error_resp.error_code.value
                if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                    # Rate-limited, not rejected: releasing here would return
                    # budget for spend that already happened. Retry instead,
                    # honoring the server's Retry-After.
                    logger.warning(
                        "Async stream commit rate-limited; scheduling retry: id=%s", self._reservation_id
                    )
                    self._retry_engine.schedule(
                        self._reservation_id, commit_body, event_fallback,
                        retry_after_ms=response.retry_after_ms_header,
                    )
                elif response.status in (401, 403):
                    # Credentials failed after the spend happened: journal the
                    # commit for replay once they're fixed. Never release —
                    # that would return budget for real spend.
                    logger.error(
                        "Stream commit got authentication failure (status=%d); journaling for replay: id=%s",
                        response.status, self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                    logger.warning(
                        "Reservation expired before commit; recovering spend via POST /v1/events: id=%s",
                        self._reservation_id,
                    )
                    self._retry_engine.schedule_event(self._reservation_id, event_fallback)
                elif error_code == "RESERVATION_FINALIZED":
                    logger.warning("Reservation already finalized: id=%s", self._reservation_id)
                elif error_code == "IDEMPOTENCY_MISMATCH":
                    logger.warning("Commit idempotency mismatch (not releasing): id=%s", self._reservation_id)
                elif response.is_client_error and _is_recognized_rejection(error_code):
                    await self._handle_release(f"commit_rejected_{error_code}")
                elif response.is_client_error:
                    # Codeless or forward-compat-unknown 4xx: neither release
                    # nor drop — retain the spend record.
                    logger.error(
                        "Async stream commit got unclassifiable client error (status=%d, error=%s); "
                        "journaling for replay: id=%s",
                        response.status, error_code, self._reservation_id,
                    )
                    self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)
                else:
                    logger.warning("Unrecognized commit response: id=%s", self._reservation_id)
        except Exception:
            logger.exception("Failed to commit async stream: id=%s", self._reservation_id)
            self._retry_engine.schedule(self._reservation_id, commit_body, event_fallback)

    async def _handle_release(self, reason: str) -> None:
        assert self._reservation_id is not None
        try:
            body = _build_release_body(reason)
            response = await self._client.release_reservation(self._reservation_id, body)
            if response.is_success:
                logger.info("Async stream released: id=%s", self._reservation_id)
            else:
                logger.warning("Async stream release failed: id=%s, status=%d", self._reservation_id, response.status)
        except Exception:
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
            last_success_ms = anchor_ms
            held_delay_ms = min(ttl_ms / 2, 30_000.0)
            clamp_warned = False
            rtt_max_ms = 0.0
            # Authoritative scheduling (spec v0.1.25.16): a response carrying
            # remaining_ttl_ms is the server's own statement of the live
            # lease, so the beat is scheduled from it directly. When the
            # create response carried it, the first beat derives from it and
            # no primed extension is spent.
            lead_floor_ms: float | None = None
            lead_anchor_ms = anchor_ms
            authoritative = initial_remaining_ms is not None
            if initial_remaining_ms is not None:
                lead_floor_ms = float(initial_remaining_ms)
                delay_ms = _authoritative_delay_ms(lead_floor_ms, rtt_max_ms)
            else:
                # Immediate first extension (fallback): with lead_min starting
                # at 0 and no lease signal on the wire, any bounded first
                # delay can outlive a policy-capped lease. Priming costs one
                # extension; total protected runtime is unchanged.
                delay_ms = 0.0
            try:
                while True:
                    await asyncio.sleep(delay_ms / 1000.0)
                    # After the primed (delay-0) first beat, the baseline
                    # cadence is the held delay — a transient failure must
                    # not hot-loop.
                    delay_ms = delay_ms or held_delay_ms
                    if not authoritative:
                        lead_min = grants_sum - (_lifecycle._now_mono_ms() - anchor_ms)
                        if last_grant is not None and lead_min >= _LEAD_TARGET_FACTOR * last_grant:
                            continue
                    try:
                        body = pending_body if pending_body is not None else _build_extend_body(ttl_ms)
                        pending_body = body
                        sent_ms = _lifecycle._now_mono_ms()
                        response = await client.extend_reservation(reservation_id, body)
                        recv_ms = _lifecycle._now_mono_ms()
                        if response.is_success:
                            pending_body = None
                            new_expires = response.get_body_attribute("expires_at_ms")
                            if new_expires is not None:
                                new_expires = int(new_expires)
                                if ctx is not None:
                                    ctx.update_expires_at_ms(new_expires)
                                grant = (
                                    float(new_expires - prev_expiry)
                                    if prev_expiry is not None
                                    else float(ttl_ms)
                                )
                                prev_expiry = new_expires
                            else:
                                grant = float(ttl_ms)
                                if prev_expiry is not None:
                                    prev_expiry += ttl_ms
                            grant = max(grant, 0.0)
                            now_ms = _lifecycle._now_mono_ms()
                            elapsed_since_success = now_ms - last_success_ms
                            last_success_ms = now_ms
                            grants_sum += grant
                            last_grant = grant
                            rtt_ms = recv_ms - sent_ms
                            rtt_max_ms = max(rtt_max_ms, rtt_ms)
                            remaining = response.get_body_attribute("remaining_ttl_ms")
                            if remaining is not None:
                                # Server-authoritative lease (spec v0.1.25.16):
                                # schedule from it directly; the heuristic arms
                                # below only serve servers that omit the field.
                                authoritative = True
                                lead_floor_ms = max(0.0, float(int(remaining)) - rtt_ms)
                                lead_anchor_ms = recv_ms
                                delay_ms = _authoritative_delay_ms(lead_floor_ms, rtt_max_ms)
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
                                    code, response.status, reservation_id,
                                )
                                return
                            logger.warning("Async stream heartbeat failed: id=%s", reservation_id)
                            if authoritative and lead_floor_ms is not None:
                                # Retry inside the known lease: the failed extend may
                                # have been applied server-side, so the SAME body (same
                                # idempotency key) is retried at a bounded fraction of
                                # the remaining lead.
                                lead_now = max(0.0, lead_floor_ms - (_lifecycle._now_mono_ms() - lead_anchor_ms))
                                delay_ms = min(max(lead_now / 4, 1000.0), 30_000.0)
                    except Exception:
                        logger.warning("Async stream heartbeat error: id=%s", reservation_id, exc_info=True)
                        if authoritative and lead_floor_ms is not None:
                            # Retry inside the known lease: the failed extend may
                            # have been applied server-side, so the SAME body (same
                            # idempotency key) is retried at a bounded fraction of
                            # the remaining lead.
                            lead_now = max(0.0, lead_floor_ms - (_lifecycle._now_mono_ms() - lead_anchor_ms))
                            delay_ms = min(max(lead_now / 4, 1000.0), 30_000.0)
            except asyncio.CancelledError:
                return

        return asyncio.create_task(heartbeat_loop())
