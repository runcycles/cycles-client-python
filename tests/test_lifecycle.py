"""Tests for lifecycle orchestration logic."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.exceptions import (
    BudgetExceededError,
    CyclesProtocolError,
    DebtOutstandingError,
    OverdraftLimitExceededError,
    ReservationExpiredError,
    ReservationFinalizedError,
)
from runcycles.lifecycle import (
    AsyncCyclesLifecycle,
    CyclesLifecycle,
    DecoratorConfig,
    _build_commit_body,
    _build_extend_body,
    _build_protocol_exception,
    _build_release_body,
    _build_reservation_body,
    _evaluate_actual,
    _evaluate_amount,
)
from runcycles.models import CyclesMetrics
from runcycles.response import CyclesResponse
from runcycles.retry import AsyncCommitRetryEngine, CommitRetryEngine


class TestBuildReservationBody:
    def test_action_defaults_to_unknown(self) -> None:
        """When action_kind and action_name are not provided, they default to 'unknown'."""
        cfg = DecoratorConfig(estimate=1000, tenant="acme")
        body = _build_reservation_body(cfg, 1000, {})
        assert body["action"]["kind"] == "unknown"
        assert body["action"]["name"] == "unknown"

    def test_action_fields_set(self) -> None:
        cfg = DecoratorConfig(
            estimate=1000,
            action_kind="llm.completion",
            action_name="gpt-4",
            action_tags=["prod"],
            tenant="acme",
        )
        body = _build_reservation_body(cfg, 1000, {})
        assert body["action"]["kind"] == "llm.completion"
        assert body["action"]["name"] == "gpt-4"
        assert body["action"]["tags"] == ["prod"]

    def test_validates_estimate_non_negative(self) -> None:
        """Spec: Amount.amount has minimum: 0, so 0 is valid but negative is not."""
        cfg = DecoratorConfig(estimate=0, tenant="acme")
        # 0 should be valid per spec
        body = _build_reservation_body(cfg, 0, {})
        assert body["estimate"]["amount"] == 0

        cfg_neg = DecoratorConfig(estimate=-1, tenant="acme")
        with pytest.raises(ValueError, match="estimate"):
            _build_reservation_body(cfg_neg, -1, {})

    def test_validates_ttl_range(self) -> None:
        cfg = DecoratorConfig(estimate=1000, ttl_ms=500, tenant="acme")
        with pytest.raises(ValueError, match="ttl_ms"):
            _build_reservation_body(cfg, 1000, {})

    def test_validates_subject_has_standard_field(self) -> None:
        cfg = DecoratorConfig(estimate=1000, dimensions={"custom": "val"})
        with pytest.raises(ValueError, match="at least one standard field"):
            _build_reservation_body(cfg, 1000, {})

    def test_validates_grace_period_ms_range(self) -> None:
        cfg = DecoratorConfig(estimate=1000, grace_period_ms=60001, tenant="acme")
        with pytest.raises(ValueError, match="grace_period_ms"):
            _build_reservation_body(cfg, 1000, {})

    def test_validates_grace_period_ms_negative(self) -> None:
        cfg = DecoratorConfig(estimate=1000, grace_period_ms=-1, tenant="acme")
        with pytest.raises(ValueError, match="grace_period_ms"):
            _build_reservation_body(cfg, 1000, {})

    def test_merges_default_subject_fields(self) -> None:
        cfg = DecoratorConfig(estimate=1000, workflow="task-1")
        defaults = {"tenant": "acme", "workspace": "prod"}
        body = _build_reservation_body(cfg, 1000, defaults)
        assert body["subject"]["tenant"] == "acme"
        assert body["subject"]["workspace"] == "prod"
        assert body["subject"]["workflow"] == "task-1"

    def test_decorator_subject_overrides_defaults(self) -> None:
        cfg = DecoratorConfig(estimate=1000, tenant="override")
        defaults = {"tenant": "default-tenant"}
        body = _build_reservation_body(cfg, 1000, defaults)
        assert body["subject"]["tenant"] == "override"

    def test_dry_run_flag(self) -> None:
        cfg = DecoratorConfig(estimate=1000, dry_run=True, tenant="acme")
        body = _build_reservation_body(cfg, 1000, {})
        assert body["dry_run"] is True

    def test_grace_period_included(self) -> None:
        cfg = DecoratorConfig(estimate=1000, grace_period_ms=10000, tenant="acme")
        body = _build_reservation_body(cfg, 1000, {})
        assert body["grace_period_ms"] == 10000


class TestBuildCommitBody:
    def test_basic(self) -> None:
        body = _build_commit_body(500, "USD_MICROCENTS", None, None)
        assert body["actual"]["amount"] == 500
        assert body["actual"]["unit"] == "USD_MICROCENTS"
        assert "metrics" not in body
        assert "metadata" not in body

    def test_with_metrics(self) -> None:
        metrics = CyclesMetrics(tokens_input=100, tokens_output=50)
        body = _build_commit_body(500, "USD_MICROCENTS", metrics, None)
        assert body["metrics"]["tokens_input"] == 100

    def test_with_metadata(self) -> None:
        body = _build_commit_body(500, "USD_MICROCENTS", None, {"source": "test"})
        assert body["metadata"]["source"] == "test"


class TestBuildReleaseBody:
    def test_basic(self) -> None:
        body = _build_release_body("cancelled")
        assert body["reason"] == "cancelled"
        assert "idempotency_key" in body


class TestEvaluateAmount:
    def test_constant(self) -> None:
        assert _evaluate_amount(42, (), {}) == 42

    def test_callable(self) -> None:
        assert _evaluate_amount(lambda x, y: x + y, (3, 4), {}) == 7

    def test_callable_with_kwargs(self) -> None:
        assert _evaluate_amount(lambda x: x * 2, (), {"x": 5}) == 10


class TestEvaluateActual:
    def test_constant(self) -> None:
        assert _evaluate_actual(100, "result", 200, True) == 100

    def test_callable(self) -> None:
        assert _evaluate_actual(lambda r: len(r), "hello", 200, True) == 5

    def test_fallback_to_estimate(self) -> None:
        assert _evaluate_actual(None, "result", 200, True) == 200

    def test_no_fallback_raises(self) -> None:
        with pytest.raises(ValueError, match="actual expression is required"):
            _evaluate_actual(None, "result", 200, False)


class TestBuildProtocolException:
    def test_extracts_error_details(self) -> None:
        response = CyclesResponse.http_error(
            409,
            "Budget exceeded",
            body={
                "error": "BUDGET_EXCEEDED",
                "message": "Insufficient budget",
                "request_id": "req-123",
                "details": {"scope": "tenant:acme", "remaining": 0},
            },
        )
        exc = _build_protocol_exception("Reserve failed", response)
        assert exc.details is not None
        assert exc.details["scope"] == "tenant:acme"
        assert exc.request_id == "req-123"

    def test_maps_to_typed_exception(self) -> None:

        response = CyclesResponse.http_error(
            409,
            "Budget exceeded",
            body={
                "error": "BUDGET_EXCEEDED",
                "message": "No budget",
                "request_id": "req-456",
            },
        )
        exc = _build_protocol_exception("Reserve failed", response)
        assert isinstance(exc, BudgetExceededError)
        assert exc.error_code == "BUDGET_EXCEEDED"

    def test_handles_missing_error_response(self) -> None:
        response = CyclesResponse.http_error(500, "Server error", body=None)
        exc = _build_protocol_exception("Something failed", response)
        assert "Server error" in str(exc)

    def test_extracts_retry_after(self) -> None:
        response = CyclesResponse.http_error(
            409,
            "Denied",
            body={
                "error": "BUDGET_EXCEEDED",
                "message": "Denied",
                "request_id": "req-789",
                "retry_after_ms": 5000,
            },
        )
        exc = _build_protocol_exception("Denied", response)
        assert exc.retry_after_ms == 5000


class TestBuildExtendBody:
    def test_basic(self) -> None:
        body = _build_extend_body(30000)
        assert body["extend_by_ms"] == 30000
        assert "idempotency_key" in body


class TestBuildProtocolExceptionEdgeCases:
    def test_maps_overdraft_limit_exceeded(self) -> None:
        response = CyclesResponse.http_error(
            409, "Over limit",
            body={"error": "OVERDRAFT_LIMIT_EXCEEDED", "message": "Over limit", "request_id": "r1"},
        )
        exc = _build_protocol_exception("Failed", response)
        assert isinstance(exc, OverdraftLimitExceededError)

    def test_maps_debt_outstanding(self) -> None:
        response = CyclesResponse.http_error(
            409, "Debt",
            body={"error": "DEBT_OUTSTANDING", "message": "Debt", "request_id": "r2"},
        )
        exc = _build_protocol_exception("Failed", response)
        assert isinstance(exc, DebtOutstandingError)

    def test_maps_reservation_expired(self) -> None:
        response = CyclesResponse.http_error(
            410, "Expired",
            body={"error": "RESERVATION_EXPIRED", "message": "Expired", "request_id": "r3"},
        )
        exc = _build_protocol_exception("Failed", response)
        assert isinstance(exc, ReservationExpiredError)

    def test_maps_reservation_finalized(self) -> None:
        response = CyclesResponse.http_error(
            409, "Finalized",
            body={"error": "RESERVATION_FINALIZED", "message": "Finalized", "request_id": "r4"},
        )
        exc = _build_protocol_exception("Failed", response)
        assert isinstance(exc, ReservationFinalizedError)

    def test_fallback_when_body_not_error_response(self) -> None:
        """When body has an error field but doesn't parse as ErrorResponse."""
        response = CyclesResponse.http_error(
            500, "Something broke",
            body={"error": "INTERNAL_ERROR"},  # missing required 'message' and 'request_id'
        )
        exc = _build_protocol_exception("Call failed", response)
        assert exc.error_code == "INTERNAL_ERROR"
        assert "Something broke" in str(exc)

    def test_fallback_raw_error_no_error_message(self) -> None:
        """When body has error but response has no error_message."""
        response = CyclesResponse.http_error(500, "", body={"error": "UNKNOWN_CODE"})
        exc = _build_protocol_exception("Prefix", response)
        assert exc.error_code == "UNKNOWN_CODE"


