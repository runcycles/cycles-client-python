"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_default_client() -> None:
    """Reset the module-level default client before each test."""
    import runcycles.decorator as dec
    dec._default_client = None
    dec._default_config = None


@pytest.fixture(autouse=True)
def _isolate_commit_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the default commit journal at a per-test temp dir and reset replay state.

    Without this, any engine built from a default CyclesConfig would write
    journal files into the real ``~/.runcycles`` during tests.
    """
    import runcycles.journal as journal_mod
    import runcycles.retry as retry_mod

    monkeypatch.setattr(journal_mod, "default_journal_dir", lambda: tmp_path / "commit-journal")
    with retry_mod._replay_lock:
        retry_mod._replayed_dirs.clear()
