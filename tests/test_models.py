"""Tests for Pydantic models."""

import pytest
from pydantic import ValidationError

from runcycles.models import (
    Action,
    Amount,
    Balance,
    Caps,
    CommitOveragePolicy,
    CommitRequest,
    CyclesMetrics,
    Decision,
    DecisionRequest,
    DecisionResponse,
    DryRunResult,
    ErrorCode,
    ErrorResponse,
    EventCreateRequest,
    ReservationCreateRequest,
    ReservationDetail,
    ReservationExtendRequest,
    ReservationCreateResponse,
    ReservationStatus,
    ReleaseRequest,
    SignedAmount,
    Subject,
    Unit,
)


class TestAmount:
    def test_create(self) -> None:
        a = Amount(unit=Unit.USD_MICROCENTS, amount=1000)
        assert a.unit == Unit.USD_MICROCENTS
        assert a.amount == 1000

    def test_serialize(self) -> None:
        a = Amount(unit=Unit.USD_MICROCENTS, amount=500)
        d = a.model_dump()
        assert d == {"unit": "USD_MICROCENTS", "amount": 500}

    def test_deserialize(self) -> None:
        a = Amount.model_validate({"unit": "TOKENS", "amount": 42})
        assert a.unit == Unit.TOKENS
        assert a.amount == 42


class TestSubject:
    def test_standard_field_check(self) -> None:
        s = Subject(tenant="acme")
        assert s.has_at_least_one_standard_field()

    def test_no_standard_field(self) -> None:
        s = Subject(dimensions={"foo": "bar"})
        assert not s.has_at_least_one_standard_field()

    def test_serialize_excludes_none(self) -> None:
        s = Subject(tenant="acme", agent="bot")
        d = s.model_dump(exclude_none=True)
        assert d == {"tenant": "acme", "agent": "bot"}
        assert "workspace" not in d


class TestCaps:
    def test_tool_allowed_with_allowlist(self) -> None:
        c = Caps(tool_allowlist=["search", "calc"])
        assert c.is_tool_allowed("search")
        assert not c.is_tool_allowed("code_exec")

    def test_tool_denied_with_denylist(self) -> None:
        c = Caps(tool_denylist=["code_exec"])
        assert c.is_tool_allowed("search")
        assert not c.is_tool_allowed("code_exec")

    def test_tool_allowed_no_lists(self) -> None:
        c = Caps()
        assert c.is_tool_allowed("anything")


class TestCyclesMetrics:
    def test_is_empty(self) -> None:
        m = CyclesMetrics()
        assert m.is_empty()

    def test_not_empty(self) -> None:
        m = CyclesMetrics(tokens_input=100)
        assert not m.is_empty()

    def test_put_custom(self) -> None:
        m = CyclesMetrics()
        m.put_custom("cache_hit", True)
        assert m.custom == {"cache_hit": True}
        assert not m.is_empty()


