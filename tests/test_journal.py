"""Tests for the durable commit journal, retry-engine durability, and event fallback."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from runcycles.config import CyclesConfig
from runcycles.journal import (
    CommitJournal,
    PendingCommitRecord,
    _safe_filename,
    auth_fingerprint,
    default_journal_dir,
)
from runcycles.lifecycle import (
    AsyncCyclesLifecycle,
    CyclesLifecycle,
    DecoratorConfig,
    _build_event_fallback_body,
)
from runcycles.response import CyclesResponse
from runcycles.retry import (
    AsyncCommitRetryEngine,
    CommitRetryEngine,
    _extract_error_code,
    _PendingCommit,
)

BASE_URL = "http://localhost:7878"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, **overrides: Any) -> CyclesConfig:
    defaults: dict[str, Any] = dict(
        base_url=BASE_URL,
        api_key="test-key",
        retry_enabled=True,
        retry_max_attempts=3,
        retry_initial_delay=0.001,
        retry_multiplier=1.0,
        retry_max_delay=0.005,
        retry_flush_timeout=5.0,
        journal_enabled=True,
        journal_dir=str(tmp_path / "journal"),
    )
    defaults.update(overrides)
    return CyclesConfig(**defaults)


def _commit_body() -> dict[str, Any]:
    return {"idempotency_key": "ck-1", "actual": {"unit": "USD_MICROCENTS", "amount": 100}}


def _event_body() -> dict[str, Any]:
    return {
        "idempotency_key": "ck-1",
        "subject": {"tenant": "acme"},
        "action": {"kind": "llm.completion", "name": "gpt"},
        "actual": {"unit": "USD_MICROCENTS", "amount": 100},
    }


def _expired_response() -> CyclesResponse:
    return CyclesResponse.http_error(
        410, "Expired",
        body={"error": "RESERVATION_EXPIRED", "message": "Expired", "request_id": "r1"},
    )


def _finalized_response() -> CyclesResponse:
    return CyclesResponse.http_error(
        409, "Finalized",
        body={"error": "RESERVATION_FINALIZED", "message": "Finalized", "request_id": "r2"},
    )


def _event_success() -> CyclesResponse:
    return CyclesResponse.success(201, {"status": "APPLIED", "event_id": "evt_1"})


def _commit_success() -> CyclesResponse:
    return CyclesResponse.success(200, {"status": "COMMITTED"})


def _record(reservation_id: str = "rsv_1", **overrides: Any) -> PendingCommitRecord:
    defaults: dict[str, Any] = dict(
        reservation_id=reservation_id,
        base_url=BASE_URL,
        mode="commit",
        commit_body=_commit_body(),
        event_fallback_body=_event_body(),
    )
    defaults.update(overrides)
    return PendingCommitRecord(**defaults)


def _identity_dir(tmp_path: Path, api_key: str = "test-key", base_url: str = BASE_URL) -> Path:
    """The per-identity subdirectory an engine with these credentials uses."""
    return tmp_path / "journal" / auth_fingerprint(base_url, api_key)


def _journal_files(tmp_path: Path) -> list[Path]:
    d = tmp_path / "journal"
    return sorted(d.rglob("*.json")) if d.is_dir() else []


# ---------------------------------------------------------------------------
# CommitJournal
# ---------------------------------------------------------------------------


class TestCommitJournal:
    def test_record_load_discard_roundtrip(self, tmp_path: Path) -> None:
        journal = CommitJournal(tmp_path / "j")
        journal.record(_record("rsv_a"))

        loaded = journal.load_pending(BASE_URL)
        assert len(loaded) == 1
        entry = loaded[0]
        assert entry.reservation_id == "rsv_a"
        assert entry.mode == "commit"
        assert entry.commit_body == _commit_body()
        assert entry.event_fallback_body == _event_body()
        assert entry.recorded_at_ms > 0

        journal.discard("rsv_a")
        assert journal.load_pending(BASE_URL) == []

    def test_record_overwrites_same_reservation(self, tmp_path: Path) -> None:
        journal = CommitJournal(tmp_path / "j")
        journal.record(_record("rsv_a"))
        journal.record(_record("rsv_a", mode="event"))
        loaded = journal.load_pending(BASE_URL)
        assert len(loaded) == 1
        assert loaded[0].mode == "event"

    def test_load_filters_by_base_url(self, tmp_path: Path) -> None:
        journal = CommitJournal(tmp_path / "j")
        journal.record(_record("rsv_a"))
        journal.record(_record("rsv_b", base_url="http://other:9999"))
        loaded = journal.load_pending(BASE_URL)
        assert [e.reservation_id for e in loaded] == ["rsv_a"]

    def test_load_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        journal = CommitJournal(tmp_path / "does-not-exist")
        assert journal.load_pending(BASE_URL) == []

    def test_corrupt_file_renamed_and_skipped(self, tmp_path: Path) -> None:
        directory = tmp_path / "j"
        journal = CommitJournal(directory)
        journal.record(_record("rsv_good"))
        (directory / "rsv_bad.json").write_text("{not json", encoding="utf-8")

        loaded = journal.load_pending(BASE_URL)
        assert [e.reservation_id for e in loaded] == ["rsv_good"]
        assert (directory / "rsv_bad.corrupt").exists()
        assert not (directory / "rsv_bad.json").exists()

    def test_semantically_invalid_records_are_corrupt(self, tmp_path: Path) -> None:
        directory = tmp_path / "j"
        directory.mkdir(parents=True)
        cases = {
            "no_rid.json": '{"reservation_id": "", "mode": "commit", "commit_body": {}}',
            "bad_mode.json": '{"reservation_id": "r1", "mode": "sideways", "commit_body": {}}',
            "commit_no_body.json": '{"reservation_id": "r2", "mode": "commit"}',
            "event_no_body.json": '{"reservation_id": "r3", "mode": "event", "commit_body": {}}',
        }
        for name, content in cases.items():
            (directory / name).write_text(content, encoding="utf-8")

        journal = CommitJournal(directory)
        assert journal.load_pending(BASE_URL) == []
        assert len(list(directory.glob("*.corrupt"))) == len(cases)

    def test_record_swallows_os_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        journal = CommitJournal(tmp_path / "j")
        monkeypatch.setattr(Path, "mkdir", MagicMock(side_effect=OSError("disk full")))
        journal.record(_record("rsv_a"))  # must not raise
        assert journal.load_pending(BASE_URL) == []

    def test_discard_swallows_os_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        journal = CommitJournal(tmp_path / "j")
        journal.record(_record("rsv_a"))
        monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("locked")))
        journal.discard("rsv_a")  # must not raise

    def test_load_swallows_scan_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        journal = CommitJournal(tmp_path / "j")
        journal.record(_record("rsv_a"))
        monkeypatch.setattr(Path, "glob", MagicMock(side_effect=OSError("io error")))
        assert journal.load_pending(BASE_URL) == []

    def test_corrupt_rename_failure_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = tmp_path / "j"
        directory.mkdir(parents=True)
        (directory / "rsv_bad.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(Path, "replace", MagicMock(side_effect=OSError("locked")))

        journal = CommitJournal(directory)
        assert journal.load_pending(BASE_URL) == []  # skipped, no raise

    def test_safe_filename_sanitizes(self) -> None:
        assert _safe_filename("rsv_abc-123") == "rsv_abc-123.json"
        assert _safe_filename("rsv/../etc") == "rsv____etc.json"

    def test_default_journal_dir_under_home(self) -> None:
        # Note: conftest patches the module attribute; this exercises the real function.
        path = default_journal_dir()
        assert path == Path.home() / ".runcycles" / "commit-journal"

    def test_auth_fingerprint_is_stable_and_identity_scoped(self) -> None:
        fp = auth_fingerprint(BASE_URL, "test-key")
        assert fp == auth_fingerprint(BASE_URL, "test-key")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)
        assert fp != auth_fingerprint(BASE_URL, "other-key")
        assert fp != auth_fingerprint("http://other:9999", "test-key")


# ---------------------------------------------------------------------------
# _extract_error_code
# ---------------------------------------------------------------------------


class TestExtractErrorCode:
    def test_from_error_response(self) -> None:
        assert _extract_error_code(_expired_response()) == "RESERVATION_EXPIRED"

    def test_from_raw_body(self) -> None:
        response = CyclesResponse.http_error(400, "Bad", body={"error": "SOMETHING_ODD"})
        assert _extract_error_code(response) == "SOMETHING_ODD"

    def test_none_when_absent(self) -> None:
        assert _extract_error_code(CyclesResponse.http_error(400, "Bad")) is None


# ---------------------------------------------------------------------------
# CommitRetryEngine durability
# ---------------------------------------------------------------------------


class TestSyncEngineDurability:
    def test_schedule_journals_then_success_discards(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body(), _event_body())
        engine.flush(timeout=5.0)

        assert mock_client.commit_reservation.call_count == 1
        assert _journal_files(tmp_path) == []

    def test_expired_falls_back_to_event(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _expired_response()
        mock_client.create_event.return_value = _event_success()
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 1
        mock_client.create_event.assert_called_once_with(_event_body())
        assert _journal_files(tmp_path) == []

    def test_expired_without_fallback_retains_journal(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _expired_response()
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body())
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 1
        mock_client.create_event.assert_not_called()
        assert len(_journal_files(tmp_path)) == 1

    def test_finalized_discards_journal(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _finalized_response()
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert _journal_files(tmp_path) == []

    def test_exhausted_retains_journal(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 3
        assert len(_journal_files(tmp_path)) == 1

    def test_schedule_event_posts_event(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.create_event.return_value = _event_success()
        engine.set_client(mock_client)

        engine.schedule_event("rsv_1", _event_body())
        engine.flush(timeout=5.0)

        mock_client.create_event.assert_called_once_with(_event_body())
        mock_client.commit_reservation.assert_not_called()
        assert _journal_files(tmp_path) == []

    def test_event_client_error_discards_journal(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.create_event.return_value = CyclesResponse.http_error(
            409, "Mismatch", body={"error": "IDEMPOTENCY_MISMATCH", "message": "m", "request_id": "r"},
        )
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", None, _event_body(), mode="event")
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert mock_client.create_event.call_count == 1
        assert _journal_files(tmp_path) == []

    def test_event_transient_then_success(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.create_event.side_effect = [
            CyclesResponse.http_error(500, "boom"),
            _event_success(),
        ]
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", None, _event_body(), mode="event")
        engine._retry_loop(pending)

        assert mock_client.create_event.call_count == 2

    def test_expired_then_event_transient_continues_in_event_mode(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _expired_response()
        mock_client.create_event.side_effect = [
            CyclesResponse.http_error(503, "unavailable"),
            _event_success(),
        ]
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._retry_loop(pending)

        # One commit attempt, then immediate event attempt, then one retried event attempt.
        assert mock_client.commit_reservation.call_count == 1
        assert mock_client.create_event.call_count == 2

    def test_disabled_with_journal_persists_entry(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path, retry_enabled=False))
        mock_client = MagicMock()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body(), _event_body())

        mock_client.commit_reservation.assert_not_called()
        assert len(_journal_files(tmp_path)) == 1

    def test_disabled_without_journal_drops(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path, retry_enabled=False, journal_enabled=False))
        mock_client = MagicMock()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body())

        mock_client.commit_reservation.assert_not_called()
        assert _journal_files(tmp_path) == []

    def test_flush_zero_timeout_returns_immediately(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path, retry_flush_timeout=0.0))
        engine.flush()  # must not raise or block

    def test_flush_gives_up_at_deadline(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()

        def _slow_commit(*args: Any, **kwargs: Any) -> CyclesResponse:
            time.sleep(0.2)
            return _commit_success()

        mock_client.commit_reservation.side_effect = _slow_commit
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body())
        engine.schedule("rsv_2", _commit_body())
        # First join consumes the whole budget; the second iteration hits the deadline.
        engine.flush(timeout=0.05)
        engine.flush(timeout=5.0)  # clean up before the test ends

    def test_atexit_hook_flushes_registered_engines(self, tmp_path: Path) -> None:
        import runcycles.retry as retry_mod

        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body())
        retry_mod._flush_all_engines()  # what atexit runs at interpreter exit

        assert mock_client.commit_reservation.call_count == 1
        assert _journal_files(tmp_path) == []

    def test_flush_all_engines_with_no_engines(self) -> None:
        import weakref

        import runcycles.retry as retry_mod

        original = retry_mod._live_engines
        retry_mod._live_engines = weakref.WeakSet()
        try:
            retry_mod._flush_all_engines()  # must not raise
        finally:
            retry_mod._live_engines = original

    def test_flush_all_engines_shares_one_deadline(self, tmp_path: Path) -> None:
        # Finding 4: exit flush is bounded by retry_flush_timeout for the
        # whole process, not per engine.
        import weakref

        import runcycles.retry as retry_mod

        config = _config(tmp_path, retry_flush_timeout=0.5)

        def _slow_commit(*args: Any, **kwargs: Any) -> CyclesResponse:
            time.sleep(2.0)
            return _commit_success()

        engines = []
        for i in range(2):
            engine = CommitRetryEngine(config)
            mock_client = MagicMock()
            mock_client.commit_reservation.side_effect = _slow_commit
            engine.set_client(mock_client)
            engine.schedule(f"rsv_{i}", _commit_body())
            engines.append(engine)

        isolated: weakref.WeakSet[CommitRetryEngine] = weakref.WeakSet(engines)
        original = retry_mod._live_engines
        retry_mod._live_engines = isolated
        try:
            start = time.monotonic()
            retry_mod._flush_all_engines()
            elapsed = time.monotonic() - start
        finally:
            retry_mod._live_engines = original

        # One shared 0.5s budget — sequential per-engine flushes would take ~1.0s+.
        assert elapsed < 0.9


class TestRateLimitedRetry:
    def test_429_commit_is_transient_and_honors_retry_after(self, tmp_path: Path) -> None:
        # Finding 2: a rate-limited commit must keep its journal entry and
        # keep retrying, waiting at least the server's Retry-After.
        engine = CommitRetryEngine(_config(tmp_path))
        response = CyclesResponse.http_error(
            429, "Rate limited",
            body={"error": "LIMIT_EXCEEDED", "message": "slow down", "request_id": "r9"},
            headers={"retry-after": "2"},
        )
        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)

        assert engine._classify_commit_response(pending, response) is False
        assert pending.retry_after_s == 2.0
        assert len(_journal_files(tmp_path)) == 1  # retained, not discarded

        delay = engine._delay_for(pending)
        assert delay >= 2.0  # server's Retry-After wins over backoff
        assert pending.retry_after_s is None  # consumed — applies once

    def test_429_without_body_detected_by_status(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        pending = _PendingCommit("rsv_1", _commit_body())
        assert engine._classify_commit_response(pending, CyclesResponse.http_error(429, "busy")) is False
        assert pending.retry_after_s is None  # no header → plain backoff

    def test_429_then_success_discards_journal(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.side_effect = [
            CyclesResponse.http_error(429, "busy", headers={"retry-after": "0"}),
            _commit_success(),
        ]
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body())
        engine._journal_record(pending)
        engine._retry_loop(pending)

        assert mock_client.commit_reservation.call_count == 2
        assert _journal_files(tmp_path) == []

    def test_429_event_fallback_is_transient(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.create_event.return_value = CyclesResponse.http_error(
            429, "busy", body={"error": "LIMIT_EXCEEDED", "message": "m", "request_id": "r"},
        )
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", None, _event_body(), mode="event")
        engine._journal_record(pending)
        engine._retry_loop(pending)

        # Exhausts attempts without ever discarding the durable record.
        assert mock_client.create_event.call_count == 3
        assert len(_journal_files(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Journal replay
# ---------------------------------------------------------------------------


class TestSyncReplay:
    def test_replays_pending_commit_on_set_client(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(_record("rsv_old"))

        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)
        engine.flush(timeout=5.0)

        mock_client.commit_reservation.assert_called_once_with("rsv_old", _commit_body())
        assert _journal_files(tmp_path) == []

    def test_replays_event_mode_entry(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(
            _record("rsv_old", mode="event", commit_body=None)
        )

        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        mock_client.create_event.return_value = _event_success()
        engine.set_client(mock_client)
        engine.flush(timeout=5.0)

        mock_client.create_event.assert_called_once_with(_event_body())
        assert _journal_files(tmp_path) == []

    def test_replay_happens_once_per_directory(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(_record("rsv_old"))
        config = _config(tmp_path)

        first = CommitRetryEngine(config)
        client1 = MagicMock()
        client1.commit_reservation.return_value = _commit_success()
        first.set_client(client1)
        first.flush(timeout=5.0)

        second = CommitRetryEngine(config)
        client2 = MagicMock()
        second.set_client(client2)
        second.flush(timeout=5.0)

        assert client1.commit_reservation.call_count == 1
        client2.commit_reservation.assert_not_called()

    def test_replay_skips_other_server_entries(self, tmp_path: Path) -> None:
        # Defense-in-depth: a mismatched-base_url record inside the identity
        # dir (should not happen — the fingerprint includes base_url) is
        # still filtered out rather than replayed against the wrong server.
        journal = CommitJournal(_identity_dir(tmp_path))
        journal.record(_record("rsv_other", base_url="http://other:9999"))

        engine = CommitRetryEngine(_config(tmp_path))
        mock_client = MagicMock()
        engine.set_client(mock_client)
        engine.flush(timeout=5.0)

        mock_client.commit_reservation.assert_not_called()
        assert len(_journal_files(tmp_path)) == 1  # left in place, never discarded

    def test_replay_isolated_by_api_key(self, tmp_path: Path) -> None:
        # Finding 1: same server, different credentials → separate identity
        # dirs. Client A must never replay (and 401-discard) client B's spend.
        CommitJournal(_identity_dir(tmp_path, api_key="test-key")).record(_record("rsv_a"))
        CommitJournal(_identity_dir(tmp_path, api_key="other-key")).record(_record("rsv_b"))

        engine_a = CommitRetryEngine(_config(tmp_path))
        client_a = MagicMock()
        client_a.commit_reservation.return_value = _commit_success()
        engine_a.set_client(client_a)
        engine_a.flush(timeout=5.0)

        client_a.commit_reservation.assert_called_once_with("rsv_a", _commit_body())
        assert _journal_files(tmp_path) == [_identity_dir(tmp_path, api_key="other-key") / "rsv_b.json"]

        engine_b = CommitRetryEngine(_config(tmp_path, api_key="other-key"))
        client_b = MagicMock()
        client_b.commit_reservation.return_value = _commit_success()
        engine_b.set_client(client_b)
        engine_b.flush(timeout=5.0)

        client_b.commit_reservation.assert_called_once_with("rsv_b", _commit_body())
        assert _journal_files(tmp_path) == []

    def test_one_server_claim_does_not_block_another(self, tmp_path: Path) -> None:
        # Finding 3: the replay claim is scoped to the identity subdirectory,
        # so server A's engine starting first cannot starve server B's entries.
        other_url = "http://other:9999"
        CommitJournal(_identity_dir(tmp_path, base_url=other_url)).record(
            _record("rsv_b", base_url=other_url)
        )

        engine_a = CommitRetryEngine(_config(tmp_path))
        client_a = MagicMock()
        engine_a.set_client(client_a)  # claims A's (empty) identity dir first
        engine_a.flush(timeout=5.0)
        client_a.commit_reservation.assert_not_called()

        engine_b = CommitRetryEngine(_config(tmp_path, base_url=other_url))
        client_b = MagicMock()
        client_b.commit_reservation.return_value = _commit_success()
        engine_b.set_client(client_b)
        engine_b.flush(timeout=5.0)

        client_b.commit_reservation.assert_called_once_with("rsv_b", _commit_body())
        assert _journal_files(tmp_path) == []

    def test_no_replay_when_retry_disabled(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(_record("rsv_old"))

        engine = CommitRetryEngine(_config(tmp_path, retry_enabled=False))
        mock_client = MagicMock()
        engine.set_client(mock_client)

        mock_client.commit_reservation.assert_not_called()
        assert len(_journal_files(tmp_path)) == 1

    def test_no_replay_when_journal_disabled(self, tmp_path: Path) -> None:
        engine = CommitRetryEngine(_config(tmp_path, journal_enabled=False))
        mock_client = MagicMock()
        engine.set_client(mock_client)
        mock_client.commit_reservation.assert_not_called()


# ---------------------------------------------------------------------------
# AsyncCommitRetryEngine durability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAsyncEngineDurability:
    async def test_schedule_holds_task_reference_and_discards_on_success(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body(), _event_body())
        assert len(engine._tasks) == 1  # reference held → cannot be garbage-collected
        await engine.flush(timeout=5.0)

        assert mock_client.commit_reservation.call_count == 1
        assert _journal_files(tmp_path) == []
        assert engine._tasks == set()

    async def test_expired_falls_back_to_event(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = _expired_response()
        mock_client.create_event.return_value = _event_success()
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)
        await engine._retry_loop(pending)

        mock_client.create_event.assert_awaited_once_with(_event_body())
        assert _journal_files(tmp_path) == []

    async def test_expired_without_fallback_retains_journal(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = _expired_response()
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body())
        engine._journal_record(pending)
        await engine._retry_loop(pending)

        assert len(_journal_files(tmp_path)) == 1

    async def test_schedule_event_posts_event(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.create_event.return_value = _event_success()
        engine.set_client(mock_client)

        engine.schedule_event("rsv_1", _event_body())
        await engine.flush(timeout=5.0)

        mock_client.create_event.assert_awaited_once_with(_event_body())
        assert _journal_files(tmp_path) == []

    async def test_exhausted_retains_journal(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")
        engine.set_client(mock_client)

        pending = _PendingCommit("rsv_1", _commit_body(), _event_body())
        engine._journal_record(pending)
        await engine._retry_loop(pending)

        assert mock_client.commit_reservation.await_count == 3
        assert len(_journal_files(tmp_path)) == 1

    async def test_disabled_with_journal_persists_entry(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path, retry_enabled=False))
        mock_client = AsyncMock()
        engine.set_client(mock_client)

        engine.schedule("rsv_1", _commit_body())

        mock_client.commit_reservation.assert_not_called()
        assert len(_journal_files(tmp_path)) == 1

    async def test_replay_on_set_client_with_running_loop(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(_record("rsv_old"))

        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)
        await engine.flush(timeout=5.0)

        mock_client.commit_reservation.assert_awaited_once_with("rsv_old", _commit_body())
        assert _journal_files(tmp_path) == []

    async def test_flush_zero_timeout_returns_immediately(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path, retry_flush_timeout=0.0))
        await engine.flush()  # must not raise or block


class TestAsyncEngineNoLoop:
    def test_schedule_without_loop_keeps_journal_entry(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path))
        engine.set_client(AsyncMock())

        engine.schedule("rsv_1", _commit_body(), _event_body())

        # Could not spawn a task, but the entry survives for the next run.
        assert len(_journal_files(tmp_path)) == 1

    def test_schedule_without_loop_and_journal_drops(self, tmp_path: Path) -> None:
        engine = AsyncCommitRetryEngine(_config(tmp_path, journal_enabled=False))
        engine.set_client(AsyncMock())
        engine.schedule("rsv_1", _commit_body())  # must not raise
        assert _journal_files(tmp_path) == []

    def test_deferred_replay_runs_at_first_schedule(self, tmp_path: Path) -> None:
        CommitJournal(_identity_dir(tmp_path)).record(_record("rsv_old"))

        engine = AsyncCommitRetryEngine(_config(tmp_path))
        mock_client = AsyncMock()
        mock_client.commit_reservation.return_value = _commit_success()
        engine.set_client(mock_client)  # no loop yet → replay deferred
        assert engine._replay_deferred is True

        async def _run() -> None:
            engine.schedule("rsv_new", _commit_body(), _event_body())
            await engine.flush(timeout=5.0)

        asyncio.run(_run())

        committed_ids = {call.args[0] for call in mock_client.commit_reservation.await_args_list}
        assert committed_ids == {"rsv_old", "rsv_new"}
        assert _journal_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Event fallback body construction
# ---------------------------------------------------------------------------


class TestBuildEventFallbackBody:
    def test_builds_spec_shape_reusing_commit_idempotency_key(self) -> None:
        commit_body = {
            "idempotency_key": "ck-9",
            "actual": {"unit": "USD_MICROCENTS", "amount": 250},
            "metrics": {"latency_ms": 12},
            "metadata": {"run": "abc"},
        }
        body = _build_event_fallback_body(
            "rsv_9", {"tenant": "acme"}, {"kind": "llm.completion", "name": "gpt"}, commit_body,
        )

        assert body["idempotency_key"] == "ck-9"
        assert body["subject"] == {"tenant": "acme"}
        assert body["action"] == {"kind": "llm.completion", "name": "gpt"}
        assert body["actual"] == {"unit": "USD_MICROCENTS", "amount": 250}
        assert body["metrics"] == {"latency_ms": 12}
        assert body["metadata"]["run"] == "abc"
        assert body["metadata"]["recovered_reservation_id"] == "rsv_9"
        assert body["metadata"]["recovery_reason"] == "commit_after_reservation_expired"
        assert "overage_policy" not in body  # server default ALLOW_IF_AVAILABLE never rejects

    def test_without_metrics_or_metadata(self) -> None:
        body = _build_event_fallback_body(
            "rsv_9", {"tenant": "acme"}, {"kind": "k", "name": "n"}, _commit_body(),
        )
        assert "metrics" not in body
        assert set(body["metadata"]) == {"recovered_reservation_id", "recovery_reason"}


# ---------------------------------------------------------------------------
# Lifecycle wiring: expired commit → schedule_event, transient → fallback passed
# ---------------------------------------------------------------------------


def _allow_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "ALLOW",
        "reservation_id": "rsv_test",
        "expires_at_ms": int(time.time() * 1000) + 600_000,
        "affected_scopes": ["tenant:acme"],
        "scope_path": "tenant:acme",
        "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


def _make_cfg() -> DecoratorConfig:
    return DecoratorConfig(estimate=1000, tenant="acme", ttl_ms=60000)


class TestLifecycleEventFallbackWiring:
    def _make(self, tmp_path: Path) -> tuple[CyclesLifecycle, MagicMock, MagicMock]:
        mock_client = MagicMock()
        mock_client._config = _config(tmp_path)
        engine = MagicMock(spec=CommitRetryEngine)
        lifecycle = CyclesLifecycle(mock_client, engine, {"tenant": "acme"})
        return lifecycle, mock_client, engine

    def test_expired_commit_schedules_event(self, tmp_path: Path) -> None:
        lifecycle, mock_client, engine = self._make(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _expired_response()

        lifecycle.execute(lambda: "result", (), {}, _make_cfg())

        engine.schedule_event.assert_called_once()
        rid, event_body = engine.schedule_event.call_args.args
        assert rid == "rsv_test"
        assert event_body["subject"] == {"tenant": "acme"}
        assert event_body["metadata"]["recovered_reservation_id"] == "rsv_test"
        mock_client.release_reservation.assert_not_called()

    def test_transient_commit_passes_event_fallback(self, tmp_path: Path) -> None:
        lifecycle, mock_client, engine = self._make(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")

        lifecycle.execute(lambda: "result", (), {}, _make_cfg())

        engine.schedule.assert_called_once()
        args = engine.schedule.call_args.args
        assert args[0] == "rsv_test"
        assert args[2]["metadata"]["recovered_reservation_id"] == "rsv_test"

    def test_finalized_commit_does_not_schedule_event(self, tmp_path: Path) -> None:
        lifecycle, mock_client, engine = self._make(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _finalized_response()

        lifecycle.execute(lambda: "result", (), {}, _make_cfg())

        engine.schedule_event.assert_not_called()
        engine.schedule.assert_not_called()


@pytest.mark.asyncio
class TestAsyncLifecycleEventFallbackWiring:
    def _make(self, tmp_path: Path) -> tuple[AsyncCyclesLifecycle, AsyncMock, MagicMock]:
        mock_client = AsyncMock()
        mock_client._config = _config(tmp_path)
        engine = MagicMock(spec=AsyncCommitRetryEngine)
        lifecycle = AsyncCyclesLifecycle(mock_client, engine, {"tenant": "acme"})
        return lifecycle, mock_client, engine

    async def test_expired_commit_schedules_event(self, tmp_path: Path) -> None:
        lifecycle, mock_client, engine = self._make(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _expired_response()

        async def fn() -> str:
            return "result"

        await lifecycle.execute(fn, (), {}, _make_cfg())

        engine.schedule_event.assert_called_once()
        rid, event_body = engine.schedule_event.call_args.args
        assert rid == "rsv_test"
        assert event_body["metadata"]["recovery_reason"] == "commit_after_reservation_expired"

    async def test_transient_commit_passes_event_fallback(self, tmp_path: Path) -> None:
        lifecycle, mock_client, engine = self._make(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")

        async def fn() -> str:
            return "result"

        await lifecycle.execute(fn, (), {}, _make_cfg())

        engine.schedule.assert_called_once()
        args = engine.schedule.call_args.args
        assert args[2]["metadata"]["recovered_reservation_id"] == "rsv_test"


# ---------------------------------------------------------------------------
# Streaming wiring: expired commit → schedule_event
# ---------------------------------------------------------------------------


class TestStreamingEventFallbackWiring:
    def _make_stream(self, tmp_path: Path) -> tuple[Any, MagicMock, MagicMock]:
        from runcycles.client import CyclesClient
        from runcycles.models import Action, Amount, Subject, Unit
        from runcycles.streaming import StreamReservation

        mock_client = MagicMock(spec=CyclesClient)
        mock_client._config = _config(tmp_path)
        stream = StreamReservation(
            mock_client,
            subject=Subject(tenant="acme"),
            action=Action(kind="llm.completion", name="gpt"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        engine = MagicMock(spec=CommitRetryEngine)
        stream._retry_engine = engine
        return stream, mock_client, engine

    def test_expired_commit_schedules_event(self, tmp_path: Path) -> None:
        stream, mock_client, engine = self._make_stream(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _expired_response()

        with stream:
            pass

        engine.schedule_event.assert_called_once()
        rid, event_body = engine.schedule_event.call_args.args
        assert rid == "rsv_test"
        assert event_body["subject"] == {"tenant": "acme"}
        assert event_body["metadata"]["recovered_reservation_id"] == "rsv_test"

    def test_transient_commit_passes_event_fallback(self, tmp_path: Path) -> None:
        stream, mock_client, engine = self._make_stream(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")

        with stream:
            pass

        engine.schedule.assert_called_once()
        args = engine.schedule.call_args.args
        assert args[2]["metadata"]["recovered_reservation_id"] == "rsv_test"


@pytest.mark.asyncio
class TestAsyncStreamingEventFallbackWiring:
    async def _make_stream(self, tmp_path: Path) -> tuple[Any, AsyncMock, MagicMock]:
        from runcycles.client import AsyncCyclesClient
        from runcycles.models import Action, Amount, Subject, Unit
        from runcycles.streaming import AsyncStreamReservation

        mock_client = AsyncMock(spec=AsyncCyclesClient)
        mock_client._config = _config(tmp_path)
        stream = AsyncStreamReservation(
            mock_client,
            subject=Subject(tenant="acme"),
            action=Action(kind="llm.completion", name="gpt"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=60_000,
        )
        engine = MagicMock(spec=AsyncCommitRetryEngine)
        stream._retry_engine = engine
        return stream, mock_client, engine

    async def test_expired_commit_schedules_event(self, tmp_path: Path) -> None:
        stream, mock_client, engine = await self._make_stream(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _expired_response()

        async with stream:
            pass

        engine.schedule_event.assert_called_once()
        rid, event_body = engine.schedule_event.call_args.args
        assert rid == "rsv_test"
        assert event_body["metadata"]["recovery_reason"] == "commit_after_reservation_expired"

    async def test_transient_commit_passes_event_fallback(self, tmp_path: Path) -> None:
        stream, mock_client, engine = await self._make_stream(tmp_path)
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "boom")

        async with stream:
            pass

        engine.schedule.assert_called_once()
        args = engine.schedule.call_args.args
        assert args[2]["metadata"]["recovered_reservation_id"] == "rsv_test"
