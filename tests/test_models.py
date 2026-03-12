"""Tests for Pydantic models."""

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
    ErrorCode,
    ErrorResponse,
    EventCreateRequest,
    ReservationCreateRequest,
    ReservationExtendRequest,
    ReservationResult,
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


class TestReservationResult:
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
        r = ReservationResult.model_validate(data)
        assert r.decision == Decision.ALLOW
        assert r.reservation_id == "res_abc-123"
        assert r.is_allowed()
        assert not r.is_denied()
        assert r.reserved is not None
        assert r.reserved.amount == 500000

    def test_deny(self) -> None:
        data = {"decision": "DENY", "reason_code": "BUDGET_EXCEEDED"}
        r = ReservationResult.model_validate(data)
        assert r.is_denied()
        assert not r.is_allowed()

    def test_allow_with_caps(self) -> None:
        data = {
            "decision": "ALLOW_WITH_CAPS",
            "reservation_id": "res_123",
            "caps": {"max_tokens": 500, "tool_denylist": ["code_exec"]},
        }
        r = ReservationResult.model_validate(data)
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