class TestReservationCreateRequest:
    def test_serialize(self) -> None:
        r = ReservationCreateRequest(
            idempotency_key="req-001",
            subject=Subject(tenant="acme"),
            action=Action(kind="llm.completion", name="gpt-4"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ttl_ms=30000,
            overage_policy=CommitOveragePolicy.REJECT,
        )
        d = r.model_dump(exclude_none=True)
        assert d["idempotency_key"] == "req-001"
        assert d["subject"]["tenant"] == "acme"
        assert d["estimate"]["amount"] == 1000
        assert d["ttl_ms"] == 30000


class TestCommitRequest:
    def test_serialize(self) -> None:
        r = CommitRequest(
            idempotency_key="commit-001",
            actual=Amount(unit=Unit.USD_MICROCENTS, amount=800),
            metrics=CyclesMetrics(tokens_input=100, tokens_output=50),
        )
        d = r.model_dump(exclude_none=True)
        assert d["actual"]["amount"] == 800
        assert d["metrics"]["tokens_input"] == 100


class TestReservationCreateResponse:
    def test_from_server_response(self) -> None:
        data = {
            "decision": "ALLOW",
            "reservation_id": "res_abc-123",
            "expires_at_ms": 1710000060000,
            "affected_scopes": ["tenant:acme"],
            "scope_path": "tenant:acme",
            "reserved": {"unit": "USD_MICROCENTS", "amount": 500000},
            "caps": None,
            "reason_code": None,
            "retry_after_ms": None,
        }
        r = ReservationCreateResponse.model_validate(data)
        assert r.decision == Decision.ALLOW
        assert r.reservation_id == "res_abc-123"
        assert r.is_allowed()
        assert not r.is_denied()
        assert r.reserved is not None
        assert r.reserved.amount == 500000

    def test_deny(self) -> None:
        data = {"decision": "DENY", "reason_code": "BUDGET_EXCEEDED", "affected_scopes": ["tenant:acme"]}
        r = ReservationCreateResponse.model_validate(data)
        assert r.is_denied()
        assert not r.is_allowed()

    def test_allow_with_caps(self) -> None:
        data = {
            "decision": "ALLOW_WITH_CAPS",
            "reservation_id": "res_123",
            "affected_scopes": ["tenant:acme"],
            "caps": {"max_tokens": 500, "tool_denylist": ["code_exec"]},
        }
        r = ReservationCreateResponse.model_validate(data)
        assert r.decision == Decision.ALLOW_WITH_CAPS
        assert r.caps is not None
        assert r.caps.max_tokens == 500


class TestErrorResponse:
    def test_parse(self) -> None:
        data = {
            "error": "BUDGET_EXCEEDED",
            "message": "Insufficient budget",
            "request_id": "req-123",
        }
        e = ErrorResponse.model_validate(data)
        assert e.error_code == ErrorCode.BUDGET_EXCEEDED
        assert e.message == "Insufficient budget"

    def test_unknown_error(self) -> None:
        data = {"error": "SOME_FUTURE_ERROR", "message": "Something new", "request_id": "req-456"}
        e = ErrorResponse.model_validate(data)
        assert e.error_code == ErrorCode.UNKNOWN


class TestErrorCode:
    def test_retryable(self) -> None:
        assert ErrorCode.INTERNAL_ERROR.is_retryable
        assert ErrorCode.UNKNOWN.is_retryable
        assert not ErrorCode.BUDGET_EXCEEDED.is_retryable

    def test_from_string(self) -> None:
        assert ErrorCode.from_string("BUDGET_EXCEEDED") == ErrorCode.BUDGET_EXCEEDED
        assert ErrorCode.from_string("NONSENSE") == ErrorCode.UNKNOWN
        assert ErrorCode.from_string(None) is None


class TestAmountValidation:
    def test_rejects_negative_amount(self) -> None:
        with pytest.raises(ValidationError):
            Amount(unit=Unit.USD_MICROCENTS, amount=-1)

    def test_allows_zero_amount(self) -> None:
        a = Amount(unit=Unit.USD_MICROCENTS, amount=0)
        assert a.amount == 0

    def test_signed_amount_allows_negative(self) -> None:
        s = SignedAmount(unit=Unit.USD_MICROCENTS, amount=-500)
        assert s.amount == -500


class TestCapsToolPrecedence:
    def test_allowlist_takes_precedence_over_denylist(self) -> None:
        """Spec: If tool_allowlist is non-empty, ONLY those tools are allowed (denylist ignored)."""
        c = Caps(tool_allowlist=["search", "calc"], tool_denylist=["search"])
        # search is on both lists — allowlist takes precedence
        assert c.is_tool_allowed("search")
        assert not c.is_tool_allowed("code_exec")

    def test_empty_allowlist_blocks_all(self) -> None:
        """Empty allowlist means no tools are allowed."""
        c = Caps(tool_allowlist=[])
        assert not c.is_tool_allowed("anything")


class TestDryRunResult:
    def test_is_allowed(self) -> None:
        r = DryRunResult(decision=Decision.ALLOW)
        assert r.is_allowed()
        assert not r.is_denied()

    def test_is_denied(self) -> None:
        r = DryRunResult(decision=Decision.DENY, reason_code="BUDGET_EXCEEDED")
        assert r.is_denied()
        assert not r.is_allowed()

    def test_has_caps(self) -> None:
        r = DryRunResult(decision=Decision.ALLOW_WITH_CAPS, caps=Caps(max_tokens=100))
        assert r.has_caps()
        assert r.is_allowed()


def _make_detail(status: ReservationStatus) -> ReservationDetail:
    """Helper to build a ReservationDetail with all required fields."""
    return ReservationDetail(
        reservation_id="rsv_1",
        status=status,
        subject=Subject(tenant="acme"),
        action=Action(kind="test", name="test"),
        reserved=Amount(unit=Unit.USD_MICROCENTS, amount=100),
        created_at_ms=1000000,
        expires_at_ms=2000000,
        scope_path="tenant:acme",
        affected_scopes=["tenant:acme"],
    )


class TestReservationDetail:
    def test_is_active(self) -> None:
        r = _make_detail(ReservationStatus.ACTIVE)
        assert r.is_active()
        assert not r.is_committed()
        assert not r.is_released()
        assert not r.is_expired()

    def test_is_committed(self) -> None:
        r = _make_detail(ReservationStatus.COMMITTED)
        assert r.is_committed()

    def test_is_released(self) -> None:
        r = _make_detail(ReservationStatus.RELEASED)
        assert r.is_released()

    def test_is_expired(self) -> None:
        r = _make_detail(ReservationStatus.EXPIRED)
        assert r.is_expired()


class TestDecisionResponse:
    def test_allow(self) -> None:
        r = DecisionResponse(decision=Decision.ALLOW)
        assert r.is_allowed()
        assert not r.is_denied()

    def test_deny(self) -> None:
        r = DecisionResponse(decision=Decision.DENY, reason_code="BUDGET_EXCEEDED")
        assert r.is_denied()
        assert not r.is_allowed()

    def test_allow_with_caps(self) -> None:
        r = DecisionResponse(decision=Decision.ALLOW_WITH_CAPS, caps=Caps(max_tokens=500))
        assert r.is_allowed()


class TestRequiredFields:
    """Validate spec-mandated required fields are enforced."""

    def test_reservation_result_requires_decision(self) -> None:
        with pytest.raises(ValidationError):
            ReservationCreateResponse.model_validate({"affected_scopes": ["tenant:acme"]})

    def test_reservation_result_requires_affected_scopes(self) -> None:
        with pytest.raises(ValidationError):
            ReservationCreateResponse.model_validate({"decision": "ALLOW"})

    def test_commit_result_requires_status_and_charged(self) -> None:
        from runcycles.models import CommitResponse
        with pytest.raises(ValidationError):
            CommitResponse.model_validate({"status": "COMMITTED"})
        with pytest.raises(ValidationError):
            CommitResponse.model_validate({"charged": {"unit": "USD_MICROCENTS", "amount": 100}})

    def test_release_result_requires_status_and_released(self) -> None:
        from runcycles.models import ReleaseResponse
        with pytest.raises(ValidationError):
            ReleaseResponse.model_validate({"status": "RELEASED"})
        with pytest.raises(ValidationError):
            ReleaseResponse.model_validate({"released": {"unit": "USD_MICROCENTS", "amount": 100}})

    def test_extend_result_requires_status_and_expires(self) -> None:
        from runcycles.models import ReservationExtendResponse
        with pytest.raises(ValidationError):
            ReservationExtendResponse.model_validate({"status": "ACTIVE"})
        with pytest.raises(ValidationError):
            ReservationExtendResponse.model_validate({"expires_at_ms": 9999999999})

    def test_event_result_requires_status_and_event_id(self) -> None:
        from runcycles.models import EventCreateResponse
        with pytest.raises(ValidationError):
            EventCreateResponse.model_validate({"status": "APPLIED"})
        with pytest.raises(ValidationError):
            EventCreateResponse.model_validate({"event_id": "evt_123"})

    def test_balance_requires_scope_scope_path_remaining(self) -> None:
        with pytest.raises(ValidationError):
            Balance.model_validate({"scope_path": "/", "remaining": {"unit": "USD_MICROCENTS", "amount": 100}})
        with pytest.raises(ValidationError):
            Balance.model_validate({"scope": "tenant:acme", "remaining": {"unit": "USD_MICROCENTS", "amount": 100}})
        with pytest.raises(ValidationError):
            Balance.model_validate({"scope": "tenant:acme", "scope_path": "/"})

    def test_decision_result_requires_decision(self) -> None:
        with pytest.raises(ValidationError):
            DecisionResponse.model_validate({})


class TestFieldConstraints:
    """Validate spec-mandated field constraints are enforced."""

    def test_subject_tenant_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Subject(tenant="x" * 129)

    def test_subject_tenant_at_max_length(self) -> None:
        s = Subject(tenant="x" * 128)
        assert len(s.tenant) == 128

    def test_subject_dimensions_max_entries(self) -> None:
        dims = {f"k{i}": "v" for i in range(17)}
        with pytest.raises(ValidationError):
            Subject(tenant="acme", dimensions=dims)

    def test_subject_dimension_value_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Subject(tenant="acme", dimensions={"key": "v" * 257})

    def test_action_kind_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Action(kind="x" * 65, name="ok")

    def test_action_name_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Action(kind="ok", name="x" * 257)

    def test_action_tags_max_items(self) -> None:
        with pytest.raises(ValidationError):
            Action(kind="ok", name="ok", tags=[f"t{i}" for i in range(11)])

    def test_action_tag_item_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Action(kind="ok", name="ok", tags=["x" * 65])

    def test_caps_max_tokens_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            Caps(max_tokens=-1)

    def test_caps_max_steps_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            Caps(max_steps_remaining=-1)

    def test_caps_cooldown_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            Caps(cooldown_ms=-1)

    def test_metrics_tokens_input_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            CyclesMetrics(tokens_input=-1)

    def test_metrics_tokens_output_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            CyclesMetrics(tokens_output=-1)

    def test_metrics_latency_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            CyclesMetrics(latency_ms=-1)

    def test_metrics_model_version_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CyclesMetrics(model_version="x" * 129)

    def test_idempotency_key_min_length(self) -> None:
        with pytest.raises(ValidationError):
            ReservationCreateRequest(
                idempotency_key="",
                subject=Subject(tenant="acme"),
                action=Action(kind="test", name="test"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=100),
            )

    def test_idempotency_key_max_length(self) -> None:
        with pytest.raises(ValidationError):
            ReservationCreateRequest(
                idempotency_key="x" * 257,
                subject=Subject(tenant="acme"),
                action=Action(kind="test", name="test"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=100),
            )

    def test_extend_by_ms_min(self) -> None:
        with pytest.raises(ValidationError):
            ReservationExtendRequest(idempotency_key="key-1", extend_by_ms=0)

    def test_extend_by_ms_max(self) -> None:
        with pytest.raises(ValidationError):
            ReservationExtendRequest(idempotency_key="key-1", extend_by_ms=86_400_001)

    def test_release_reason_max_length(self) -> None:
        with pytest.raises(ValidationError):
            ReleaseRequest(idempotency_key="key-1", reason="x" * 257)

    def test_reservation_detail_requires_all_spec_fields(self) -> None:
        """ReservationDetail spec requires many fields."""
        with pytest.raises(ValidationError):
            ReservationDetail(reservation_id="rsv_1", status=ReservationStatus.ACTIVE)

    def test_reservation_list_requires_reservations(self) -> None:
        from runcycles.models import ReservationListResponse
        with pytest.raises(ValidationError):
            ReservationListResponse.model_validate({})

    def test_balance_query_requires_balances(self) -> None:
        from runcycles.models import BalanceResponse
        with pytest.raises(ValidationError):
            BalanceResponse.model_validate({})
