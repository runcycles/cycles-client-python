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

import runcycles.lifecycle as lifecycle_mod
from runcycles.config import CyclesConfig
from runcycles.lifecycle import AsyncCyclesLifecycle, CyclesLifecycle, DecoratorConfig
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
        base_url="http://localhost:7878", api_key="test-key", tenant="acme",
        retry_enabled=False,
    )


def _extend_ok(expires_at_ms: int | None) -> CyclesResponse:
    body: dict[str, Any] = {"status": "ACTIVE"}
    if expires_at_ms is not None:
        body["expires_at_ms"] = expires_at_ms
    return CyclesResponse.success(200, body)


def _allow_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "ALLOW",
        "reservation_id": "rsv_test",
        "expires_at_ms": int(time.time() * 1000) + 600_000,
        "affected_scopes": ["tenant:acme"],
        "scope_path": "tenant:acme",
        "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


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
) -> list[float]:
    """Drive the sync heartbeat for `beats` iterations, advancing the fake
    clock by the beat interval on every wait. Returns the wait timeouts."""
    monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
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
    thread = lifecycle._start_heartbeat("rsv_1", ttl, ctx or _ctx(), stop)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    return timeouts


class TestSyncHeartbeatLeadEstimate:
    def test_extends_only_when_lead_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # lead_min starts at 0 and the first beat fires IMMEDIATELY: beats
        # 1-3 extend (grants measured at +ttl each), beat 4 skips once
        # lead_min reaches 1.5*grant (180k-90k=90k), beat 5 extends again.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)
        ]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=5)

        assert client.extend_reservation.call_count == 4
        assert timeouts[0] == 0.0
        assert timeouts[1] == min(TTL / 2, 30_000) / 1000.0

    def test_interval_has_no_floor_for_small_ttl(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ttl=1200 → after the immediate first beat, cadence must be 600ms
        # (the old 1s floor guaranteed lapse in this spec-legal range).
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)

        ctx = _ctx(1200)
        timeouts = _run_sync_beats(
            lifecycle, FakeClock(), monkeypatch, beats=2, ttl=1200, ctx=ctx,
        )

        assert timeouts[0] == 0.0
        assert timeouts[1] == 0.6

    def test_failed_extend_retries_with_same_idempotency_key(
        self, monkeypatch: pytest.MonkeyPatch,
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
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            409, "capped",
            body={"error": "MAX_EXTENSIONS_EXCEEDED", "message": "m", "request_id": "r"},
        )

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=4)

        # One doomed call, then the loop self-terminates — no retry spam.
        assert client.extend_reservation.call_count == 1

    def test_clamped_grants_extend_every_beat(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Server clamps each grant to ttl/4: the lead estimate sees the
        # small grants (authoritative expires_at) and keeps extending.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * (TTL // 4)) for n in range(3)
        ]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=3)

        assert client.extend_reservation.call_count == 3
        # Cadence re-derived from the MEASURED grant (ttl/4 → beat at ttl/8).
        assert timeouts[1] == (TTL / 4 / 2) / 1000.0

    def test_grant_clamp_misclassification_after_skip_is_transient(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # After a skip, the next measured grant arrives across a doubled
        # gap, so grant ≈ elapsed and the beat lands in the lead-clamp arm
        # once. The classifier's lower band (0.75×elapsed) must make that
        # non-sticky: at the held cadence the ratio falls to ~0.5, the
        # regime reads normal again, and cadence re-tightens — without the
        # band the hold sticks and a ttl/4-grant lease decays to a lapse.
        lifecycle, client = _make_sync()
        grant = TTL // 4  # 15000 → cadence ttl/8 = 7500ms
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * grant) for n in range(6)
        ]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=7)

        # b1-b3 extend @7.5s, b4 skips (lead 22.5k ≥ 1.5×15k), b5 extends
        # across the doubled gap (misclassified → one 30s hold), b6
        # re-tightens to 7.5s, b7 extends on cadence.
        assert client.extend_reservation.call_count == 6
        assert timeouts == [0.0, 7.5, 7.5, 7.5, 7.5, 30.0, 7.5, 7.5]

    def test_missing_expires_in_response_falls_back_to_plus_ttl(
        self, monkeypatch: pytest.MonkeyPatch,
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
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(500_000),            # beat 1: fallback grant, sets frame
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
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = CyclesResponse.http_error(
            409, "closed",
            body={"error": "TENANT_CLOSED", "message": "m", "request_id": "r"},
        )

        _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=4)

        assert client.extend_reservation.call_count == 1

    def test_transient_failure_then_recovery(
        self, monkeypatch: pytest.MonkeyPatch,
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
        monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
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
        await task

    async def test_extends_only_when_lead_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)
        ]

        await self._run(lifecycle, 5, monkeypatch)

        # Immediate first beat, then lead_min builds from 0: beats 1-3
        # extend, beat 4 skips at lead_min >= 1.5*grant, beat 5 extends.
        assert client.extend_reservation.await_count == 4

    async def test_permanent_code_stops_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.return_value = CyclesResponse.http_error(410, "gone")

        await self._run(lifecycle, 4, monkeypatch)

        assert client.extend_reservation.await_count == 1

    async def test_transient_failure_missing_expires_and_late_anchor(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Covers the async warn-continue, +=ttl fallback, and late-anchor branches.
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(None),        # frame not yet anchored
            _extend_ok(700_000),     # late anchor
            _extend_ok(None),        # anchored: += ttl fallback
        ]

        await self._run(lifecycle, 4, monkeypatch, ctx=_ctx(None))

        assert client.extend_reservation.await_count == 4


