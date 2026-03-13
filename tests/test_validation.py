"""Tests for input validation utilities."""

import pytest

from runcycles._validation import (
    validate_grace_period_ms,
    validate_non_negative,
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


class TestValidateNonNegative:
    def test_positive_valid(self) -> None:
        validate_non_negative(1, "amount")

    def test_zero_valid(self) -> None:
        # Spec Amount.amount has minimum: 0, so 0 is valid
        validate_non_negative(0, "amount")

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="estimate"):
            validate_non_negative(-1, "estimate")


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


class TestValidateGracePeriodMs:
    def test_none_is_valid(self) -> None:
        validate_grace_period_ms(None)

    def test_zero_is_valid(self) -> None:
        validate_grace_period_ms(0)

    def test_max_is_valid(self) -> None:
        validate_grace_period_ms(60_000)

    def test_mid_range(self) -> None:
        validate_grace_period_ms(5000)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="grace_period_ms"):
            validate_grace_period_ms(-1)

    def test_above_maximum(self) -> None:
        with pytest.raises(ValueError, match="grace_period_ms"):
            validate_grace_period_ms(60_001)
