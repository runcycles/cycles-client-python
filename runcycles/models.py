"""Pydantic models for the Cycles protocol."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

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
    BUDGET_FROZEN = "BUDGET_FROZEN"
    BUDGET_CLOSED = "BUDGET_CLOSED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_FINALIZED = "RESERVATION_FINALIZED"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    OVERDRAFT_LIMIT_EXCEEDED = "OVERDRAFT_LIMIT_EXCEEDED"
    DEBT_OUTSTANDING = "DEBT_OUTSTANDING"
    MAX_EXTENSIONS_EXCEEDED = "MAX_EXTENSIONS_EXCEEDED"
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
    amount: int = Field(ge=0)


class SignedAmount(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    unit: Unit
    amount: int  # can be negative


class Subject(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    tenant: Annotated[str, Field(max_length=128)] | None = None
    workspace: Annotated[str, Field(max_length=128)] | None = None
    app: Annotated[str, Field(max_length=128)] | None = None
    workflow: Annotated[str, Field(max_length=128)] | None = None
    agent: Annotated[str, Field(max_length=128)] | None = None
    toolset: Annotated[str, Field(max_length=128)] | None = None
    dimensions: dict[str, Annotated[str, Field(max_length=256)]] | None = Field(default=None, max_length=16)

    def has_at_least_one_standard_field(self) -> bool:
        return any([self.tenant, self.workspace, self.app, self.workflow, self.agent, self.toolset])


class Action(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    kind: str = Field(max_length=64)
    name: str = Field(max_length=256)
    tags: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=10)


class Caps(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    max_tokens: Annotated[int, Field(ge=0)] | None = None
    max_steps_remaining: Annotated[int, Field(ge=0)] | None = None
    tool_allowlist: list[Annotated[str, Field(max_length=256)]] | None = None
    tool_denylist: list[Annotated[str, Field(max_length=256)]] | None = None
    cooldown_ms: Annotated[int, Field(ge=0)] | None = None

    def is_tool_allowed(self, tool: str) -> bool:
        if self.tool_allowlist is not None:
            return tool in self.tool_allowlist
        if self.tool_denylist and tool in self.tool_denylist:
            return False
        return True


class CyclesMetrics(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    tokens_input: Annotated[int, Field(ge=0)] | None = None
    tokens_output: Annotated[int, Field(ge=0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    model_version: Annotated[str, Field(max_length=128)] | None = None
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

    scope: str
    scope_path: str
    remaining: SignedAmount
    reserved: Amount | None = None
    spent: Amount | None = None
    allocated: Amount | None = None
    debt: Amount | None = None
    overdraft_limit: Amount | None = None
    is_over_limit: bool | None = None


# --- Request models ---


class ReservationCreateRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    subject: Subject
    action: Action
    estimate: Amount
    ttl_ms: int | None = None
    grace_period_ms: int | None = None
    overage_policy: CommitOveragePolicy | None = None
    dry_run: bool | None = None
    metadata: dict[str, Any] | None = None


class CommitRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    actual: Amount
    metrics: CyclesMetrics | None = None
    metadata: dict[str, Any] | None = None


class ReleaseRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: Annotated[str, Field(max_length=256)] | None = None


class ReservationExtendRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    extend_by_ms: int = Field(ge=1, le=86_400_000)
    metadata: dict[str, Any] | None = None


class DecisionRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    subject: Subject
    action: Action
    estimate: Amount
    metadata: dict[str, Any] | None = None


class EventCreateRequest(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    idempotency_key: str = Field(min_length=1, max_length=256)
    subject: Subject
    action: Action
    actual: Amount
    overage_policy: CommitOveragePolicy | None = None
    metrics: CyclesMetrics | None = None
    client_time_ms: int | None = None
    metadata: dict[str, Any] | None = None


# --- Response result models ---


class ReservationCreateResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision
    reservation_id: str | None = None
    affected_scopes: list[str]
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


class CommitResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: CommitStatus
    charged: Amount
    released: Amount | None = None
    balances: list[Balance] | None = None


class ReleaseResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: ReleaseStatus
    released: Amount
    balances: list[Balance] | None = None


class ReservationExtendResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: ExtendStatus
    expires_at_ms: int
    balances: list[Balance] | None = None


class DecisionResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision
    caps: Caps | None = None
    reason_code: str | None = None
    retry_after_ms: int | None = None
    affected_scopes: list[str] | None = None

    def is_allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CAPS)

    def is_denied(self) -> bool:
        return self.decision == Decision.DENY


class EventCreateResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    status: EventStatus
    event_id: str
    charged: Amount | None = None
    balances: list[Balance] | None = None


class DryRunResult(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    decision: Decision
    caps: Caps | None = None
    affected_scopes: list[str] | None = None
    scope_path: str | None = None
    reserved: Amount | None = None
    balances: list[Balance] | None = None
    reason_code: str | None = None
    retry_after_ms: int | None = None

    def is_allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CAPS)

    def is_denied(self) -> bool:
        return self.decision == Decision.DENY

    def has_caps(self) -> bool:
        return self.caps is not None


class ErrorResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    error: str = Field(..., description="Error code string")
    message: str
    request_id: str
    details: dict[str, Any] | None = None

    @property
    def error_code(self) -> ErrorCode | None:
        return ErrorCode.from_string(self.error)


class ReservationDetail(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservation_id: str
    status: ReservationStatus
    subject: Subject
    action: Action
    reserved: Amount
    created_at_ms: int
    expires_at_ms: int
    scope_path: str
    affected_scopes: list[str]
    idempotency_key: str | None = None
    committed: Amount | None = None
    finalized_at_ms: int | None = None
    metadata: dict[str, Any] | None = None

    def is_active(self) -> bool:
        return self.status == ReservationStatus.ACTIVE

    def is_committed(self) -> bool:
        return self.status == ReservationStatus.COMMITTED

    def is_released(self) -> bool:
        return self.status == ReservationStatus.RELEASED

    def is_expired(self) -> bool:
        return self.status == ReservationStatus.EXPIRED


class ReservationSummary(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservation_id: str
    status: ReservationStatus
    subject: Subject
    action: Action
    reserved: Amount
    created_at_ms: int
    expires_at_ms: int
    scope_path: str
    affected_scopes: list[str]
    idempotency_key: str | None = None


class ReservationListResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    reservations: list[ReservationSummary]
    has_more: bool | None = None
    next_cursor: str | None = None


class BalanceResponse(BaseModel):
    model_config = _SNAKE_CASE_CONFIG

    balances: list[Balance]
    has_more: bool | None = None
    next_cursor: str | None = None
