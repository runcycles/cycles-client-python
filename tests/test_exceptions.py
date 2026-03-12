"""Tests for exceptions."""

from runcycles.exceptions import (
    BudgetExceededError,
    CyclesError,
    CyclesProtocolError,
    CyclesTransportError,
    DebtOutstandingError,
    OverdraftLimitExceededError,
    ReservationExpiredError,
    ReservationFinalizedError,
)


class TestCyclesProtocolError:
    def test_basic(self) -> None:
        e = CyclesProtocolError("test", status=409, error_code="BUDGET_EXCEEDED")
        assert str(e) == "test"
        assert e.status == 409
        assert e.is_budget_exceeded()
        assert not e.is_overdraft_limit_exceeded()

    def test_retryable(self) -> None:
        e = CyclesProtocolError("test", status=500, error_code="INTERNAL_ERROR")
        assert e.is_retryable()

    def test_not_retryable(self) -> None:
        e = CyclesProtocolError("test", status=409, error_code="BUDGET_EXCEEDED")
        assert not e.is_retryable()

    def test_subclass(self) -> None:
        e = BudgetExceededError("budget", status=409, error_code="BUDGET_EXCEEDED")
        assert isinstance(e, CyclesProtocolError)
        assert isinstance(e, CyclesError)

    def test_overdraft(self) -> None:
        e = OverdraftLimitExceededError("overdraft", status=409, error_code="OVERDRAFT_LIMIT_EXCEEDED")
        assert e.is_overdraft_limit_exceeded()

    def test_debt_outstanding(self) -> None:
        e = DebtOutstandingError("debt", status=409, error_code="DEBT_OUTSTANDING")
        assert isinstance(e, CyclesProtocolError)
        assert isinstance(e, CyclesError)
        assert e.is_debt_outstanding()
        assert not e.is_budget_exceeded()

    def test_reservation_expired(self) -> None:
        e = ReservationExpiredError("expired", status=410, error_code="RESERVATION_EXPIRED")
        assert isinstance(e, CyclesProtocolError)
        assert e.is_reservation_expired()
        assert not e.is_retryable()

    def test_reservation_finalized(self) -> None:
        e = ReservationFinalizedError("finalized", status=409, error_code="RESERVATION_FINALIZED")
        assert isinstance(e, CyclesProtocolError)
        assert e.is_reservation_finalized()
        assert not e.is_retryable()

    def test_is_idempotency_mismatch(self) -> None:
        e = CyclesProtocolError("mismatch", status=409, error_code="IDEMPOTENCY_MISMATCH")
        assert e.is_idempotency_mismatch()
        assert not e.is_retryable()

    def test_is_unit_mismatch(self) -> None:
        e = CyclesProtocolError("unit mismatch", status=400, error_code="UNIT_MISMATCH")
        assert e.is_unit_mismatch()
        assert not e.is_retryable()

    def test_retryable_unknown(self) -> None:
        e = CyclesProtocolError("unknown", status=500, error_code="UNKNOWN")
        assert e.is_retryable()

    def test_retryable_5xx_without_known_code(self) -> None:
        e = CyclesProtocolError("server error", status=502)
        assert e.is_retryable()

    def test_retry_after_ms(self) -> None:
        e = CyclesProtocolError("test", status=409, error_code="BUDGET_EXCEEDED", retry_after_ms=5000)
        assert e.retry_after_ms == 5000

    def test_request_id(self) -> None:
        e = CyclesProtocolError("test", status=500, request_id="req-123")
        assert e.request_id == "req-123"


class TestCyclesTransportError:
    def test_basic(self) -> None:
        cause = ConnectionError("refused")
        e = CyclesTransportError("Connection failed", cause=cause)
        assert "Connection failed" in str(e)
        assert e.cause is cause
