#!/usr/bin/env python3
"""Bind shared recovery scenario IDs to native Python SDK behavior tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TESTS = {
    "CR-CORE-001": ("tests/test_retry.py::TestCommitRetryEngine::test_retries_until_success",),
    "CR-CORE-002": ("tests/test_journal.py::TestLifecycleEventFallbackWiring::test_expired_commit_schedules_event",),
    "CR-CORE-003": ("tests/test_lifecycle.py::TestSyncLifecycleExecution::test_heartbeat_exception_does_not_crash",),
    "CR-CORE-004": (
        "tests/test_journal.py::TestLifecycleEventFallbackWiring::"
        "test_protocol_invalid_2xx_is_ambiguous_and_keeps_same_key",
    ),
    "CR-DURABLE-001": (
        "tests/test_journal.py::TestLifecycleEventFallbackWiring::"
        "test_journal_write_precedes_first_commit_and_success_discards",
        "tests/test_journal.py::TestSyncReplay::test_replays_pending_commit_on_set_client",
        "tests/test_retry.py::TestCommitRetryEngine::test_retries_until_success",
    ),
    "CR-DURABLE-002": (
        "tests/test_journal.py::TestSyncEngineDurability::test_expired_then_event_transient_continues_in_event_mode",
        "tests/test_journal.py::TestSyncReplay::test_replays_event_mode_entry",
    ),
    "CR-DURABLE-003": (
        "tests/test_journal.py::TestRateLimitedRetry::test_429_commit_is_transient_and_honors_retry_after",
        "tests/test_journal.py::TestRateLimitedRetry::test_replay_restores_future_retry_after_floor",
        "tests/test_journal.py::TestRateLimitedRetry::test_429_then_success_discards_journal",
    ),
    "CR-DURABLE-004": (
        "tests/test_journal.py::TestAuthFailureRetention::test_replay_survives_api_key_rotation_with_tenant",
    ),
    "CR-DURABLE-005": (
        "tests/test_journal.py::TestCommitJournal::"
        "test_corrupt_and_unsupported_records_are_quarantined_without_blocking_valid",
    ),
    "CR-DURABLE-006": (
        "tests/test_journal.py::TestSyncReplay::test_concurrent_replay_workers_reuse_one_key_and_remove_record",
    ),
    "CR-DURABLE-007": (
        "tests/test_journal.py::TestCommitJournal::test_colliding_legacy_ids_are_distinct_and_migrate_safely",
    ),
    "CR-BOUNDARY-001": (
        "tests/test_lifecycle.py::TestSyncLifecycleExecution::test_missing_actual_surfaces_without_settlement",
    ),
}


def main() -> int:
    if len(sys.argv) != 2:
        print("expected one scenario ID", file=sys.stderr)
        return 2
    scenario = json.load(sys.stdin)
    scenario_id = sys.argv[1]
    if scenario.get("id") != scenario_id or scenario_id not in TESTS:
        print("unknown or mismatched scenario ID", file=sys.stderr)
        return 2
    if "expected_requests" in scenario or "assertions" in scenario:
        print("runner disclosed conformance oracle", file=sys.stderr)
        return 2

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS[scenario_id]],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, file=sys.stderr, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    json.dump(
        {
            "scenario_id": scenario_id,
            "passed": completed.returncode == 0,
            "native_tests": list(TESTS[scenario_id]),
            "diagnostic": f"native pytest exit code {completed.returncode}",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
