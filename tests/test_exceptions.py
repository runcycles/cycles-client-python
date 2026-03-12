"""Tests for exceptions."""

from runcycles.exceptions import (
    BudgetExceededError,
    CyclesError,
    CyclesProtocolError,
    CyclesTransportError,
    OverdraftLimitExceededError,
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


class TestCyclesTransportError:
    def test_basic(self) -> None:
        cause = ConnectionError("refused")
        e = CyclesTransportError("Connection failed", cause=cause)
        assert "Connection failed" in str(e)
        assert e.cause is cause
