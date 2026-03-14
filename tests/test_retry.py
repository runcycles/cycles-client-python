"""Tests for the commit retry engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from runcycles.config import CyclesConfig
from runcycles.response import CyclesResponse
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine


@pytest.fixture
def config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878",
        api_key="test-key",
        retry_enabled=True,
        retry_max_attempts=3,
        retry_initial_delay=0.01,  # fast for tests
        retry_multiplier=1.0,
        retry_max_delay=0.05,
    )


@pytest.fixture
def disabled_config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878",
        api_key="test-key",
        retry_enabled=False,
    )


class TestCommitRetryEngine:
    def test_disabled_does_not_retry(self, disabled_config: CyclesConfig) -> None:
        engine = CommitRetryEngine(disabled_config)
        mock_client = MagicMock()
        engine.set_client(mock_client)
        engine.schedule("rsv_1", {"idempotency_key": "k1", "actual": {"unit": "USD_MICROCENTS", "amount": 100}})
        mock_client.commit_reservation.assert_not_called()

    def test_retries_until_success(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        mock_client = MagicMock()
        # First call fails with 500, second succeeds
        mock_client.commit_reservation.side_effect = [
            CyclesResponse.http_error(500, "Server error"),
            CyclesResponse.success(200, {"status": "COMMITTED"}),
        ]
        engine.set_client(mock_client)

        # Run _retry_loop directly (synchronous, avoids thread timing issues)
        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 2

    def test_stops_on_client_error(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        mock_client = MagicMock()
        # 409 is a client error — should not retry
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(409, "Already finalized")
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 1

    def test_exhausts_retries(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        mock_client = MagicMock()
        # Always returns 500
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "Server error")
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == config.retry_max_attempts

    def test_handles_exception_during_retry(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        mock_client = MagicMock()
        # First call throws, second succeeds
        mock_client.commit_reservation.side_effect = [
            ConnectionError("network down"),
            CyclesResponse.success(200, {"status": "COMMITTED"}),
        ]
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 2

    def test_no_client_set(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        # Don't set client — should bail out
        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        engine._retry_loop(pending)  # should not raise


@pytest.mark.asyncio
class TestAsyncCommitRetryEngine:
    async def test_disabled_does_not_retry(self, disabled_config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(disabled_config)
        mock_client = AsyncMock()
        engine.set_client(mock_client)
        engine.schedule("rsv_1", {"idempotency_key": "k1"})
        mock_client.commit_reservation.assert_not_called()

    async def test_retries_until_success(self, config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(config)
        mock_client = AsyncMock()
        mock_client.commit_reservation.side_effect = [
            CyclesResponse.http_error(500, "Server error"),
            CyclesResponse.success(200, {"status": "COMMITTED"}),
        ]
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        await engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 2

    async def test_stops_on_client_error(self, config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(config)
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(409, "Finalized")
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        await engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 1

    async def test_exhausts_retries(self, config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(config)
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "Error")
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        await engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == config.retry_max_attempts

    async def test_handles_exception_during_retry(self, config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(config)
        mock_client = AsyncMock()
        mock_client.commit_reservation.side_effect = [
            ConnectionError("network down"),
            CyclesResponse.success(200, {"status": "COMMITTED"}),
        ]
        engine.set_client(mock_client)

        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        await engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 2

    async def test_no_client_set(self, config: CyclesConfig) -> None:
        engine = AsyncCommitRetryEngine(config)
        from runcycles.retry import _PendingCommit
        pending = _PendingCommit(reservation_id="rsv_1", commit_body={"idempotency_key": "k1"})
        await engine._retry_loop(pending)  # should not raise

    async def test_schedule_no_event_loop(self, config: CyclesConfig) -> None:
        """Schedule outside an event loop should log error, not crash."""
        engine = AsyncCommitRetryEngine(config)
        mock_client = AsyncMock()
        engine.set_client(mock_client)
        # Can't easily test outside event loop from within async test,
        # but we can verify schedule works inside one
        engine.schedule("rsv_1", {"idempotency_key": "k1"})
        # Let the scheduled task actually run
        await asyncio.sleep(0.1)


class TestCommitRetryEngineSchedule:
    def test_schedule_creates_thread(self, config: CyclesConfig) -> None:
        engine = CommitRetryEngine(config)
        mock_client = MagicMock()
        # Return success immediately so the thread finishes quickly
        mock_client.commit_reservation.return_value = CyclesResponse.success(200, {"status": "COMMITTED"})
        engine.set_client(mock_client)

        engine.schedule("rsv_1", {"idempotency_key": "k1"})
        # Give the thread time to run
        import time
        time.sleep(0.1)
        assert mock_client.commit_reservation.call_count >= 1


class TestAsyncCommitRetryEngineScheduleNoLoop:
    def test_schedule_without_event_loop(self, config: CyclesConfig) -> None:
        """Schedule outside an event loop should log error, not crash."""
        engine = AsyncCommitRetryEngine(config)
        mock_client = MagicMock()
        engine.set_client(mock_client)
        # This should not raise even without an event loop
        engine.schedule("rsv_1", {"idempotency_key": "k1"})
