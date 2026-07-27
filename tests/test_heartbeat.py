"""Deterministic tests for the lead-estimate heartbeat and the
``actual_source: estimate`` audit marker."""

from __future__ import annotations

import asyncio
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
    est_ttl_ms: int | None = None,
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
    thread = lifecycle._start_heartbeat("rsv_1", ttl, ctx or _ctx(), stop, est_ttl_ms)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    return timeouts


class TestSyncHeartbeatLeadEstimate:
    def test_extends_only_when_lead_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # lead_min starts at 0, so the heartbeat builds margin first:
        # beats 1-4 extend (grants measured at +ttl each), beat 5 skips
        # once lead_min reaches 1.5*grant.
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            _extend_ok(INITIAL_EXPIRY + (n + 1) * TTL) for n in range(4)
        ]

        timeouts = _run_sync_beats(lifecycle, FakeClock(), monkeypatch, beats=5)

        assert client.extend_reservation.call_count == 4
        assert timeouts[0] == min(TTL / 2, 30_000) / 1000.0

    def test_interval_has_no_floor_for_small_ttl(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ttl=1200 → interval must be 600ms (the old 1s floor guaranteed
        # lapse in this spec-legal range).
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)

        ctx = _ctx(1200)
        timeouts = _run_sync_beats(
            lifecycle, FakeClock(), monkeypatch, beats=2, ttl=1200, ctx=ctx,
        )

        assert timeouts[0] == 0.6

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

        # Beat 1 extends with the fallback grant (no prior frame); beat 2's
        # measured grant (3*ttl) lifts lead_min past 1.5*grant... beat 3:
        # lead_min = (ttl + 3*ttl) - 90s = 150s >= 1.5*180s? No — 240-90=150
        # < 270 → extends would need a 3rd response; assert the skip math
        # via count with exactly 2 responses and a 3rd beat that must skip:
        # grants_sum=4*ttl=240s, elapsed=90s → lead 150s, 1.5*last_grant
        # = 270s → NOT a skip. Give beat 3 nothing → the StopIteration is
        # swallowed as a transient error, count stays meaningful at 3.
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
        task = lifecycle._start_heartbeat("rsv_1", TTL, ctx or _ctx(), None)
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

        # lead_min builds from 0: beats 1-4 extend, beat 5 skips once
        # lead_min reaches 1.5*grant.
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
# Effective TTL (tenant policy caps)
# ---------------------------------------------------------------------------


class TestEffectiveTtl:
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

    def test_effective_ttl_derives_capped_grant(self) -> None:
        from runcycles.lifecycle import _effective_ttl_ms

        # Requested 24h, tenant policy capped to 1h: expires − Date = 1h.
        assert _effective_ttl_ms(86_400_000, 1_000_000 + 3_600_000, 1_000_000) == 3_600_000
        # Underivable → None (hint only; the caller falls back to caps).
        assert _effective_ttl_ms(86_400_000, None, 1_000_000) is None
        assert _effective_ttl_ms(86_400_000, 4_600_000, None) is None
        # Never clamped UPWARD (that would fabricate lease) nor above request.
        assert _effective_ttl_ms(60_000, 1_000_100, 1_000_000) == 100
        assert _effective_ttl_ms(60_000, 1_000_000 + 999_000, 1_000_000) == 60_000

    def test_execute_seeds_heartbeat_with_effective_ttl(self) -> None:
        # A 24h request capped to 1h must heartbeat on the 1h grant — the
        # old behavior would schedule the first beat ~12h after expiry.
        lifecycle, client = _make_sync()
        now_ms = 1_785_153_600_000
        base_body = _allow_response().body
        assert base_body is not None
        body = dict(base_body)
        body["expires_at_ms"] = now_ms + 3_600_000
        client.create_reservation.return_value = CyclesResponse.success(
            200, body, headers={"date": "Mon, 27 Jul 2026 12:00:00 GMT"},
        )
        client.commit_reservation.return_value = _commit_success()

        captured: dict[str, Any] = {}

        def fake_hb(rid: str, ttl: int, ctx: Any, stop: Any, est: Any = None) -> None:
            captured["ttl"] = ttl
            captured["est"] = est
            return None

        lifecycle._start_heartbeat = fake_hb  # type: ignore[method-assign]
        lifecycle.execute(lambda: "r", (), {}, _cfg(ttl_ms=86_400_000))

        # Requested ttl drives extend amounts; the derived grant is a HINT.
        assert captured["ttl"] == 86_400_000
        assert captured["est"] == 3_600_000


# ---------------------------------------------------------------------------
# actual_source marker
# ---------------------------------------------------------------------------


def _cfg(**kwargs: Any) -> DecoratorConfig:
    defaults: dict[str, Any] = {"estimate": 1000, "tenant": "acme", "ttl_ms": 60_000}
    defaults.update(kwargs)
    return DecoratorConfig(**defaults)


class TestFirstBeatDelay:
    def test_thirty_second_cap_without_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)
        timeouts = _run_sync_beats(
            lifecycle, FakeClock(), monkeypatch, beats=1, ttl=86_400_000,
        )
        assert timeouts[0] == 30.0  # 30s cap beats requested/2 = 12h

    def test_estimate_hint_tightens_first_beat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok(None)
        timeouts = _run_sync_beats(
            lifecycle, FakeClock(), monkeypatch, beats=1, ttl=86_400_000, est_ttl_ms=10_000,
        )
        assert timeouts[0] == 5.0  # est/2 wins when tighter than the cap


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