# --- Helper to build a mock sync client ---

def _make_config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878",
        api_key="test-key",
        tenant="acme",
        retry_enabled=False,
        retry_initial_delay=0.001,
        retry_max_delay=0.01,
    )


def _allow_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "ALLOW",
        "reservation_id": "rsv_test",
        "expires_at_ms": int(time.time() * 1000) + 600_000,
        "affected_scopes": ["tenant:acme"],
        "scope_path": "tenant:acme",
        "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


def _deny_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "DENY",
        "affected_scopes": ["tenant:acme"],
        "reason_code": "BUDGET_EXCEEDED",
    })


def _dry_run_allow_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "ALLOW",
        "affected_scopes": ["tenant:acme"],
        "scope_path": "tenant:acme",
        "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


def _dry_run_deny_response() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "decision": "DENY",
        "affected_scopes": ["tenant:acme"],
        "reason_code": "BUDGET_EXCEEDED",
    })


def _commit_success() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "status": "COMMITTED",
        "charged": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


def _release_success() -> CyclesResponse:
    return CyclesResponse.success(200, {
        "status": "RELEASED",
        "released": {"unit": "USD_MICROCENTS", "amount": 1000},
    })


def _make_cfg(**kwargs: object) -> DecoratorConfig:
    defaults: dict[str, object] = {"estimate": 1000, "tenant": "acme", "ttl_ms": 60000}
    defaults.update(kwargs)
    return DecoratorConfig(**defaults)  # type: ignore[arg-type]


