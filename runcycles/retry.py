"""Background commit retry engine with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from runcycles.config import CyclesConfig

logger = logging.getLogger(__name__)


@dataclass
class _PendingCommit:
    reservation_id: str
    commit_body: dict[str, Any]
    attempt: int = 0


class CommitRetryEngine:
    """Retries failed commits in background threads with exponential backoff.

    Used by the sync lifecycle. Commits that fail transiently are scheduled
    for retry. The engine stops retrying after ``max_attempts``.
    """

    def __init__(self, config: CyclesConfig) -> None:
        self._enabled = config.retry_enabled
        self._max_attempts = config.retry_max_attempts
        self._initial_delay = config.retry_initial_delay
        self._multiplier = config.retry_multiplier
        self._max_delay = config.retry_max_delay
        self._client: Any = None  # set by lifecycle to avoid circular import

    def set_client(self, client: Any) -> None:
        self._client = client

    def schedule(self, reservation_id: str, commit_body: dict[str, Any]) -> None:
        if not self._enabled:
            logger.warning("Retry disabled, dropping failed commit: reservation_id=%s", reservation_id)
            return

        pending = _PendingCommit(reservation_id=reservation_id, commit_body=commit_body)
        thread = threading.Thread(target=self._retry_loop, args=(pending,), daemon=True)
        thread.start()

    def _retry_loop(self, pending: _PendingCommit) -> None:
        while pending.attempt < self._max_attempts:
            delay = min(self._initial_delay * (self._multiplier ** pending.attempt), self._max_delay)
            pending.attempt += 1
            logger.info(
                "Scheduling commit retry: reservation_id=%s, attempt=%d/%d, delay=%.1fs",
                pending.reservation_id, pending.attempt, self._max_attempts, delay,
            )
            time.sleep(delay)

            try:
                if self._client is None:
                    logger.error("No client set on retry engine, cannot retry commit")
                    return
                response = self._client.commit_reservation(pending.reservation_id, pending.commit_body)
                if response.is_success:
                    logger.info(
                        "Commit retry succeeded: reservation_id=%s, attempt=%d",
                        pending.reservation_id, pending.attempt,
                    )
                    return
                elif response.is_client_error:
                    logger.warning(
                        "Commit retry got non-retryable error: reservation_id=%s, status=%d",
                        pending.reservation_id, response.status,
                    )
                    return
                else:
                    logger.warning(
                        "Commit retry failed: reservation_id=%s, attempt=%d, status=%d",
                        pending.reservation_id, pending.attempt, response.status,
                    )
            except Exception:
                logger.exception(
                    "Commit retry error: reservation_id=%s, attempt=%d",
                    pending.reservation_id, pending.attempt,
                )

        logger.error(
            "Commit retry exhausted: reservation_id=%s, attempts=%d",
            pending.reservation_id, self._max_attempts,
        )


class AsyncCommitRetryEngine:
    """Retries failed commits as async tasks with exponential backoff.

    Used by the async lifecycle.
    """

    def __init__(self, config: CyclesConfig) -> None:
        self._enabled = config.retry_enabled
        self._max_attempts = config.retry_max_attempts
        self._initial_delay = config.retry_initial_delay
        self._multiplier = config.retry_multiplier
        self._max_delay = config.retry_max_delay
        self._client: Any = None

    def set_client(self, client: Any) -> None:
        self._client = client

    def schedule(self, reservation_id: str, commit_body: dict[str, Any]) -> None:
        if not self._enabled:
            logger.warning("Retry disabled, dropping failed commit: reservation_id=%s", reservation_id)
            return

        pending = _PendingCommit(reservation_id=reservation_id, commit_body=commit_body)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._retry_loop(pending))
        except RuntimeError:
            logger.error("No running event loop, cannot schedule async commit retry: reservation_id=%s", reservation_id)

    async def _retry_loop(self, pending: _PendingCommit) -> None:
        while pending.attempt < self._max_attempts:
            delay = min(self._initial_delay * (self._multiplier ** pending.attempt), self._max_delay)
            pending.attempt += 1
            logger.info(
                "Scheduling async commit retry: reservation_id=%s, attempt=%d/%d, delay=%.1fs",
                pending.reservation_id, pending.attempt, self._max_attempts, delay,
            )
            await asyncio.sleep(delay)

            try:
                if self._client is None:
                    logger.error("No client set on async retry engine, cannot retry commit")
                    return
                response = await self._client.commit_reservation(pending.reservation_id, pending.commit_body)
                if response.is_success:
                    logger.info(
                        "Async commit retry succeeded: reservation_id=%s, attempt=%d",
                        pending.reservation_id, pending.attempt,
                    )
                    return
                elif response.is_client_error:
                    logger.warning(
                        "Async commit retry got non-retryable error: reservation_id=%s, status=%d",
                        pending.reservation_id, response.status,
                    )
                    return
                else:
                    logger.warning(
                        "Async commit retry failed: reservation_id=%s, attempt=%d, status=%d",
                        pending.reservation_id, pending.attempt, response.status,
                    )
            except Exception:
                logger.exception(
                    "Async commit retry error: reservation_id=%s, attempt=%d",
                    pending.reservation_id, pending.attempt,
                )

        logger.error(
            "Async commit retry exhausted: reservation_id=%s, attempts=%d",
            pending.reservation_id, self._max_attempts,
        )
