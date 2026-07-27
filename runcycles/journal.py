"""Durable on-disk journal for pending commits awaiting retry.

Committed spend must survive process restarts: a commit that fails
transiently and only lives in an in-memory retry thread vanishes if the
process exits, and once the reservation's grace period elapses the server
returns the reserved budget to the pool — the ledger under-counts real
spend. The journal records every pending commit before the background
retry starts, and removes it only on a terminal outcome. On the next
process start the SDK replays surviving entries: commit first (idempotent),
falling back to ``POST /v1/events`` when the reservation has expired.

Journal I/O is strictly best-effort — a failure to persist must never break
the commit path itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RECORD_VERSION = 1
_SUFFIX = ".json"


def default_journal_dir() -> Path:
    """Default location for the pending-commit journal."""
    return Path.home() / ".runcycles" / "commit-journal"


def auth_fingerprint(base_url: str, api_key: str, tenant: str | None = None) -> str:
    """Non-secret identity for one (server, principal) pair.

    Journal records are stored under a per-identity subdirectory so that
    clients sharing a journal directory but using different servers or
    principals never replay each other's records. When a tenant is
    configured it is the principal — stable across API-key rotation, and
    any same-tenant credential may settle the records. Without a tenant
    the API key itself is the principal; rotating it then orphans pending
    records under the old fingerprint (records are plain JSON, so an
    operator can move them into the new identity directory — replay is
    idempotent). A truncated SHA-256 is not reversible and API keys are
    high-entropy, so the fingerprint is safe to use as a directory name.
    """
    principal = f"tenant\n{tenant}" if tenant else f"key\n{api_key}"
    digest = hashlib.sha256(f"{base_url}\n{principal}".encode()).hexdigest()
    return digest[:16]


def _restrict_permissions(path: Path, mode: int) -> None:
    """Best-effort permission tightening — records carry spend metadata.

    No-op semantics on platforms without POSIX modes; failure never blocks
    the write itself.
    """
    try:
        path.chmod(mode)
    except OSError:
        pass


def _safe_filename(reservation_id: str) -> str:
    sanitized = "".join(c if c.isalnum() or c in "-_" else "_" for c in reservation_id)
    return f"{sanitized}{_SUFFIX}"


@dataclass
class PendingCommitRecord:
    """One journaled pending commit (or its event fallback)."""

    reservation_id: str
    base_url: str
    mode: str = "commit"  # "commit" | "event"
    commit_body: dict[str, Any] | None = None
    event_fallback_body: dict[str, Any] | None = None
    recorded_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": _RECORD_VERSION,
                "reservation_id": self.reservation_id,
                "base_url": self.base_url,
                "mode": self.mode,
                "commit_body": self.commit_body,
                "event_fallback_body": self.event_fallback_body,
                "recorded_at_ms": self.recorded_at_ms,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> PendingCommitRecord:
        data = json.loads(raw)
        reservation_id = data["reservation_id"]
        mode = data.get("mode", "commit")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("journal record missing reservation_id")
        if mode not in ("commit", "event"):
            raise ValueError(f"journal record has unknown mode: {mode}")
        if mode == "commit" and not isinstance(data.get("commit_body"), dict):
            raise ValueError("commit-mode journal record missing commit_body")
        if mode == "event" and not isinstance(data.get("event_fallback_body"), dict):
            raise ValueError("event-mode journal record missing event_fallback_body")
        return cls(
            reservation_id=reservation_id,
            base_url=data.get("base_url", ""),
            mode=mode,
            commit_body=data.get("commit_body"),
            event_fallback_body=data.get("event_fallback_body"),
            recorded_at_ms=int(data.get("recorded_at_ms", 0)),
        )


class CommitJournal:
    """File-per-pending-commit journal.

    Each record is one JSON file named after its reservation id, written
    atomically (temp file + rename) and deleted on a terminal outcome.
    One file per commit avoids cross-process file locking; concurrent
    replay by multiple processes is safe because commit and event requests
    both carry idempotency keys.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    @property
    def directory(self) -> Path:
        return self._dir

    def record(self, entry: PendingCommitRecord) -> None:
        """Persist a pending commit. Never raises."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(self._dir, 0o700)
            target = self._dir / _safe_filename(entry.reservation_id)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(entry.to_json(), encoding="utf-8")
            _restrict_permissions(tmp, 0o600)
            tmp.replace(target)
            logger.debug("Journaled pending commit: id=%s, path=%s", entry.reservation_id, target)
        except OSError:
            logger.warning(
                "Failed to journal pending commit (continuing without durability): id=%s",
                entry.reservation_id,
                exc_info=True,
            )

    def discard(self, reservation_id: str) -> None:
        """Remove a journal entry after a terminal outcome. Never raises."""
        try:
            (self._dir / _safe_filename(reservation_id)).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to discard journal entry: id=%s", reservation_id, exc_info=True)

    def load_pending(self, base_url: str) -> list[PendingCommitRecord]:
        """Load surviving entries for the given server. Never raises.

        Only entries recorded against the same ``base_url`` are returned —
        a journal directory may be shared by processes talking to different
        servers, and replaying against the wrong one would fail auth.
        Unparseable files are renamed to ``*.corrupt`` so they surface to
        operators instead of being retried forever.
        """
        entries: list[PendingCommitRecord] = []
        try:
            if not self._dir.is_dir():
                return entries
            for path in sorted(self._dir.glob(f"*{_SUFFIX}")):
                try:
                    entry = PendingCommitRecord.from_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    logger.warning("Skipping corrupt journal entry: %s", path, exc_info=True)
                    try:
                        path.replace(path.with_suffix(".corrupt"))
                    except OSError:
                        pass
                    continue
                if entry.base_url == base_url:
                    entries.append(entry)
        except OSError:
            logger.warning("Failed to scan commit journal: %s", self._dir, exc_info=True)
        return entries
