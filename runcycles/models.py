"""Pydantic models for the Cycles protocol."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Unit(str, Enum):
    """Cost unit for amounts."""

    USD_MICROCENTS = "USD_MICROCENTS"
    TOKENS = "TOKENS"
    CREDITS = "CREDITS"
    RISK_POINTS = "RISK_POINTS"


class CommitOveragePolicy(str, Enum):
    """Overage policy for commits and events."""

    REJECT = "REJECT"
    ALLOW_IF_AVAILABLE = "ALLOW_IF_AVAILABLE"
    ALLOW_WITH_OVERDRAFT = "ALLOW_WITH_OVERDRAFT"


class Decision(str, Enum):
    """Server decision on a reservation or decision request."""

    ALLOW = "ALLOW"
    ALLOW_WITH_CAPS = "ALLOW_WITH_CAPS"
    DENY = "DENY"


class ReservationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class CommitStatus(str, Enum):
    COMMITTED = "COMMITTED"


class ReleaseStatus(str, Enum):
    RELEASED = "RELEASED"


class ExtendStatus(str, Enum):
    ACTIVE = "ACTIVE"


class EventStatus(str, Enum):
    APPLIED = "APPLIED"


class ErrorCode(str, Enum):
    """Protocol error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_FINALIZED = "RESERVATION_FINALIZED"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    OVERDRAFT_LIMIT_EXCEEDED = "OVERDRAFT_LIMIT_EXCEEDED"
    DEBT_OUTSTANDING = "DEBT_OUTSTANDING"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN = "UNKNOWN"

    @property
    def is_retryable(self) -> bool:
        return self in (ErrorCode.INTERNAL_ERROR, ErrorCode.UNKNOWN)

    @classmethod
    def from_string(cls, value: str | None) -> ErrorCode | None:
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


# --- Core value objects ---

_SNAKE_CASE_CONFIG = ConfigDict(populate_by_name=True)


