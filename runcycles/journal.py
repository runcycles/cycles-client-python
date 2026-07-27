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
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RECORD_VERSION = 1
_SUFFIX = ".json"


def default_journal_dir() -> Path:
    """Default location for the pending-commit journal."""
    return Path.home() / ".runcycles" / "commit-journal"


@lru_cache(maxsize=256)
def _principal_digest(base_url: str, principal: str) -> str:
    # PBKDF2 rather than a bare hash: the principal may embed a credential,
    # and a KDF makes offline recovery from a leaked directory name harder
    # for a weak user-chosen key. The round count is deliberately modest
    # (~tens of ms cold, cached per identity per process): the principal is
    # normally a high-entropy machine credential, so rounds only defend the
    # weak-key fallback, and this runs synchronously at engine setup —
    # password-storage round counts would stall async callers. Parameters
    # are fixed constants — the digest must be deterministic across
    # processes and releases.
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        principal.encode(),
        f"runcycles-commit-journal\n{base_url}".encode(),
        30_000,
    )
    return digest.hex()[:16]


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
    idempotent). The truncated PBKDF2-HMAC-SHA256 digest is not reversible,
    so the fingerprint is safe to use as a directory name.
    """
    principal = f"tenant\n{tenant}" if tenant and tenant.strip() else f"key\n{api_key}"
    return _principal_digest(base_url, principal)


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
    # ASCII-only, matching the TS/Java SDKs exactly: same-tenant clients in
    # other languages settle records from this directory, and their discard()
    # must compute the identical filename or the record replays forever.
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", reservation_id)
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
    # Absolute wall-clock floor (ms) for the next attempt, set from a 429's
    # Retry-After. Absolute so it survives a process restart mid-wait.
    not_before_ms: int | None = None

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
                "not_before_ms": self.not_before_ms,
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
        not_before_raw = data.get("not_before_ms")
        return cls(
            reservation_id=reservation_id,
            base_url=data.get("base_url", ""),
            mode=mode,
            commit_body=data.get("commit_body"),
            event_fallback_body=data.get("event_fallback_body"),
            recorded_at_ms=int(data.get("recorded_at_ms", 0)),
            not_before_ms=int(not_before_raw) if not_before_raw is not None else None,
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
            _restrict_permissions(self._dir.parent, 0o700)
            _restrict_permissions(self._dir, 0o700)
            target = self._dir / _safe_filename(entry.reservation_id)
            # Unique temp name per writer: concurrent processes may settle
            # the same reservation (replay is idempotent), and a shared
            # temp filename would let one truncate the other mid-write and
            # atomically publish partial JSON.
            tmp = self._dir / f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(entry.to_json(), encoding="utf-8")
                _restrict_permissions(tmp, 0o600)
                tmp.replace(target)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
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
            cutoff = time.time() - 3600
            for tmp in self._dir.glob("*.tmp"):
                # Crashed writers leave unique temp files behind; reap the
                # stale ones so they don't accumulate forever.
                try:
                    if tmp.stat().st_mtime < cutoff:
                        tmp.unlink(missing_ok=True)
                except OSError:
                    pass
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
