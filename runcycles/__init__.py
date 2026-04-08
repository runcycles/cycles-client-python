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
    BalanceResponse,
    Caps,
    CommitOveragePolicy,
    CommitRequest,
    CommitResponse,
    CommitStatus,
    CyclesMetrics,
    Decision,
    DecisionRequest,
    DecisionResponse,
    DryRunResult,
    ErrorCode,
    ErrorResponse,
    EventCreateRequest,
    EventCreateResponse,
    EventStatus,
    ExtendStatus,
    ReleaseRequest,
    ReleaseResponse,
    ReleaseStatus,
    ReservationCreateRequest,
    ReservationCreateResponse,
    ReservationDetail,
    ReservationExtendRequest,
    ReservationExtendResponse,
    ReservationListResponse,
    ReservationStatus,
    ReservationSummary,
    SignedAmount,
    Subject,
    Unit,
)
from runcycles.response import CyclesResponse
from runcycles.streaming import AsyncStreamReservation, StreamReservation, StreamUsage

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
    # Streaming
    "StreamReservation",
    "AsyncStreamReservation",
    "StreamUsage",
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
    "ReservationCreateResponse",
    "CommitResponse",
    "ReleaseResponse",
    "ReservationExtendResponse",
    "DecisionResponse",
    "EventCreateResponse",
    "DryRunResult",
    "ReservationDetail",
    "ReservationSummary",
    "ReservationListResponse",
    "BalanceResponse",
]

from importlib.metadata import version as _metadata_version

__version__ = _metadata_version("runcycles")
