"""Background commit retry engine with exponential backoff.

Durability model: every scheduled retry is journaled to disk first (see
:mod:`runcycles.journal`) and the entry is removed only on a terminal
outcome, so pending commits survive process restarts and are replayed the
next time an engine is created for the same journal directory. When a
retried commit lands after the reservation's grace period (the server
answers ``RESERVATION_EXPIRED`` and has already returned the reserved
budget to the pool), the engine falls back to ``POST /v1/events`` — the
spec's post-hoc direct-debit endpoint — so the spend is still recorded.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runcycles import journal as _journal
from runcycles.config import CyclesConfig
from runcycles.journal import CommitJournal, PendingCommitRecord
from runcycles.response import CyclesResponse

logger = logging.getLogger(__name__)


@dataclass
class _PendingCommit:
    reservation_id: str
    commit_body: dict[str, Any] | None = None
    event_fallback_body: dict[str, Any] | None = None
    mode: str = "commit"  # "commit" | "event"
    attempt: int = 0


def _extract_error_code(response: CyclesResponse) -> str | None:
    error_resp = response.get_error_response()
    if error_resp and error_resp.error_code:
        return error_resp.error_code.value
    raw = response.get_body_attribute("error")
    return raw if isinstance(raw, str) else None


# Journal replay must happen at most once per journal directory per process:
# the first engine created for a directory claims it and replays surviving
# entries; later engines (and the claimer's own in-flight work) are excluded.
_replay_lock = threading.Lock()
_replayed_dirs: set[Path] = set()


def _claim_replay(directory: Path) -> bool:
    with _replay_lock:
        if directory in _replayed_dirs:
            return False
        _replayed_dirs.add(directory)
        return True


# Retry threads are daemons so they never wedge interpreter exit, but a
# clean exit would otherwise kill them mid-backoff. A single atexit hook
# gives in-flight retries a bounded window to finish; whatever remains
# stays journaled and replays on the next run.
_flush_lock = threading.Lock()
_live_engines: weakref.WeakSet[CommitRetryEngine] = weakref.WeakSet()
_atexit_registered = False


def _flush_all_engines() -> None:
    for engine in list(_live_engines):
        engine.flush()


def _register_engine_for_flush(engine: CommitRetryEngine) -> None:
    global _atexit_registered
    with _flush_lock:
        _live_engines.add(engine)
        if not _atexit_registered:
            atexit.register(_flush_all_engines)
            _atexit_registered = True


class _RetryEngineBase:
    """Configuration, journal plumbing, and outcome classification shared by both engines."""

    def __init__(self, config: CyclesConfig) -> None:
        self._enabled = config.retry_enabled
        self._max_attempts = config.retry_max_attempts
        self._initial_delay = config.retry_initial_delay
        self._multiplier = config.retry_multiplier
        self._max_delay = config.retry_max_delay
        self._flush_timeout = config.retry_flush_timeout
        self._base_url = config.base_url
        self._client: Any = None  # set by lifecycle to avoid circular import
        self._journal: CommitJournal | None = None
        if config.journal_enabled:
            directory = Path(config.journal_dir) if config.journal_dir else _journal.default_journal_dir()
            self._journal = CommitJournal(directory)

    def _journal_record(self, pending: _PendingCommit) -> None:
        if self._journal is not None:
            self._journal.record(
                PendingCommitRecord(
                    reservation_id=pending.reservation_id,
                    base_url=self._base_url,
                    mode=pending.mode,
                    commit_body=pending.commit_body,
                    event_fallback_body=pending.event_fallback_body,
                )
            )

    def _journal_discard(self, reservation_id: str) -> None:
        if self._journal is not None:
            self._journal.discard(reservation_id)

    def _load_replay_entries(self) -> list[_PendingCommit]:
        """Claim and load journaled entries for this engine's server, if eligible."""
        if not self._enabled or self._journal is None or self._client is None:
            return []
        if not _claim_replay(self._journal.directory):
            return []
        entries = self._journal.load_pending(self._base_url)
        if entries:
            logger.info(
                "Replaying %d journaled pending commit(s) from %s", len(entries), self._journal.directory
            )
        return [
            _PendingCommit(
                reservation_id=e.reservation_id,
                commit_body=e.commit_body,
                event_fallback_body=e.event_fallback_body,
                mode=e.mode,
            )
            for e in entries
        ]

    def _log_disabled_drop(self, pending: _PendingCommit) -> None:
        if self._journal is not None:
            logger.warning(
                "Retry disabled; pending %s journaled for replay on next run: reservation_id=%s",
                pending.mode, pending.reservation_id,
            )
        else:
            logger.warning(
                "Retry and journal disabled, dropping failed %s: reservation_id=%s",
                pending.mode, pending.reservation_id,
            )

    def _delay_for(self, attempt: int) -> float:
        return min(self._initial_delay * (self._multiplier**attempt), self._max_delay)

    def _classify_commit_response(self, pending: _PendingCommit, response: CyclesResponse) -> bool:
        """Handle a commit attempt's response. Returns True when terminal.

        May flip ``pending`` into event mode; the caller then delivers the
        event fallback immediately (no extra backoff) via ``_attempt_event``.
        """
        if response.is_success:
            logger.info(
                "Commit retry succeeded: reservation_id=%s, attempt=%d",
                pending.reservation_id, pending.attempt,
            )
            self._journal_discard(pending.reservation_id)
            return True
        if response.is_client_error:
            code = _extract_error_code(response)
            if code == "RESERVATION_EXPIRED":
                if pending.event_fallback_body:
                    logger.warning(
                        "Reservation expired before commit landed; falling back to POST /v1/events: "
                        "reservation_id=%s",
                        pending.reservation_id,
                    )
                    pending.mode = "event"
                    pending.attempt = 0
                    self._journal_record(pending)
                    return False
                logger.error(
                    "Reservation expired with no event fallback; spend is unrecorded "
                    "(journal entry retained): reservation_id=%s",
                    pending.reservation_id,
                )
                return True
            logger.warning(
                "Commit retry got non-retryable error: reservation_id=%s, status=%d, error=%s",
                pending.reservation_id, response.status, code,
            )
            self._journal_discard(pending.reservation_id)
            return True
        logger.warning(
            "Commit retry failed: reservation_id=%s, attempt=%d, status=%d",
            pending.reservation_id, pending.attempt, response.status,
        )
        return False

    def _classify_event_response(self, pending: _PendingCommit, response: CyclesResponse) -> bool:
        """Handle an event-fallback attempt's response. Returns True when terminal."""
        if response.is_success:
            logger.info(
                "Recovered expired-commit spend via /v1/events: reservation_id=%s, event_id=%s",
                pending.reservation_id, response.get_body_attribute("event_id"),
            )
            self._journal_discard(pending.reservation_id)
            return True
        if response.is_client_error:
            logger.error(
                "Event fallback rejected (%s); spend recovery failed: reservation_id=%s, status=%d",
                _extract_error_code(response), pending.reservation_id, response.status,
            )
            self._journal_discard(pending.reservation_id)
            return True
        logger.warning(
            "Event fallback failed: reservation_id=%s, attempt=%d, status=%d",
            pending.reservation_id, pending.attempt, response.status,
        )
        return False

    def _log_exhausted(self, pending: _PendingCommit) -> None:
        logger.error(
            "%s retry exhausted: reservation_id=%s, attempts=%d%s",
            pending.mode, pending.reservation_id, self._max_attempts,
            " (journal entry retained for replay on next run)" if self._journal is not None else "",
        )


