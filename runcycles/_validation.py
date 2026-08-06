"""Input validation utilities."""

from __future__ import annotations

from runcycles.models import Subject


def validate_subject(subject: Subject | None) -> None:
    """Validate that a subject has at least one standard field."""
    if subject is not None and not subject.has_at_least_one_standard_field():
        raise ValueError(
            "Subject must have at least one standard field"
            " (tenant, workspace, app, workflow, agent, or toolset)"
        )


def validate_reservation_id(reservation_id: str | None) -> None:
    """Validate that a reservation ID is non-empty."""
    if not reservation_id:
        raise ValueError("reservation_id is required and must be non-empty")


def validate_non_negative(value: int, name: str) -> None:
    """Validate a protocol Amount integer in its non-negative int64 range."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 9_223_372_036_854_775_807:
        raise ValueError(f"{name} must be a non-negative int64, got {value!r}")


def validate_ttl_ms(ttl_ms: int) -> None:
    """Validate TTL is within allowed range (1s to 24h)."""
    if ttl_ms < 1000 or ttl_ms > 86_400_000:
        raise ValueError(f"ttl_ms must be between 1000 and 86400000, got {ttl_ms}")


def validate_extend_by_ms(extend_by_ms: int) -> None:
    """Validate extend_by_ms is within allowed range (1ms to 24h)."""
    if extend_by_ms < 1 or extend_by_ms > 86_400_000:
        raise ValueError(f"extend_by_ms must be between 1 and 86400000, got {extend_by_ms}")


def validate_grace_period_ms(grace_period_ms: int | None) -> None:
    """Validate grace period is within allowed range (0 to 60s)."""
    if grace_period_ms is not None and (grace_period_ms < 0 or grace_period_ms > 60_000):
        raise ValueError(f"grace_period_ms must be between 0 and 60000, got {grace_period_ms}")