class TestSyncLifecycleExecution:
    def _make_lifecycle(self) -> tuple[CyclesLifecycle, MagicMock]:
        config = _make_config()
        mock_client = MagicMock(spec=CyclesClient)
        mock_client._config = config
        retry_engine = CommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = CyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})
        return lifecycle, mock_client

    def test_dry_run_deny_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _dry_run_deny_response()

        cfg = _make_cfg(dry_run=True)

        with pytest.raises(CyclesProtocolError, match="Dry-run denied"):
            lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_dry_run_allow_returns_result(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _dry_run_allow_response()

        cfg = _make_cfg(dry_run=True)
        result = lifecycle.execute(lambda: "should not run", (), {}, cfg)

        from runcycles.models import DryRunResult
        assert isinstance(result, DryRunResult)
        assert result.is_allowed()
        mock_client.commit_reservation.assert_not_called()

    def test_deny_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _deny_response()

        cfg = _make_cfg()

        with pytest.raises(CyclesProtocolError, match="Reservation denied"):
            lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_missing_reservation_id_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = CyclesResponse.success(200, {
            "decision": "ALLOW",
            "affected_scopes": ["tenant:acme"],
            # reservation_id intentionally missing
        })

        cfg = _make_cfg()

        with pytest.raises(CyclesProtocolError, match="reservation_id missing"):
            lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_commit_finalized_does_not_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(
            409, "Finalized",
            body={"error": "RESERVATION_FINALIZED", "message": "Already committed", "request_id": "r1"},
        )

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

        mock_client.release_reservation.assert_not_called()

    def test_commit_expired_does_not_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(
            410, "Expired",
            body={"error": "RESERVATION_EXPIRED", "message": "Expired", "request_id": "r1"},
        )

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

        mock_client.release_reservation.assert_not_called()

    def test_commit_idempotency_mismatch_does_not_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(
            409, "Mismatch",
            body={"error": "IDEMPOTENCY_MISMATCH", "message": "Mismatch", "request_id": "r1"},
        )

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

        mock_client.release_reservation.assert_not_called()

    def test_commit_other_client_error_triggers_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(
            400, "Bad request",
            body={"error": "UNIT_MISMATCH", "message": "Unit mismatch", "request_id": "r1"},
        )
        mock_client.release_reservation.return_value = _release_success()

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

        mock_client.release_reservation.assert_called_once()

    def test_commit_transport_error_schedules_retry(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        # Re-enable retry for this test
        config = _make_config()
        config.retry_enabled = True
        retry_engine = CommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = CyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})

        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.transport_error(
            ConnectionError("network down")
        )

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

        # Transport error should schedule retry (commit_reservation called once by lifecycle,
        # retry engine will call again in background thread)

    def test_commit_server_error_schedules_retry(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        config = _make_config()
        config.retry_enabled = True
        retry_engine = CommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = CyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})

        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse.http_error(500, "Server error")

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_commit_exception_schedules_retry(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        config = _make_config()
        config.retry_enabled = True
        retry_engine = CommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = CyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})

        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.side_effect = RuntimeError("unexpected")

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_release_failure_does_not_raise(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.release_reservation.return_value = CyclesResponse.http_error(500, "Release failed")

        cfg = _make_cfg()

        def failing_fn() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            lifecycle.execute(failing_fn, (), {}, cfg)

    def test_release_exception_does_not_raise(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.release_reservation.side_effect = ConnectionError("network down")

        cfg = _make_cfg()

        def failing_fn() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            lifecycle.execute(failing_fn, (), {}, cfg)

    def test_heartbeat_extends_reservation(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _commit_success()
        mock_client.extend_reservation.return_value = CyclesResponse.success(200, {
            "status": "ACTIVE", "expires_at_ms": 9999999999,
        })

        # Use a very short TTL so heartbeat fires during execution
        cfg = _make_cfg(ttl_ms=2000)

        def slow_fn() -> str:
            time.sleep(1.5)
            return "done"

        result = lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"
        assert mock_client.extend_reservation.call_count >= 1

    def test_heartbeat_failure_does_not_crash(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _commit_success()
        mock_client.extend_reservation.return_value = CyclesResponse.http_error(500, "Extend failed")

        cfg = _make_cfg(ttl_ms=2000)

        def slow_fn() -> str:
            time.sleep(1.5)
            return "done"

        result = lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"

    def test_heartbeat_exception_does_not_crash(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _commit_success()
        mock_client.extend_reservation.side_effect = ConnectionError("network down")

        cfg = _make_cfg(ttl_ms=2000)

        def slow_fn() -> str:
            time.sleep(1.5)
            return "done"

        result = lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"

    def test_commit_unrecognized_response_logged(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = CyclesResponse(status=600)

        cfg = _make_cfg()
        lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_reservation_creation_failure_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = CyclesResponse.http_error(
            500, "Internal error",
            body={"error": "INTERNAL_ERROR", "message": "Server down", "request_id": "r1"},
        )

        cfg = _make_cfg()
        with pytest.raises(CyclesProtocolError, match="Failed to create reservation"):
            lifecycle.execute(lambda: "result", (), {}, cfg)

    def test_heartbeat_skipped_when_ttl_zero(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation.return_value = _allow_response()
        mock_client.commit_reservation.return_value = _commit_success()

        _make_cfg(ttl_ms=1000)
        # We can't easily set ttl_ms=0 since validation rejects it, but we can test
        # the heartbeat path by calling _start_heartbeat directly
        import threading
        stop = threading.Event()
        result = lifecycle._start_heartbeat("rsv_1", 0, MagicMock(), stop)
        assert result is None
        stop.set()


@pytest.mark.asyncio
class TestAsyncLifecycleExecution:
    def _make_lifecycle(self) -> tuple[AsyncCyclesLifecycle, MagicMock]:
        config = _make_config()
        mock_client = MagicMock(spec=AsyncCyclesClient)
        mock_client._config = config
        retry_engine = AsyncCommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = AsyncCyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})
        return lifecycle, mock_client

    async def test_basic_lifecycle(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=_commit_success())

        cfg = _make_cfg()

        async def my_func() -> str:
            return "async result"

        result = await lifecycle.execute(my_func, (), {}, cfg)
        assert result == "async result"
        mock_client.commit_reservation.assert_awaited_once()

    async def test_deny_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_deny_response())

        cfg = _make_cfg()

        async def my_func() -> str:
            return "should not run"

        with pytest.raises(CyclesProtocolError, match="Reservation denied"):
            await lifecycle.execute(my_func, (), {}, cfg)

    async def test_dry_run_deny_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_dry_run_deny_response())

        cfg = _make_cfg(dry_run=True)

        async def my_func() -> str:
            return "should not run"

        with pytest.raises(CyclesProtocolError, match="Dry-run denied"):
            await lifecycle.execute(my_func, (), {}, cfg)

    async def test_dry_run_allow_returns_result(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_dry_run_allow_response())

        cfg = _make_cfg(dry_run=True)

        async def my_func() -> str:
            return "should not run"

        from runcycles.models import DryRunResult
        result = await lifecycle.execute(my_func, (), {}, cfg)
        assert isinstance(result, DryRunResult)

    async def test_missing_reservation_id_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=CyclesResponse.success(200, {
            "decision": "ALLOW",
            "affected_scopes": ["tenant:acme"],
        }))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        with pytest.raises(CyclesProtocolError, match="reservation_id missing"):
            await lifecycle.execute(my_func, (), {}, cfg)

    async def test_function_exception_triggers_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.release_reservation = AsyncMock(return_value=_release_success())

        cfg = _make_cfg()

        async def failing_fn() -> str:
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            await lifecycle.execute(failing_fn, (), {}, cfg)

        mock_client.release_reservation.assert_awaited_once()

    async def test_commit_finalized_does_not_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=CyclesResponse.http_error(
            409, "Finalized",
            body={"error": "RESERVATION_FINALIZED", "message": "Done", "request_id": "r1"},
        ))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)
        mock_client.release_reservation.assert_not_called()

    async def test_commit_idempotency_mismatch_does_not_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=CyclesResponse.http_error(
            409, "Mismatch",
            body={"error": "IDEMPOTENCY_MISMATCH", "message": "Mismatch", "request_id": "r1"},
        ))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)
        mock_client.release_reservation.assert_not_called()

    async def test_commit_client_error_triggers_release(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=CyclesResponse.http_error(
            400, "Bad",
            body={"error": "UNIT_MISMATCH", "message": "Wrong unit", "request_id": "r1"},
        ))
        mock_client.release_reservation = AsyncMock(return_value=_release_success())

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)
        mock_client.release_reservation.assert_awaited_once()

    async def test_commit_transport_error_schedules_retry(self) -> None:
        config = _make_config()
        config.retry_enabled = True
        mock_client = MagicMock(spec=AsyncCyclesClient)
        mock_client._config = config
        retry_engine = AsyncCommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = AsyncCyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})

        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(
            return_value=CyclesResponse.transport_error(ConnectionError("down"))
        )

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)

    async def test_commit_exception_schedules_retry(self) -> None:
        config = _make_config()
        config.retry_enabled = True
        mock_client = MagicMock(spec=AsyncCyclesClient)
        mock_client._config = config
        retry_engine = AsyncCommitRetryEngine(config)
        retry_engine.set_client(mock_client)
        lifecycle = AsyncCyclesLifecycle(mock_client, retry_engine, {"tenant": "acme"})

        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(side_effect=RuntimeError("unexpected"))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)

    async def test_release_failure_does_not_raise(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.release_reservation = AsyncMock(
            return_value=CyclesResponse.http_error(500, "Release failed")
        )

        cfg = _make_cfg()

        async def failing_fn() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await lifecycle.execute(failing_fn, (), {}, cfg)

    async def test_release_exception_does_not_raise(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.release_reservation = AsyncMock(side_effect=ConnectionError("network"))

        cfg = _make_cfg()

        async def failing_fn() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await lifecycle.execute(failing_fn, (), {}, cfg)

    async def test_heartbeat_extends_reservation(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=_commit_success())
        mock_client.extend_reservation = AsyncMock(return_value=CyclesResponse.success(200, {
            "status": "ACTIVE", "expires_at_ms": 9999999999,
        }))

        cfg = _make_cfg(ttl_ms=2000)

        async def slow_fn() -> str:
            await asyncio.sleep(1.5)
            return "done"

        result = await lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"
        assert mock_client.extend_reservation.await_count >= 1

    async def test_heartbeat_failure_does_not_crash(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=_commit_success())
        mock_client.extend_reservation = AsyncMock(
            return_value=CyclesResponse.http_error(500, "Extend failed")
        )

        cfg = _make_cfg(ttl_ms=2000)

        async def slow_fn() -> str:
            await asyncio.sleep(1.5)
            return "done"

        result = await lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"

    async def test_heartbeat_exception_does_not_crash(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=_commit_success())
        mock_client.extend_reservation = AsyncMock(side_effect=ConnectionError("net"))

        cfg = _make_cfg(ttl_ms=2000)

        async def slow_fn() -> str:
            await asyncio.sleep(1.5)
            return "done"

        result = await lifecycle.execute(slow_fn, (), {}, cfg)
        assert result == "done"

    async def test_commit_unrecognized_response_logged(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=_allow_response())
        mock_client.commit_reservation = AsyncMock(return_value=CyclesResponse(status=600))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        await lifecycle.execute(my_func, (), {}, cfg)

    async def test_reservation_creation_failure_raises(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        mock_client.create_reservation = AsyncMock(return_value=CyclesResponse.http_error(
            500, "Internal error",
            body={"error": "INTERNAL_ERROR", "message": "Server down", "request_id": "r1"},
        ))

        cfg = _make_cfg()

        async def my_func() -> str:
            return "result"

        with pytest.raises(CyclesProtocolError, match="Failed to create reservation"):
            await lifecycle.execute(my_func, (), {}, cfg)

    async def test_heartbeat_skipped_when_ttl_zero(self) -> None:
        lifecycle, mock_client = self._make_lifecycle()
        result = lifecycle._start_heartbeat("rsv_1", 0, MagicMock())
        assert result is None