class CommitRetryEngine(_RetryEngineBase):
    """Retries failed commits in background threads with exponential backoff.

    Used by the sync lifecycle. Commits that fail transiently are scheduled
    for retry. The engine stops retrying after ``max_attempts``; entries
    remain journaled for replay on the next run.
    """

    def __init__(self, config: CyclesConfig) -> None:
        super().__init__(config)
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    def set_client(self, client: Any) -> None:
        self._client = client
        for pending in self._load_replay_entries():
            self._spawn(pending)

    def schedule(
        self,
        reservation_id: str,
        commit_body: dict[str, Any],
        event_fallback_body: dict[str, Any] | None = None,
    ) -> None:
        self._submit(_PendingCommit(reservation_id, commit_body, event_fallback_body, mode="commit"))

    def schedule_event(self, reservation_id: str, event_body: dict[str, Any]) -> None:
        """Deliver spend via POST /v1/events for a reservation that already expired."""
        self._submit(_PendingCommit(reservation_id, None, event_body, mode="event"))

    def _submit(self, pending: _PendingCommit) -> None:
        self._journal_record(pending)
        if not self._enabled:
            self._log_disabled_drop(pending)
            return
        self._spawn(pending)

    def _spawn(self, pending: _PendingCommit) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(pending,),
            daemon=True,
            name=f"cycles-commit-retry-{pending.reservation_id[:12]}",
        )
        with self._threads_lock:
            self._threads.add(thread)
        _register_engine_for_flush(self)
        thread.start()

    def _run(self, pending: _PendingCommit) -> None:
        try:
            self._retry_loop(pending)
        finally:
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def flush(self, timeout: float | None = None) -> None:
        """Wait (bounded) for in-flight retry threads to finish.

        Called automatically at interpreter exit. Anything still pending
        when the timeout elapses stays journaled and replays on the next run.
        """
        if timeout is None:
            timeout = self._flush_timeout
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=remaining)

    def _retry_loop(self, pending: _PendingCommit) -> None:
        while pending.attempt < self._max_attempts:
            delay = self._delay_for(pending.attempt)
            pending.attempt += 1
            logger.info(
                "Scheduling %s retry: reservation_id=%s, attempt=%d/%d, delay=%.1fs",
                pending.mode, pending.reservation_id, pending.attempt, self._max_attempts, delay,
            )
            time.sleep(delay)

            try:
                if self._client is None:
                    logger.error("No client set on retry engine, cannot retry %s", pending.mode)
                    return
                if self._attempt_once(pending):
                    return
            except Exception:
                logger.exception(
                    "%s retry error: reservation_id=%s, attempt=%d",
                    pending.mode, pending.reservation_id, pending.attempt,
                )

        self._log_exhausted(pending)

    def _attempt_once(self, pending: _PendingCommit) -> bool:
        if pending.mode == "commit":
            response = self._client.commit_reservation(pending.reservation_id, pending.commit_body)
            terminal = self._classify_commit_response(pending, response)
            if not terminal and pending.mode == "event":
                # Expired → deliver the event fallback immediately, no extra backoff.
                return self._attempt_once(pending)
            return terminal
        response = self._client.create_event(pending.event_fallback_body)
        return self._classify_event_response(pending, response)


