"""Tests for input validation utilities."""

import pytest

from runcycles._validation import (
    validate_positive,
    validate_reservation_id,
    validate_subject,
    validate_ttl_ms,
)
from runcycles.models import Subject


class TestValidateSubject:
    def test_valid_subject(self) -> None:
        validate_subject(Subject(tenant="acme"))

    def test_none_subject(self) -> None:
        # None is acceptable (no subject to validate)
        validate_subject(None)

    def test_subject_with_only_dimensions_is_invalid(self) -> None:
        with pytest.raises(ValueError, match="at least one standard field"):
            validate_subject(Subject(dimensions={"foo": "bar"}))

    def test_subject_with_any_standard_field(self) -> None:
        for field in ("tenant", "workspace", "app", "workflow", "agent", "toolset"):
            validate_subject(Subject(**{field: "value"}))


class TestValidateReservationId:
    def test_valid_id(self) -> None:
        validate_reservation_id("rsv_123")

    def test_empty_id(self) -> None:
        with pytest.raises(ValueError, match="reservation_id"):
            validate_reservation_id("")

    def test_none_id(self) -> None:
        with pytest.raises(ValueError, match="reservation_id"):
            validate_reservation_id(None)


class TestValidatePositive:
    def test_valid(self) -> None:
        validate_positive(1, "amount")

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="amount"):
            validate_positive(0, "amount")

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="estimate"):
            validate_positive(-1, "estimate")


class TestValidateTtlMs:
    def test_valid_range(self) -> None:
        validate_ttl_ms(1000)
        validate_ttl_ms(60_000)
        validate_ttl_ms(86_400_000)

    def test_below_minimum(self) -> None:
        with pytest.raises(ValueError, match="ttl_ms"):
            validate_ttl_ms(999)

    def test_above_maximum(self) -> None:
        with pytest.raises(ValueError, match="ttl_ms"):
            validate_ttl_ms(86_400_001)