class Amount(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    unit: Unit
    amount: int


class SignedAmount(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    unit: Unit
    amount: int  # can be negative


class Subject(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    tenant: str | None = None
    workspace: str | None = None
    app: str | None = None
    workflow: str | None = None
    agent: str | None = None
    toolset: str | None = None
    dimensions: dict[str, str] | None = None

    def has_at_least_one_standard_field(self) -> bool:
        return any([self.tenant, self.workspace, self.app, self.workflow, self.agent, self.toolset])


class Action(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    kind: str | None = None
    name: str | None = None
    tags: list[str] | None = None


class Caps(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    max_tokens: int | None = None
    max_steps_remaining: int | None = None
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] | None = None
    cooldown_ms: int | None = None

    def is_tool_allowed(self, tool: str) -> bool:
        if self.tool_denylist and tool in self.tool_denylist:
            return False
        if self.tool_allowlist is not None:
            return tool in self.tool_allowlist
        return True


class CyclesMetrics(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    tokens_input: int | None = None
    tokens_output: int | None = None
    latency_ms: int | None = None
    model_version: str | None = None
    custom: dict[str, Any] | None = None

    def put_custom(self, key: str, value: Any) -> None:
        if self.custom is None:
            self.custom = {}
        self.custom[key] = value

    def is_empty(self) -> bool:
        return (
            self.tokens_input is None
            and self.tokens_output is None
            and self.latency_ms is None
            and self.model_version is None
            and not self.custom
        )


class Balance(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    scope: str | None = None
    scope_path: str | None = None
    remaining: SignedAmount | None = None
    reserved: Amount | None = None
    spent: Amount | None = None
    allocated: Amount | None = None
    debt: Amount | None = None
    overdraft_limit: Amount | None = None
    is_over_limit: bool | None = None


# --- Request models ---


class ReservationCreateRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    subject: Subject | None = None
    action: Action | None = None
    estimate: Amount | None = None
    ttl_ms: int | None = None
    grace_period_ms: int | None = None
    overage_policy: CommitOveragePolicy | None = None
    dry_run: bool | None = None
    metadata: dict[str, Any] | None = None


class CommitRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    actual: Amount | None = None
    metrics: CyclesMetrics | None = None
    metadata: dict[str, Any] | None = None


class ReleaseRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    reason: str | None = None


class ReservationExtendRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    extend_by_ms: int | None = None
    metadata: dict[str, Any] | None = None


class DecisionRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    subject: Subject | None = None
    action: Action | None = None
    estimate: Amount | None = None
    metadata: dict[str, Any] | None = None


class EventCreateRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str | None = None
    subject: Subject | None = None
    action: Action | None = None
    actual: Amount | None = None
    overage_policy: CommitOveragePolicy | None = None
    metrics: CyclesMetrics | None = None
    client_time_ms: int | None = None
    metadata: dict[str, Any] | None = None


# --- Response result models ---


class ReservationResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision | None = None
    reservation_id: str | None = None
    affected_scopes: list[str] | None = None
    expires_at_ms: int | None = None
    scope_path: str | None = None
    reserved: Amount | None = None
    caps: Caps | None = None
    reason_code: str | None = None
    retry_after_ms: int | None = None
    balances: list[Balance] | None = None

    def is_allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CAPS)

    def is_denied(self) -> bool:
        return self.decision == Decision.DENY


class CommitResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: CommitStatus | None = None
    charged: Amount | None = None
    released: Amount | None = None
    balances: list[Balance] | None = None


class ReleaseResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: ReleaseStatus | None = None
    released: Amount | None = None
    balances: list[Balance] | None = None


class ExtendResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: ExtendStatus | None = None
    expires_at_ms: int | None = None
    balances: list[Balance] | None = None


class DecisionResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision | None = None
    caps: Caps | None = None
    reason_code: str | None = None
    retry_after_ms: int | None = None
    affected_scopes: list[str] | None = None

    def is_allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CAPS)

    def is_denied(self) -> bool:
        return self.decision == Decision.DENY


class EventResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: EventStatus | None = None
    event_id: str | None = None
    balances: list[Balance] | None = None


class DryRunResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision | None = None
    caps: Caps | None = None
    affected_scopes: list[str] | None = None
    scope_path: str | None = None
    reserved: Amount | None = None
    balances: list[Balance] | None = None
    reason_code: str | None = None
    retry_after_ms: int | None = None

    def is_allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CAPS)

    def has_caps(self) -> bool:
        return self.caps is not None


class ErrorResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    error: str | None = Field(None, description="Error code string")
    message: str | None = None
    request_id: str | None = None
    details: dict[str, Any] | None = None

    @property
    def error_code(self) -> ErrorCode | None:
        return ErrorCode.from_string(self.error)


class ReservationDetailResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservation_id: str | None = None
    status: ReservationStatus | None = None
    idempotency_key: str | None = None
    subject: Subject | None = None
    action: Action | None = None
    reserved: Amount | None = None
    committed: Amount | None = None
    created_at_ms: int | None = None
    expires_at_ms: int | None = None
    finalized_at_ms: int | None = None
    scope_path: str | None = None
    affected_scopes: list[str] | None = None
    metadata: dict[str, Any] | None = None

    def is_active(self) -> bool:
        return self.status == ReservationStatus.ACTIVE

    def is_committed(self) -> bool:
        return self.status == ReservationStatus.COMMITTED

    def is_released(self) -> bool:
        return self.status == ReservationStatus.RELEASED

    def is_expired(self) -> bool:
        return self.status == ReservationStatus.EXPIRED


class ReservationSummaryResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservation_id: str | None = None
    status: ReservationStatus | None = None
    idempotency_key: str | None = None
    subject: Subject | None = None
    action: Action | None = None
    reserved: Amount | None = None
    created_at_ms: int | None = None
    expires_at_ms: int | None = None
    scope_path: str | None = None
    affected_scopes: list[str] | None = None


class ReservationListResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservations: list[ReservationSummaryResult] | None = None
    has_more: bool | None = None
    next_cursor: str | None = None


class BalanceQueryResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    balances: list[Balance] | None = None
    has_more: bool | None = None
    next_cursor: str | None = None
