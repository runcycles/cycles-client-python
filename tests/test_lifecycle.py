"""Tests for lifecycle orchestration logic."""

import pytest

from runcycles.lifecycle import (
    DecoratorConfig,
    _build_commit_body,
    _build_protocol_exception,
    _build_release_body,
    _build_reservation_body,
    _evaluate_actual,
    _evaluate_amount,
)
from runcycles.models import CyclesMetrics
from runcycles.response import CyclesResponse


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
        from runcycles.exceptions import BudgetExceededError

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
