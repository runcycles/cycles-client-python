"""Deterministic tests for alternate-beat heartbeat extension and the
``actual_source: estimate`` audit marker."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from runcycles.config import CyclesConfig
from runcycles.lifecycle import AsyncCyclesLifecycle, CyclesLifecycle, DecoratorConfig
from runcycles.models import Action, Amount, Subject, Unit
from runcycles.response import CyclesResponse
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine
from runcycles.streaming import AsyncStreamReservation, StreamReservation


def _config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878", api_key="test-key", tenant="acme",
        retry_enabled=False,
    )


def _extend_ok() -> CyclesResponse:
    return CyclesResponse.success(200, {"status": "ACTIVE", "expires_at_ms": 9999999999})


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


def _run_sync_beats(lifecycle: CyclesLifecycle, beats: int) -> None:
    """Drive the sync heartbeat loop for exactly `beats` iterations."""
    stop = threading.Event()
    stop.wait = MagicMock(side_effect=[False] * beats + [True])  # type: ignore[method-assign]
    thread = lifecycle._start_heartbeat("rsv_1", 60_000, MagicMock(), stop)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


class TestSyncHeartbeatAlternateBeat:
    def test_extends_on_first_and_alternate_beats(self) -> None:
        # extend_by_ms is relative to CURRENT expiry: extending every beat
        # at ttl/2 cadence drifts expiry outward. Expected: beats 1 and 3
        # extend, beats 2 and 4 skip.
        lifecycle, client = _make_sync()
        client.extend_reservation.return_value = _extend_ok()

        _run_sync_beats(lifecycle, 4)

        assert client.extend_reservation.call_count == 2
        body = client.extend_reservation.call_args.args[1]
        assert body["extend_by_ms"] == 60_000  # amount unchanged; cadence halved

    def test_failed_extend_retries_next_beat(self) -> None:
        lifecycle, client = _make_sync()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),  # beat 1: fail
            _extend_ok(),                            # beat 2: retry, success
            _extend_ok(),                            # beat 4: alternate resumes
        ]

        _run_sync_beats(lifecycle, 4)

        # beat 3 skipped after the beat-2 success
        assert client.extend_reservation.call_count == 3


@pytest.mark.asyncio
class TestAsyncHeartbeatAlternateBeat:
    async def _run_async_beats(
        self, lifecycle: AsyncCyclesLifecycle, beats: int, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        count = 0

        async def fake_sleep(_s: float) -> None:
            nonlocal count
            count += 1
            if count > beats:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = lifecycle._start_heartbeat("rsv_1", 60_000, MagicMock())
        assert task is not None
        await task  # heartbeat catches CancelledError and returns

    async def test_extends_on_first_and_alternate_beats(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.return_value = _extend_ok()

        await self._run_async_beats(lifecycle, 4, monkeypatch)

        assert client.extend_reservation.await_count == 2

    async def test_failed_extend_retries_next_beat(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle, client = _make_async()
        client.extend_reservation.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _extend_ok(),
            _extend_ok(),
        ]

        await self._run_async_beats(lifecycle, 4, monkeypatch)

        assert client.extend_reservation.await_count == 3


class TestStreamingHeartbeatAlternateBeat:
    def test_sync_stream_extends_alternate_beats(self) -> None:
        client = MagicMock()
        client._config = _config()
        client.extend_reservation.return_value = _extend_ok()
        stream = StreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        stream._reservation_id = "rsv_1"
        stream._heartbeat_stop.wait = MagicMock(  # type: ignore[method-assign]
            side_effect=[False] * 4 + [True],
        )

        thread = stream._start_heartbeat()
        assert thread is not None
        thread.join(timeout=5)

        assert client.extend_reservation.call_count == 2

    @pytest.mark.asyncio
    async def test_async_stream_extends_alternate_beats(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = AsyncMock()
        client._config = _config()
        client.extend_reservation.return_value = _extend_ok()
        stream = AsyncStreamReservation(
            client,
            subject=Subject(tenant="acme"),
            action=Action(kind="k", name="n"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        stream._reservation_id = "rsv_1"

        count = 0

        async def fake_sleep(_s: float) -> None:
            nonlocal count
            count += 1
            if count > 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        task = stream._start_heartbeat()
        assert task is not None
        await task

        assert client.extend_reservation.await_count == 2


# ---------------------------------------------------------------------------
# actual_source marker
# ---------------------------------------------------------------------------


def _cfg(**kwargs: Any) -> DecoratorConfig:
    defaults: dict[str, Any] = {"estimate": 1000, "tenant": "acme", "ttl_ms": 60_000}
    defaults.update(kwargs)
    return DecoratorConfig(**defaults)


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
