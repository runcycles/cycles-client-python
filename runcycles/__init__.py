"""runcycles - Python client for the Cycles budget-management protocol."""

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.context import CyclesContext, get_cycles_context
from runcycles.decorator import cycles, set_default_client, set_default_config
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
from runcycles.models import (
    Action,
    Amount,
    Balance,
    BalanceQueryResult,
    Caps,
    CommitOveragePolicy,
    CommitRequest,
    CommitResult,
    CommitStatus,
    CyclesMetrics,
    Decision,
    DecisionRequest,
    DecisionResult,
    DryRunResult,
    ErrorCode,
    ErrorResponse,
    EventCreateRequest,
    EventResult,
    EventStatus,
    ExtendResult,
    ExtendStatus,
    ReleaseRequest,
    ReleaseResult,
    ReleaseStatus,
    ReservationCreateRequest,
    ReservationDetailResult,
    ReservationExtendRequest,
    ReservationListResult,
    ReservationResult,
    ReservationStatus,
    ReservationSummaryResult,
    SignedAmount,
    Subject,
    Unit,
)
from runcycles.response import CyclesResponse

__all__ = [
    # Client
    "CyclesClient",
    "AsyncCyclesClient",
    # Config
    "CyclesConfig",
    # Decorator
    "cycles",
    "set_default_client",
    "set_default_config",
    # Context
    "CyclesContext",
    "get_cycles_context",
    # Response
    "CyclesResponse",
    # Exceptions
    "CyclesError",
    "CyclesProtocolError",
    "CyclesTransportError",
    "BudgetExceededError",
    "DebtOutstandingError",
    "OverdraftLimitExceededError",
    "ReservationExpiredError",
    "ReservationFinalizedError",
    # Models
    "Unit",
    "Amount",
    "SignedAmount",
    "Subject",
    "Action",
    "Caps",
    "Decision",
    "CyclesMetrics",
    "Balance",
    "CommitOveragePolicy",
    "ErrorCode",
    "ErrorResponse",
    "ReservationStatus",
    "CommitStatus",
    "ReleaseStatus",
    "ExtendStatus",
    "EventStatus",
    "ReservationCreateRequest",
    "CommitRequest",
    "ReleaseRequest",
    "ReservationExtendRequest",
    "DecisionRequest",
    "EventCreateRequest",
    "ReservationResult",
    "CommitResult",
    "ReleaseResult",
    "ExtendResult",
    "DecisionResult",
    "EventResult",
    "DryRunResult",
    "ReservationDetailResult",
    "ReservationSummaryResult",
    "ReservationListResult",
    "BalanceQueryResult",
]

__version__ = "0.1.0"
