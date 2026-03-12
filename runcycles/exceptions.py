"""Exception hierarchy for the Cycles client."""

from __future__ import annotations

from typing import Any


class CyclesError(Exception):
    """Base exception for all Cycles client errors."""


class CyclesProtocolError(CyclesError):
    """Raised when the Cycles server returns a protocol-level error."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        error_code: str | None = None,
        reason_code: str | None = None,
        retry_after_ms: int | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.reason_code = reason_code
        self.retry_after_ms = retry_after_ms
        self.request_id = request_id
        self.details = details

    def is_budget_exceeded(self) -> bool:
        return self.error_code == "BUDGET_EXCEEDED"

    def is_overdraft_limit_exceeded(self) -> bool:
        return self.error_code == "OVERDRAFT_LIMIT_EXCEEDED"

    def is_debt_outstanding(self) -> bool:
        return self.error_code == "DEBT_OUTSTANDING"

    def is_reservation_expired(self) -> bool:
        return self.error_code == "RESERVATION_EXPIRED"

    def is_reservation_finalized(self) -> bool:
        return self.error_code == "RESERVATION_FINALIZED"

    def is_idempotency_mismatch(self) -> bool:
        return self.error_code == "IDEMPOTENCY_MISMATCH"

    def is_unit_mismatch(self) -> bool:
        return self.error_code == "UNIT_MISMATCH"

    def is_retryable(self) -> bool:
        return self.error_code in ("INTERNAL_ERROR", "UNKNOWN") or self.status >= 500


class BudgetExceededError(CyclesProtocolError):
    """Raised when budget is insufficient for the reservation."""


class OverdraftLimitExceededError(CyclesProtocolError):
    """Raised when debt exceeds the overdraft limit."""


class DebtOutstandingError(CyclesProtocolError):
    """Raised when outstanding debt blocks new reservations."""


class ReservationExpiredError(CyclesProtocolError):
    """Raised when operating on an expired reservation."""


class ReservationFinalizedError(CyclesProtocolError):
    """Raised when operating on an already-finalized reservation."""


class CyclesTransportError(CyclesError):
    """Raised when a transport-level error occurs (connection timeout, DNS failure, etc.)."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause
