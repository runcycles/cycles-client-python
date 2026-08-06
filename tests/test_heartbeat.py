"""Deterministic tests for the conservative-lead (v2.3) heartbeat and the
``actual_source: estimate`` audit marker."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from runcycles.config import CyclesConfig
from runcycles.exceptions import CyclesProtocolError
from runcycles.lifecycle import (
    AsyncCyclesLifecycle,
    CyclesLifecycle,
    DecoratorConfig,
    _AuthoritativeScheduler,
    _create_reservation_with_recovery,
    _create_reservation_with_recovery_async,
    _remaining_at_schedule_start,
    _run_async_attempt,
    _run_sync_attempt,
    _schema_valid_create,
    _schema_valid_extend,
)
from runcycles.models import Action, Amount, Subject, Unit
from runcycles.response import CyclesResponse
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine
from runcycles.streaming import AsyncStreamReservation, StreamReservation

TTL = 60_000
INITIAL_EXPIRY = 60_000  # server frame; arbitrary origin


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def _config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878",
        api_key="test-key",
        tenant="acme",
        retry_enabled=False,
    )


def _extend_ok(
    expires_at_ms: int | None,
    remaining_ttl_ms: int | None = None,
) -> CyclesResponse:
    body: dict[str, Any] = {"status": "ACTIVE"}
    if expires_at_ms is not None:
        body["expires_at_ms"] = expires_at_ms
    if remaining_ttl_ms is not None:
        body["remaining_ttl_ms"] = remaining_ttl_ms
    return CyclesResponse.success(200, body)


def _allow_response() -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {
            "decision": "ALLOW",
            "reservation_id": "rsv_test",
            "expires_at_ms": int(time.time() * 1000) + 600_000,
            "affected_scopes": ["tenant:acme"],
            "scope_path": "tenant:acme",
            "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
        },
    )


def _commit_success() -> CyclesResponse:
    return CyclesResponse.success(200, {"status": "COMMITTED"})


def _make_sync() -> tuple[CyclesLifecycle, MagicMock]:
    client = MagicMock()
    client._config = _config()
    engine = MagicMock(spec=CommitRetryEngine)
    return CyclesLifecycle(client, engine, {"tenant": "acme"}), client


def _make_async() -> tuple[AsyncCyclesLifecycle, AsyncMock]:
    client = AsyncMock()
    client._config = _config()
    engine = MagicMock(spec=AsyncCommitRetryEngine)
    return AsyncCyclesLifecycle(client, engine, {"tenant": "acme"}), client


def _ctx(expires_at_ms: int | None = INITIAL_EXPIRY) -> MagicMock:
    ctx = MagicMock()
    ctx.expires_at_ms = expires_at_ms
    return ctx


def _run_sync_beats(
    lifecycle: CyclesLifecycle,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    beats: int,
    ttl: int = TTL,
    ctx: MagicMock | None = None,
    initial_remaining_ms: int | None = None,
) -> list[float]:
    """Drive the sync heartbeat for `beats` iterations, advancing the fake
    clock by the beat interval on every wait. Returns the wait timeouts."""
    monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
    timeouts: list[float] = []
    calls = {"n": 0}

    def wait(timeout: float | None = None) -> bool:
        timeouts.append(timeout or 0.0)
        calls["n"] += 1
        if calls["n"] > beats:
            return True
        clock.t += (timeout or 0.0) * 1000.0
        return False

    stop = threading.Event()
    stop.wait = wait  # type: ignore[method-assign]
    thread = lifecycle._start_heartbeat(
        "rsv_1",
        ttl,
        ctx or _ctx(),
        stop,
        initial_remaining_ms,
    )
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    return timeouts


class TestStrictLeaseAttemptContract:
    def test_field_mode_retains_rtt_observed_before_field_appears(self) -> None:
        scheduler = _AuthoritativeScheduler(7_000)
        scheduler.observe_rtt(6_000)

        assert scheduler.on_valid_success(60_000, 100, 0) == 23_900

    @pytest.mark.parametrize(
        ("received_ms", "now_ms", "expected"),
        [(1000.0, 3000.0, 58_000.0), (3000.0, 1000.0, 0.0), (None, 3000.0, 60_000.0)],
    )
    def test_create_lead_deducts_post_receipt_setup_time(
        self,
        received_ms: float | None,
        now_ms: float,
        expected: float,
    ) -> None:
        assert _remaining_at_schedule_start(60_000, received_ms, now_ms) == expected

    def test_sync_create_recovers_once_with_same_body(self) -> None:
        client = MagicMock()
        client._config = _config()
        body = {"idempotency_key": "same-key"}
        client.create_reservation.side_effect = [
            CyclesResponse.success(202, _allow_response().body),
            _allow_response(),
        ]

        response, parsed, rtt_ms, received_ms = _create_reservation_with_recovery(client, body)

        assert response.status == 200
        assert parsed.reservation_id == "rsv_test"
        assert rtt_ms >= 0
        assert received_ms >= rtt_ms
        assert client.create_reservation.call_count == 2
        assert all(call.args[0] is body for call in client.create_reservation.call_args_list)

    def test_sync_create_rejects_malformed_200_after_one_recovery(self) -> None:
        client = MagicMock()
        client._config = _config()
        malformed = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "reservation_id": "rsv_test",
                "affected_scopes": ["tenant:acme"],
                "unexpected": True,
            },
        )
        client.create_reservation.return_value = malformed

        with pytest.raises(CyclesProtocolError, match="schema-valid HTTP 200"):
            _create_reservation_with_recovery(client, {"idempotency_key": "same-key"})

        assert client.create_reservation.call_count == 2

    @pytest.mark.asyncio
    async def test_async_create_recovers_transport_failure_with_same_body(self) -> None:
        client = AsyncMock()
        client._config = _config()
        body = {"idempotency_key": "same-key"}
        client.create_reservation.side_effect = [ConnectionError("lost"), _allow_response()]

        response, parsed, rtt_ms, received_ms = await _create_reservation_with_recovery_async(client, body)

        assert response.status == 200
        assert parsed.reservation_id == "rsv_test"
        assert rtt_ms >= 0
        assert received_ms >= rtt_ms
        assert client.create_reservation.await_count == 2
        assert all(call.args[0] is body for call in client.create_reservation.call_args_list)

    @pytest.mark.parametrize(
        "response",
        [
            CyclesResponse.success(
                202,
                {"status": "ACTIVE", "expires_at_ms": 1, "remaining_ttl_ms": 0},
            ),
            CyclesResponse.success(200, {"status": "ACTIVE"}),
            CyclesResponse.success(200, {"status": "ACTIVE", "expires_at_ms": -1}),
            CyclesResponse.success(200, {"status": "UNKNOWN", "expires_at_ms": 1}),
            CyclesResponse.success(200, {"status": "ACTIVE", "expires_at_ms": 1, "balances": {}}),
            CyclesResponse.success(200, {"status": "ACTIVE", "expires_at_ms": 1, "extra": True}),
        ],
    )
    def test_extend_rejects_non_200_or_non_schema_response(
        self,
        response: CyclesResponse,
    ) -> None:
        assert _schema_valid_extend(response) is None

    def test_strict_create_and_extend_accept_schema_valid_200(self) -> None:
        assert _schema_valid_create(_allow_response()) is not None
        parsed = _schema_valid_extend(
            CyclesResponse.success(
                200,
                {"status": "ACTIVE", "expires_at_ms": 1, "remaining_ttl_ms": 0, "balances": []},
            ),
        )
        assert parsed is not None
        assert parsed.remaining_ttl_ms == 0

    @pytest.mark.parametrize(
        "body",
        [
            {"decision": "ALLOW", "affected_scopes": [], "caps": None},
            {
                "decision": "ALLOW",
                "affected_scopes": [],
                "reserved": {"unit": "TOKENS", "amount": "1"},
            },
            {
                "decision": "ALLOW",
                "affected_scopes": [],
                "balances": [
                    {
                        "scope": "tenant:acme",
                        "scope_path": "tenant:acme",
                        "remaining": {"unit": "TOKENS", "amount": 1},
                        "reserved": None,
                    }
                ],
            },
            {
                "decision": "ALLOW",
                "affected_scopes": [],
                "cycles_evidence": {
                    "evidence_id": "a" * 64,
                    "cycles_evidence_url": "relative",
                },
            },
            {
                "decision": "ALLOW",
                "affected_scopes": [],
                "remaining_ttl_ms": 9_223_372_036_854_775_808,
            },
            {
                "decision": "ALLOW",
                "affected_scopes": [],
                "expires_at_ms": -1,
            },
        ],
    )
    def test_create_rejects_non_schema_optional_fields(self, body: dict[str, Any]) -> None:
        assert _schema_valid_create(CyclesResponse.success(200, body)) is None

    def test_extend_rejects_nested_null_and_int64_overflow(self) -> None:
        for body in (
            {
                "status": "ACTIVE",
                "expires_at_ms": 1,
                "balances": [
                    {
                        "scope": "tenant:acme",
                        "scope_path": "tenant:acme",
                        "remaining": {"unit": "TOKENS", "amount": 1},
                        "debt": None,
                    }
                ],
            },
            {
                "status": "ACTIVE",
                "expires_at_ms": 9_223_372_036_854_775_808,
            },
        ):
            assert _schema_valid_extend(CyclesResponse.success(200, body)) is None

    def test_sync_outer_deadline_covers_complete_attempt(self) -> None:
        with pytest.raises(TimeoutError, match="exceeded"):
            _run_sync_attempt(
                lambda: (time.sleep(0.05), _allow_response())[1],
                timeout_budget_ms=1,
            )

    @pytest.mark.asyncio
    async def test_async_outer_deadline_covers_complete_attempt(self) -> None:
        async def slow() -> CyclesResponse:
            await asyncio.sleep(0.05)
            return _allow_response()

        with pytest.raises(asyncio.TimeoutError):
            await _run_async_attempt(slow, timeout_budget_ms=1)


class TestSyncHeartbeatLeadEstimate:
    def test_extends_only_when_lead_below_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # lead_min starts at 0 and the first beat fires IMMEDIATELY: beats
        # 1-3 extend (grants measured at +ttl each), beat 4 skips once
        # lead_min reaches 1.5*grant (180k-90k=90k), beat 5 extends again.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=5)

        assert client.extend_reservation.call_count == 4
        assert timeouts[0] == 0.0
        assert timeouts[1] == min(TTL / 2, 30_000) / 1000.0

    def test_interval_has_no_floor_for_small_ttl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ttl=1200 → after the immediate first beat, cadence must be 600ms
        # (the old 1s floor guaranteed lapse in this spec-legal range).
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)

        ctx = _ctx(1200)
        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=2,
            ttl=1200,
            ctx=ctx,
        )

        assert timeouts[0] == 0.0
        assert timeouts[1] == 0.6

    def test_failed_extend_retries_with_same_idempotency_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A lost/failed extend may have been applied server-side: the retry
        # must reuse the same key so it cannot double-extend. After a
        # success, a fresh key is used.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(INITIAL_EXPIRY + TTL),
            _extend_ok(INITIAL_EXPIRY + 2 * TTL),
        ]

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3)

        bodies = [c.args[1] for c in client.extend_reservation.call_args_list]
        assert len(bodies) == 3
        assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]
        assert bodies[2]["idempotency_key"] != bodies[0]["idempotency_key"]

    def test_permanent_code_stops_heartbeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            409,
            "capped",
            body={"error": "MAX_EXTENSIONS_EXCEEDED", "message": "m", "request_id": "r"},
        )

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=4)

        # One doomed call, then the loop self-terminates — no retry spam.
        assert client.extend_reservation.call_count == 1

    def test_clamped_grants_extend_every_beat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Server clamps each grant to ttl/4: the lead estimate sees the
        # small grants (authoritative expires_at) and keeps extending.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * (TTL // 4)) for n in range(3)]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3)

        assert client.extend_reservation.call_count == 3
        # Cadence re-derived from the MEASURED grant (ttl/4 → beat at ttl/8).
        assert timeouts[1] == (TTL / 4 / 2) / 1000.0

    def test_grant_clamp_misclassification_after_skip_is_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # After a skip, the next measured grant arrives across a doubled
        # gap, so grant ≈ elapsed and the beat lands in the lead-clamp arm
        # once. The classifier's lower band (0.75×elapsed) must make that
        # non-sticky: at the held cadence the ratio falls to ~0.5, the
        # regime reads normal again, and cadence re-tightens — without the
        # band the hold sticks and a ttl/4-grant lease decays to a lapse.
        lifecycle, client = _make_sync()
        grant = TTL // 4  # 15000 → cadence ttl/8 = 7500ms
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * grant) for n in range(6)]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=7)

        # b1-b3 extend @7.5s, b4 skips (lead 22.5k ≥ 1.5×15k), b5 extends
        # across the doubled gap (misclassified → one 30s hold), b6
        # re-tightens to 7.5s, b7 extends on cadence.
        assert client.extend_reservation.call_count == 6
        assert timeouts == [0.0, 7.5, 7.5, 7.5, 7.5, 30.0, 7.5, 7.5]

    def test_missing_expires_in_response_falls_back_to_plus_ttl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)
        ctx = _ctx()

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3, ctx=ctx)

        # Fallback grant = requested ttl; lead builds from 0 so all three
        # beats extend — and ctx is never updated without an expires value.
        assert client.extend_reservation.call_count == 3
        ctx.update_expires_at_ms.assert_not_called()

    def test_unknown_initial_expiry_anchors_on_first_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(500_000),  # beat 1: fallback grant, sets frame
            _extend_ok(500_000 + 3 * TTL),  # beat 2: big measured grant
        ]

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3, ctx=_ctx(None))

        # Beat 1 (immediate) extends with the fallback grant (no prior
        # frame); beat 2 measures a 3*ttl grant. Beat 3: lead_min =
        # (ttl + 3*ttl) - 60s = 180s < 1.5*last_grant = 270s → NOT a skip,
        # so a 3rd call happens; its StopIteration is swallowed as a
        # transient error and the count stays meaningful at 3.
        assert client.extend_reservation.call_count == 3

    def test_tenant_closed_stops_heartbeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            409,
            "closed",
            body={"error": "TENANT_CLOSED", "message": "m", "request_id": "r"},
        )

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=4)

        assert client.extend_reservation.call_count == 1

    def test_transient_failure_then_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-permanent failures warn and keep the loop alive.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(503, "unavailable"),
            ConnectionError("network down"),
            _extend_ok(INITIAL_EXPIRY + 3 * TTL),
        ]

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3)

        assert client.extend_reservation.call_count == 3


@pytest.mark.asyncio
class TestAsyncHeartbeatLeadEstimate:
    async def _run(
        self,
        lifecycle: AsyncCyclesLifecycle,
        beats: int,
        monkeypatch: pytest.MonkeyPatch,
        ctx: MagicMock | None = None,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        count = 0

        async def fake_sleep(s: float) -> None:
            nonlocal count
            count += 1
            if count > beats:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = lifecycle._start_heartbeat("rsv_1", TTL, ctx or _ctx())
        assert task is not None
        assert await task is None

    async def test_extends_only_when_lead_below_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)]

        await self._run(lifecycle, 5, monkeypatch)

        # Immediate first beat, then lead_min builds from 0: beats 1-3
        # extend, beat 4 skips at lead_min >= 1.5*grant, beat 5 extends.
        assert client.extend_reservation.await_count == 4

    async def test_permanent_code_stops_heartbeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.return_value = CyclesResponse.http_error(410, "gone")

        await self._run(lifecycle, 4, monkeypatch)

        assert client.extend_reservation.await_count == 1

    async def test_transient_failure_missing_expires_and_late_anchor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Covers the async warn-continue, +=ttl fallback, and late-anchor branches.
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(None),  # frame not yet anchored
            _extend_ok(700_000),  # late anchor
            _extend_ok(None),  # anchored: += ttl fallback
        ]

        await self._run(lifecycle, 4, monkeypatch, ctx=_ctx(None))

        assert client.extend_reservation.await_count == 4


class TestStreamingHeartbeatLeadEstimate:
    def test_sync_stream_lead_estimate_pattern(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)]
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        calls = {"n": 0}

        def wait(timeout: float | None = None) -> bool:
            calls["n"] += 1
            if calls["n"] > 5:
                return True
            clock.t += (timeout or 0.0) * 1000.0
            return False

        stream._heartbeat_stop.wait = wait  # type: ignore[method-assign]
        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        assert client.extend_reservation.call_count == 4

    def test_sync_stream_permanent_and_fallback_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Covers the sync-stream +=ttl fallback, late-anchor, transient-warn,
        # and permanent-stop branches in one deterministic run.
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),  # transient: warn, retry
            _extend_ok(None),  # no anchor yet: no-op
            _extend_ok(900_000),  # late anchor
            _extend_ok(None),  # anchored: += ttl fallback
            CyclesResponse.http_error(410, "gone"),  # permanent: stop
        ]
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx(None)
        calls = {"n": 0}

        def wait(timeout: float | None = None) -> bool:
            calls["n"] += 1
            if calls["n"] > 8:
                return True
            clock.t += (timeout or 0.0) * 1000.0
            return False

        stream._heartbeat_stop.wait = wait  # type: ignore[method-assign]
        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        # Stops at the 410 — remaining beats never call extend.
        assert client.extend_reservation.call_count == 5

    def test_sync_stream_field_mode_cycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Authoritative mode in the sync stream heartbeat: create field
        # drives the first delay; 503 and exception retries stay bounded.
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
            CyclesResponse.http_error(503, "unavailable"),
            ConnectionError("down"),
            _extend_ok(INITIAL_EXPIRY + 2 * TTL, remaining_ttl_ms=60_000),
        ]
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        stream._initial_remaining = 60_000
        calls = {"n": 0}
        timeouts: list[float] = []

        def wait(timeout: float | None = None) -> bool:
            timeouts.append(timeout or 0.0)
            calls["n"] += 1
            if calls["n"] > 4:
                return True
            clock.t += (timeout or 0.0) * 1000.0
            return False

        stream._heartbeat_stop.wait = wait  # type: ignore[method-assign]
        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        assert client.extend_reservation.call_count == 4
        assert timeouts[0] == 35.0
        assert timeouts[2] == 6.25
        assert timeouts[3] == 4.6875

    @pytest.mark.asyncio
    async def test_async_stream_field_mode_cycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
            CyclesResponse.http_error(503, "unavailable"),
            ConnectionError("down"),
            _extend_ok(INITIAL_EXPIRY + 2 * TTL, remaining_ttl_ms=60_000),
        ]
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        stream._initial_remaining = 60_000
        count = 0
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            nonlocal count
            sleeps.append(s)
            count += 1
            if count > 4:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 4
        assert sleeps[0] == 35.0
        assert sleeps[2] == 6.25
        assert sleeps[3] == 4.6875

    @pytest.mark.asyncio
    async def test_async_stream_lead_estimate_pattern(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.side_effect = [_extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)]
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        count = 0

        async def fake_sleep(s: float) -> None:
            nonlocal count
            count += 1
            if count > 5:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 4

    @pytest.mark.asyncio
    async def test_async_stream_permanent_and_fallback_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(None),  # no anchor yet: no-op
            _extend_ok(900_000),  # late anchor
            _extend_ok(None),  # anchored: += ttl fallback
            CyclesResponse.http_error(410, "gone"),
        ]
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx(None)
        count = 0

        async def fake_sleep(s: float) -> None:
            nonlocal count
            count += 1
            if count > 8:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 5


# ---------------------------------------------------------------------------
# Server-authoritative scheduling (remaining_ttl_ms, spec v0.1.25.16)
# ---------------------------------------------------------------------------


class TestAuthoritativeScheduling:
    # With the SDK's enforced httpx timeouts (connect 2s + read 5s + write
    # 5s), request_timeout_budget = 12000ms; at rtt 0: attempt_budget =
    # 12000, safety_margin = 1000, retry_reserve = 2×12000 + 1000 = 25000.
    RESERVE = 25_000.0

    def test_create_remaining_drives_first_beat_and_steady_cadence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # remaining=60000 → next_delay = 60000 − 25000 = 35000ms. Every
        # extend echoes the field, so the cadence holds at 35s and the
        # heuristic lead_min skip NEVER fires even though accumulated
        # fallback grants would trip it.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(
            INITIAL_EXPIRY + TTL,
            remaining_ttl_ms=60_000,
        )

        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=4,
            initial_remaining_ms=60_000,
        )

        assert client.extend_reservation.call_count == 4
        assert timeouts == [35.0, 35.0, 35.0, 35.0, 35.0]

    def test_lease_below_reserve_one_immediate_attempt_then_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 24h request capped to a 1s lease: the lease cannot hold the
        # 25s retry reserve → next_delay 0 → ONE immediate fresh attempt is
        # permitted; when its success also yields 0, the client MUST stop
        # and surface (spec zero-delay guard) rather than tight-loop a
        # maximum-lead server's extension budget away.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(
            INITIAL_EXPIRY + TTL,
            remaining_ttl_ms=1_000,
        )

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            timeouts = _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=4,
                ttl=86_400_000,
                initial_remaining_ms=1_000,
            )

        # Immediate first beat (streak 1), its success hits streak 2 → stop.
        assert timeouts[0] == 0.0
        assert client.extend_reservation.call_count == 1
        assert [r for r in caplog.records if "retry-safety budget" in r.message]

    def test_lead_clamp_server_with_field_no_warn_no_collapse(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A maximum-lead-clamping server that DOES emit remaining_ttl_ms:
        # expiry echoes elapsed (grant ≈ elapsed, the heuristic's worst
        # case) but the field carries the true 60s lead → authoritative arm
        # schedules cleanly and the clamp warning never fires.
        clock = FakeClock()
        lifecycle, client = _make_sync()

        def clamped_extend(rid: str, body: dict[str, Any]) -> CyclesResponse:
            return _extend_ok(INITIAL_EXPIRY + int(clock.t), remaining_ttl_ms=60_000)

        client.extend_reservation.side_effect = clamped_extend

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            timeouts = _run_sync_beats(
                lifecycle,
                clock,
                monkeypatch,
                beats=3,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 3
        assert timeouts == [35.0, 35.0, 35.0, 35.0]
        assert not [r for r in caplog.records if "clamp lease lead" in r.message]

    def test_field_disappearing_mid_flight_resumes_heuristic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Beat 1's response lacks the field → the v2.3+band heuristic takes
        # over seamlessly from its maintained bookkeeping.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + TTL),  # no field: fallback
            _extend_ok(INITIAL_EXPIRY + 2 * TTL),
        ]

        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=2,
            initial_remaining_ms=60_000,
        )

        assert client.extend_reservation.call_count == 2
        # First delay authoritative (35s); after the fieldless response the
        # normal-regime cadence (ttl/2 = 30s) applies.
        assert timeouts[0] == 35.0
        assert timeouts[1] == 30.0

    def test_transient_failure_recovery_window_shrinks_then_same_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 503 at the scheduled beat (t=35s): lead_est = 60000 − 35000 =
        # 25000; retry_window = 25000 − 12000 − 1000 = 12000 → retry after
        # min(30000, 25000/4, 12000) = 6250ms, with the SAME key.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(503, "unavailable"),
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
        ]

        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=2,
            initial_remaining_ms=60_000,
        )

        assert client.extend_reservation.call_count == 2
        assert timeouts[0] == 35.0
        assert timeouts[1] == 6.25
        bodies = [c.args[1] for c in client.extend_reservation.call_args_list]
        assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]

    def test_repeated_failures_stop_when_no_window_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Recovery repeats with the same key while the freshly recomputed
        # retry_window shrinks: 6250 → 4687.5 → 1062.5 → 0 (one immediate
        # retry) → no progress at 0 → MUST stop. Five attempts total.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(503, "down")

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            timeouts = _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=10,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 5
        assert timeouts == [35.0, 6.25, 4.6875, 1.0625, 0.0]
        assert [r for r in caplog.records if "no safe recovery window" in r.message]
        keys = {c.args[1]["idempotency_key"] for c in client.extend_reservation.call_args_list}
        assert len(keys) == 1  # every recovery reused the same key

    def test_ambiguous_2xx_is_not_applied_same_key_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A non-200 2xx in authoritative mode is ambiguous (spec): never
        # scheduled from — recovered with the SAME idempotency key.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.success(202, {"status": "ACTIVE", "remaining_ttl_ms": 60_000}),
            _extend_ok(INITIAL_EXPIRY + 2 * TTL, remaining_ttl_ms=60_000),
        ]

        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=2,
            initial_remaining_ms=60_000,
        )

        assert client.extend_reservation.call_count == 2
        assert timeouts[1] == 6.25
        bodies = [c.args[1] for c in client.extend_reservation.call_args_list]
        assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]

    def test_429_honored_only_within_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Retry-After 3s ≤ window 12000 → retried after exactly 3s, same key.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(429, "limited", headers={"retry-after": "3"}),
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
        ]

        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=2,
            initial_remaining_ms=60_000,
        )

        assert client.extend_reservation.call_count == 2
        assert timeouts[1] == 3.0
        bodies = [c.args[1] for c in client.extend_reservation.call_args_list]
        assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]

    def test_429_missing_or_oversized_retry_after_stops(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Retry-After exceeding the retry window (20s > 12000ms) must not be
        # re-invented earlier: stop and surface.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            429,
            "limited",
            headers={"retry-after": "20"},
        )

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=4,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 1
        assert [r for r in caplog.records if "no safe recovery window" in r.message]

    def test_other_4xx_stops_without_key_rotation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            400,
            "bad",
            body={"error": "INVALID_REQUEST", "message": "m", "request_id": "r"},
        )

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=4,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 1
        assert [r for r in caplog.records if "client error" in r.message]

    def test_unexpected_3xx_stops_without_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(302, "redirect")

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=4,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 1
        assert [r for r in caplog.records if "unexpected HTTP status" in r.message]

    @pytest.mark.asyncio
    async def test_async_field_mode_full_cycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Covers the async authoritative arms: initial field delay, 503
        # recovery, exception recovery with a shrinking window, success.
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
            CyclesResponse.http_error(503, "unavailable"),
            ConnectionError("down"),
            _extend_ok(INITIAL_EXPIRY + 2 * TTL, remaining_ttl_ms=60_000),
        ]
        count = 0
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            nonlocal count
            sleeps.append(s)
            count += 1
            if count > 4:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = lifecycle._start_heartbeat("rsv_1", TTL, _ctx(), 60_000)
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 4
        assert sleeps[0] == 35.0  # from the create field
        assert sleeps[2] == 6.25  # 503: window 12000, lead_est/4 = 6250
        assert sleeps[3] == 4.6875  # exception: window shrank to 5750

    def test_scheduler_edge_cases_direct(self) -> None:
        from runcycles.lifecycle import _AuthoritativeScheduler

        sched = _AuthoritativeScheduler(12_000.0)
        # No schema-valid response yet → lead estimate is 0 and any failure
        # has no recovery window.
        assert sched.lead_estimate_ms(1_000.0) == 0.0
        assert sched.on_transient_failure(1_000.0) is None
        # Establish a lead, then: missing and negative Retry-After stop.
        assert sched.on_valid_success(60_000, 0.0, 0.0) == 35_000.0
        assert sched.on_transient_failure(35_000.0, rate_limited=True, retry_after_ms=None) is None
        sched2 = _AuthoritativeScheduler(12_000.0)
        sched2.on_valid_success(60_000, 0.0, 0.0)
        assert sched2.on_transient_failure(35_000.0, rate_limited=True, retry_after_ms=-1) is None
        # Unknown/backward timing never fabricates lease lead.
        sched3 = _AuthoritativeScheduler(12_000.0)
        assert sched3.on_valid_success(60_000, -1.0, 0.0) == 0.0
        assert sched3.lead_estimate_ms(-1.0) == 0.0

    def test_sync_repeated_ambiguous_2xx_stops(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.success(202, {"status": "ACTIVE"})

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=10,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 5
        assert [r for r in caplog.records if "no safe recovery window" in r.message]

    def test_sync_repeated_exceptions_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = ConnectionError("down")

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            _run_sync_beats(
                lifecycle,
                FakeClock(),
                monkeypatch,
                beats=10,
                initial_remaining_ms=60_000,
            )

        assert client.extend_reservation.call_count == 5
        assert [r for r in caplog.records if "no safe recovery window" in r.message]

    def test_create_response_model_parses_remaining(self) -> None:
        from runcycles.models import ReservationCreateResponse

        body = {
            "decision": "ALLOW",
            "reservation_id": "rsv_1",
            "affected_scopes": ["tenant:acme"],
            "expires_at_ms": 1_000_000,
            "remaining_ttl_ms": 10_000,
        }
        parsed = ReservationCreateResponse.model_validate(body)
        assert parsed.remaining_ttl_ms == 10_000
        body.pop("remaining_ttl_ms")
        assert ReservationCreateResponse.model_validate(body).remaining_ttl_ms is None


# Scenario matrix for the authoritative stop/recovery arms, exercised
# against every loop variant (async lifecycle, sync stream, async stream).
# Each entry: (responses factory, initial_remaining, expected extend calls).
#   - repeated 503 / ambiguous 202 / exceptions: recovery windows shrink
#     6250 → 4687.5 → 1062.5 → 0 → no-progress stop = 5 attempts;
#   - 400: unrecoverable client error, stop after 1;
#   - tiny lease: zero-delay guard, 1 immediate attempt then stop;
#   - 429 with Retry-After 3s inside the window: honored, then success.
_STOP_MATRIX = [
    ("s503", lambda: CyclesResponse.http_error(503, "down"), 60_000, 5),
    ("s202", lambda: CyclesResponse.success(202, {"status": "ACTIVE"}), 60_000, 5),
    (
        "s400",
        lambda: CyclesResponse.http_error(
            400,
            "bad",
            body={"error": "INVALID_REQUEST", "message": "m", "request_id": "r"},
        ),
        60_000,
        1,
    ),
    ("s302", lambda: CyclesResponse.http_error(302, "redirect"), 60_000, 1),
    ("szero", lambda: _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=1_000), 1_000, 1),
    ("sexc", lambda: ConnectionError("down"), 60_000, 5),
]


def _matrix_side_effect(factory: Any) -> Any:
    def _effect(*args: Any, **kwargs: Any) -> CyclesResponse:
        result = factory()
        if isinstance(result, Exception):
            raise result
        return result

    return _effect


class TestAuthoritativeStopMatrix:
    @pytest.mark.parametrize(
        ("name", "factory", "initial", "expected"),
        _STOP_MATRIX,
        ids=[m[0] for m in _STOP_MATRIX],
    )
    @pytest.mark.asyncio
    async def test_async_lifecycle_arms(
        self,
        name: str,
        factory: Any,
        initial: int,
        expected: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = _matrix_side_effect(factory)
        count = 0

        async def fake_sleep(s: float) -> None:
            nonlocal count
            count += 1
            if count > 10:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = lifecycle._start_heartbeat("rsv_1", TTL, _ctx(), initial)
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == expected

    @pytest.mark.asyncio
    async def test_async_lifecycle_429_honored(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(429, "limited", headers={"retry-after": "3"}),
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
        ]
        count = 0
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            nonlocal count
            sleeps.append(s)
            count += 1
            if count > 2:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = lifecycle._start_heartbeat("rsv_1", TTL, _ctx(), 60_000)
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 2
        assert sleeps[1] == 3.0

    def _make_stream(self) -> tuple[StreamReservation, MagicMock]:
        client = MagicMock()
        client._config = _config()
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        return stream, client

    @pytest.mark.parametrize(
        ("name", "factory", "initial", "expected"),
        _STOP_MATRIX,
        ids=[m[0] for m in _STOP_MATRIX],
    )
    def test_sync_stream_arms(
        self,
        name: str,
        factory: Any,
        initial: int,
        expected: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        stream, client = self._make_stream()
        client.extend_reservation.side_effect = _matrix_side_effect(factory)
        stream._initial_remaining = initial
        calls = {"n": 0}

        def wait(timeout: float | None = None) -> bool:
            calls["n"] += 1
            if calls["n"] > 10:
                return True
            clock.t += (timeout or 0.0) * 1000.0
            return False

        stream._heartbeat_stop.wait = wait  # type: ignore[method-assign]
        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        assert client.extend_reservation.call_count == expected

    def test_sync_stream_429_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        stream, client = self._make_stream()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(429, "limited", headers={"retry-after": "3"}),
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
        ]
        stream._initial_remaining = 60_000
        timeouts: list[float] = []
        calls = {"n": 0}

        def wait(timeout: float | None = None) -> bool:
            timeouts.append(timeout or 0.0)
            calls["n"] += 1
            if calls["n"] > 2:
                return True
            clock.t += (timeout or 0.0) * 1000.0
            return False

        stream._heartbeat_stop.wait = wait  # type: ignore[method-assign]
        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        assert client.extend_reservation.call_count == 2
        assert timeouts[1] == 3.0

    def _make_async_stream(self) -> tuple[AsyncStreamReservation, AsyncMock]:
        client = AsyncMock()
        client._config = _config()
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=TTL,
        )
        stream._reservation_id = "rsv_1"
        stream._ctx = _ctx()
        return stream, client

    @pytest.mark.parametrize(
        ("name", "factory", "initial", "expected"),
        _STOP_MATRIX,
        ids=[m[0] for m in _STOP_MATRIX],
    )
    @pytest.mark.asyncio
    async def test_async_stream_arms(
        self,
        name: str,
        factory: Any,
        initial: int,
        expected: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        stream, client = self._make_async_stream()
        client.extend_reservation.side_effect = _matrix_side_effect(factory)
        stream._initial_remaining = initial
        count = 0

        async def fake_sleep(s: float) -> None:
            nonlocal count
            count += 1
            if count > 10:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == expected

    @pytest.mark.asyncio
    async def test_async_stream_429_honored(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("runcycles.lifecycle._now_mono_ms", clock.now)
        stream, client = self._make_async_stream()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(429, "limited", headers={"retry-after": "3"}),
            _extend_ok(INITIAL_EXPIRY + TTL, remaining_ttl_ms=60_000),
        ]
        stream._initial_remaining = 60_000
        count = 0
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            nonlocal count
            sleeps.append(s)
            count += 1
            if count > 2:
                raise asyncio.CancelledError
            clock.t += s * 1000.0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        assert await task is None

        assert client.extend_reservation.await_count == 2
        assert sleeps[1] == 3.0


# ---------------------------------------------------------------------------
# Date header accessor (kept as a general response accessor; the heartbeat
# no longer consumes it — RFC 9110 §6.6.1 makes it a different clock).
# ---------------------------------------------------------------------------


class TestServerDateAccessor:
    def test_server_date_ms_parses_http_date(self) -> None:
        response = CyclesResponse.success(
            200,
            {},
            headers={"date": "Mon, 27 Jul 2026 12:00:00 GMT"},
        )
        assert response.server_date_ms == 1785153600000

    def test_server_date_ms_absent_or_garbage(self) -> None:
        assert CyclesResponse.success(200, {}).server_date_ms is None
        assert CyclesResponse.success(200, {}, headers={"date": "not a date"}).server_date_ms is None


# ---------------------------------------------------------------------------
# actual_source marker
# ---------------------------------------------------------------------------


def _cfg(**kwargs: Any) -> DecoratorConfig:
    defaults: dict[str, Any] = {"estimate": 1000, "tenant": "acme", "ttl_ms": 60_000}
    defaults.update(kwargs)
    return DecoratorConfig(**defaults)


class TestFirstBeatAndRegimes:
    def test_first_beat_is_immediate_even_for_huge_ttl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A 24h request silently capped to a small lease by tenant policy
        # must still survive: only a zero first delay guarantees the first
        # extension lands before ANY possible capped lease expires.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)
        timeouts = _run_sync_beats(
            lifecycle,
            FakeClock(),
            monkeypatch,
            beats=1,
            ttl=86_400_000,
        )
        assert timeouts[0] == 0.0

    def test_first_beat_failure_does_not_hot_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A transient failure on the primed (delay-0) beat must back off to
        # the held cadence, not spin at 0ms against a down server.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(503, "down")
        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=2)
        assert timeouts[0] == 0.0
        assert timeouts[1] == 30.0
        assert client.extend_reservation.call_count == 2

    def test_lead_clamp_regime_holds_cadence_and_warns_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Maximum-lead clamping: every grant merely mirrors elapsed time
        # (expires_at stays ~now + cap), so grant/2 cadence would collapse
        # to the floor and burn max_extensions in seconds. The loop must
        # hold the bounded cadence and warn exactly once.
        clock = FakeClock()
        lifecycle, client = _make_sync()

        def clamped_extend(rid: str, body: dict[str, Any]) -> CyclesResponse:
            return _extend_ok(INITIAL_EXPIRY + int(clock.t))

        client.extend_reservation.side_effect = clamped_extend

        with caplog.at_level(logging.WARNING, logger="runcycles.lifecycle"):
            timeouts = _run_sync_beats(lifecycle, clock, monkeypatch, beats=3)

        # Beat 1 measures grant 0 (prime), beats 2-3 measure grant ==
        # elapsed: all extend, cadence never tightens below the held delay.
        assert client.extend_reservation.call_count == 3
        assert timeouts == [0.0, 30.0, 30.0, 30.0]
        clamp_warnings = [r for r in caplog.records if "clamp lease lead" in r.message]
        assert len(clamp_warnings) == 1


class TestActualSourceMarker:
    def test_fallback_commit_carries_marker(self) -> None:
        lifecycle, client = _make_sync()
        client.create_reservation.return_value = _allow_response()
        client.commit_reservation.return_value = _commit_success()

        lifecycle.execute(lambda: "result", (), {}, _cfg())  # no actual expression

        body = client.commit_reservation.call_args.args[1]
        assert body["metadata"]["actual_source"] == "estimate"

    def test_measured_commit_has_no_marker(self) -> None:
        lifecycle, client = _make_sync()
        client.create_reservation.return_value = _allow_response()
        client.commit_reservation.return_value = _commit_success()

        lifecycle.execute(lambda: "result", (), {}, _cfg(actual=lambda _r: 900))

        body = client.commit_reservation.call_args.args[1]
        assert "metadata" not in body or "actual_source" not in body.get("metadata", {})

    @pytest.mark.asyncio
    async def test_async_fallback_commit_carries_marker(self) -> None:
        lifecycle, client = _make_async()
        client.create_reservation.return_value = _allow_response()
        client.commit_reservation.return_value = _commit_success()

        async def fn() -> str:
            return "result"

        await lifecycle.execute(fn, (), {}, _cfg())

        body = client.commit_reservation.call_args.args[1]
        assert body["metadata"]["actual_source"] == "estimate"

    def test_stream_fallback_carries_marker_and_measured_does_not(self) -> None:
        client = MagicMock()
        client._config = _config()
        client.create_reservation.return_value = _allow_response()
        client.commit_reservation.return_value = _commit_success()
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        with stream:
            pass  # no actual cost recorded → estimate fallback
        body = client.commit_reservation.call_args.args[1]
        assert body.metadata["actual_source"] == "estimate"

        client2 = MagicMock()
        client2._config = _config()
        client2.create_reservation.return_value = _allow_response()
        client2.commit_reservation.return_value = _commit_success()
        stream2 = StreamReservation(
            client2,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        with stream2:
            stream2.usage.set_actual_cost(700)
        body2 = client2.commit_reservation.call_args.args[1]
        assert body2.metadata is None or "actual_source" not in body2.metadata

    @pytest.mark.asyncio
    async def test_async_stream_fallback_carries_marker(self) -> None:
        client = AsyncMock()
        client._config = _config()
        client.create_reservation.return_value = _allow_response()
        client.commit_reservation.return_value = _commit_success()
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        async with stream:
            pass
        body = client.commit_reservation.call_args.args[1]
        assert body.metadata["actual_source"] == "estimate"
