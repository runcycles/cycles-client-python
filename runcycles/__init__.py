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
    CyclesMetrics,
    Decision,
    DecisionRequest,
    DecisionResult,
    DryRunResult,
    ErrorCode,
    EventCreateRequest,
    EventResult,
    ReservationCreateRequest,
    ReservationDetailResult,
    ReservationExtendRequest,
    ReservationListResult,
    ReservationResult,
    ReleaseRequest,
    ReleaseResult,
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
    "OverdraftLimitExceededError",
    "ReservationExpiredError",
    "ReservationFinalizedError",
    # Models
    "Unit",
    "Amount",
    "Subject",
    "Action",
    "Caps",
    "Decision",
    "CyclesMetrics",
    "Balance",
    "CommitOveragePolicy",
    "ErrorCode",
    "ReservationCreateRequest",
    "CommitRequest",
    "ReleaseRequest",
    "ReservationExtendRequest",
    "DecisionRequest",
    "EventCreateRequest",
    "ReservationResult",
    "CommitResult",
    "ReleaseResult",
    "DecisionResult",
    "EventResult",
    "DryRunResult",
    "ReservationDetailResult",
    "ReservationListResult",
    "BalanceQueryResult",
]

__version__ = "0.1.0"
