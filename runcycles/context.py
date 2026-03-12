"""Thread-safe and async-safe context holder for active Cycles reservations."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from runcycles.models import Amount, Balance, Caps, CyclesMetrics, Decision

_cycles_context_var: ContextVar[CyclesContext | None] = ContextVar("_cycles_context", default=None)


@dataclass
class CyclesContext:
    """Holds the state of an active Cycles reservation.

    Available inside functions decorated with ``@cycles``.
    Use :func:`get_cycles_context` to access.
    """

    reservation_id: str
    estimate: int
    decision: Decision
    caps: Caps | None = None
    expires_at_ms: int | None = None
    affected_scopes: list[str] | None = None
    scope_path: str | None = None
    reserved: Amount | None = None
    balances: list[Balance] | None = None

    # Writable by the guarded function
    metrics: CyclesMetrics | None = None
    commit_metadata: dict[str, Any] | None = None

    def has_caps(self) -> bool:
        return self.caps is not None

    def update_expires_at_ms(self, new_expires_at_ms: int) -> None:
        self.expires_at_ms = new_expires_at_ms


def get_cycles_context() -> CyclesContext | None:
    """Return the current reservation context, or None if not inside a guarded call."""
    return _cycles_context_var.get()


def _set_context(ctx: CyclesContext) -> None:
    _cycles_context_var.set(ctx)


def _clear_context() -> None:
    _cycles_context_var.set(None)