class TestStreamingHeartbeatLeadEstimate:
    def test_sync_stream_lead_estimate_pattern(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)
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
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Covers the sync-stream +=ttl fallback, late-anchor, transient-warn,
        # and permanent-stop branches in one deterministic run.
        clock = FakeClock()
        monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),      # transient: warn, retry
            _extend_ok(None),                            # no anchor yet: no-op
            _extend_ok(900_000),                         # late anchor
            _extend_ok(None),                            # anchored: += ttl fallback
            CyclesResponse.http_error(410, "gone"),      # permanent: stop
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

    @pytest.mark.asyncio
    async def test_async_stream_lead_estimate_pattern(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)
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
        await task

        assert client.extend_reservation.await_count == 4

    @pytest.mark.asyncio
    async def test_async_stream_permanent_and_fallback_branches(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr(lifecycle_mod, "_now_mono_ms", clock.now)
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(None),        # no anchor yet: no-op
            _extend_ok(900_000),     # late anchor
            _extend_ok(None),        # anchored: += ttl fallback
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
        await task

        assert client.extend_reservation.await_count == 5


# ---------------------------------------------------------------------------
# Date header accessor (kept as a general response accessor; the heartbeat
# no longer consumes it — RFC 9110 §6.6.1 makes it a different clock).
# ---------------------------------------------------------------------------


class TestServerDateAccessor:
    def test_server_date_ms_parses_http_date(self) -> None:
        response = CyclesResponse.success(
            200, {}, headers={"date": "Mon, 27 Jul 2026 12:00:00 GMT"},
        )
        assert response.server_date_ms == 1785153600000

    def test_server_date_ms_absent_or_garbage(self) -> None:
        assert CyclesResponse.success(200, {}).server_date_ms is None
        assert (
            CyclesResponse.success(200, {}, headers={"date": "not a date"}).server_date_ms
            is None
        )


# ---------------------------------------------------------------------------
# actual_source marker
# ---------------------------------------------------------------------------


def _cfg(**kwargs: Any) -> DecoratorConfig:
    defaults: dict[str, Any] = {"estimate": 1000, "tenant": "acme", "ttl_ms": 60_000}
    defaults.update(kwargs)
    return DecoratorConfig(**defaults)


class TestFirstBeatAndRegimes:
    def test_first_beat_is_immediate_even_for_huge_ttl(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A 24h request silently capped to a small lease by tenant policy
        # must still survive: only a zero first delay guarantees the first
        # extension lands before ANY possible capped lease expires.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)
        timeouts = _run_sync_beats(
            lifecycle, FakeClock(), monkeypatch, beats=1, ttl=86_400_000,
        )
        assert timeouts[0] == 0.0

    def test_first_beat_failure_does_not_hot_loop(
        self, monkeypatch: pytest.MonkeyPatch,
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
        assert body["metadata"]["actual_source"] == "estimate"

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
        assert "metadata" not in body2 or "actual_source" not in body2.get("metadata", {})

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
        assert body["metadata"]["actual_source"] == "estimate"