class AsyncCommitRetryEngine(_RetryEngineBase):
    """Retries failed commits as async tasks with exponential backoff.

    Used by the async lifecycle. Task references are held until completion
    so pending retries cannot be garbage-collected mid-flight.
    """

    def __init__(self, config: CyclesConfig) -> None:
        super().__init__(config)
        self._tasks: set[asyncio.Task[None]] = set()
        self._replay_deferred = False

    def set_client(self, client: Any) -> None:
        self._client = client
        self._maybe_replay()

    def schedule(
        self,
        reservation_id: str,
        commit_body: dict[str, Any],
        event_fallback_body: dict[str, Any] | None = None,
    ) -> None:
        self._submit(_PendingCommit(reservation_id, commit_body, event_fallback_body, mode="commit"))

    def schedule_event(self, reservation_id: str, event_body: dict[str, Any]) -> None:
        """Deliver spend via POST /v1/events for a reservation that already expired."""
        self._submit(_PendingCommit(reservation_id, None, event_body, mode="event"))

    def _submit(self, pending: _PendingCommit) -> None:
        self._journal_record(pending)
        if not self._enabled:
            self._log_disabled_drop(pending)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._journal is not None:
                logger.error(
                    "No running event loop; pending %s journaled for replay on next run: reservation_id=%s",
                    pending.mode, pending.reservation_id,
                )
            else:
                logger.error(
                    "No running event loop, cannot schedule async %s retry: reservation_id=%s",
                    pending.mode, pending.reservation_id,
                )
            return
        if self._replay_deferred:
            self._replay_deferred = False
            self._maybe_replay()
        self._spawn(loop, pending)

    def _spawn(self, loop: asyncio.AbstractEventLoop, pending: _PendingCommit) -> None:
        task = loop.create_task(self._retry_loop(pending))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _maybe_replay(self) -> None:
        if not self._enabled or self._journal is None or self._client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop yet (e.g. engine built during sync setup) — replay on
            # the first schedule() call, which requires a running loop.
            self._replay_deferred = True
            return
        for pending in self._load_replay_entries():
            self._spawn(loop, pending)

    async def flush(self, timeout: float | None = None) -> None:
        """Wait (bounded) for in-flight retry tasks to finish."""
        if timeout is None:
            timeout = self._flush_timeout
        if timeout <= 0:
            return
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def _retry_loop(self, pending: _PendingCommit) -> None:
        while pending.attempt < self._max_attempts:
            delay = self._delay_for(pending.attempt)
            pending.attempt += 1
            logger.info(
                "Scheduling async %s retry: reservation_id=%s, attempt=%d/%d, delay=%.1fs",
                pending.mode, pending.reservation_id, pending.attempt, self._max_attempts, delay,
            )
            await asyncio.sleep(delay)

            try:
                if self._client is None:
                    logger.error("No client set on async retry engine, cannot retry %s", pending.mode)
                    return
                if await self._attempt_once(pending):
                    return
            except Exception:
                logger.exception(
                    "Async %s retry error: reservation_id=%s, attempt=%d",
                    pending.mode, pending.reservation_id, pending.attempt,
                )

        self._log_exhausted(pending)

    async def _attempt_once(self, pending: _PendingCommit) -> bool:
        if pending.mode == "commit":
            response = await self._client.commit_reservation(pending.reservation_id, pending.commit_body)
            terminal = self._classify_commit_response(pending, response)
            if not terminal and pending.mode == "event":
                # Expired → deliver the event fallback immediately, no extra backoff.
                return await self._attempt_once(pending)
            return terminal
        response = await self._client.create_event(pending.event_fallback_body)
        return self._classify_event_response(pending, response)
