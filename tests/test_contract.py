"""OpenAPI contract tests: validate sample payloads against the Cycles Protocol v0 spec."""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

SPEC_PATH = pathlib.Path(__file__).parent / "fixtures" / "cycles-protocol-v0.yaml"

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_spec() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_refs(schema: dict | list, schemas: dict) -> dict | list:
    """Recursively resolve $ref pointers against the components/schemas map."""
    if isinstance(schema, list):
        return [_resolve_refs(item, schemas) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        ref_path = schema["$ref"]  # e.g. "#/components/schemas/Amount"
        ref_name = ref_path.rsplit("/", 1)[-1]
        resolved = copy.deepcopy(schemas[ref_name])
        return _resolve_refs(resolved, schemas)
    return {k: _resolve_refs(v, schemas) for k, v in schema.items()}


def _validate(instance: dict, schema_name: str, spec: dict) -> None:
    """Validate *instance* against *schema_name* from the spec with $ref resolution."""
    schemas = spec["components"]["schemas"]
    schema = copy.deepcopy(schemas[schema_name])
    resolved = _resolve_refs(schema, schemas)
    validator = Draft202012Validator(resolved)
    validator.validate(instance)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spec() -> dict:
    return _load_spec()


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

SAMPLE_SUBJECT = {"tenant": "acme"}
SAMPLE_ACTION = {"kind": "llm.completion", "name": "openai:gpt-4o"}
SAMPLE_AMOUNT = {"unit": "USD_MICROCENTS", "amount": 5000}
SAMPLE_AMOUNT_TOKENS = {"unit": "TOKENS", "amount": 1024}


# ---- Request bodies ----

DECISION_REQUEST = {
    "idempotency_key": "dec-001",
    "subject": SAMPLE_SUBJECT,
    "action": SAMPLE_ACTION,
    "estimate": SAMPLE_AMOUNT,
}

RESERVATION_REQUEST = {
    "idempotency_key": "res-001",
    "subject": SAMPLE_SUBJECT,
    "action": SAMPLE_ACTION,
    "estimate": SAMPLE_AMOUNT,
    "ttl_ms": 30000,
    "grace_period_ms": 5000,
    "overage_policy": "ALLOW_IF_AVAILABLE",
}

RESERVATION_REQUEST_MINIMAL = {
    "idempotency_key": "res-002",
    "subject": {"tenant": "acme"},
    "action": {"kind": "tool.search", "name": "web.search"},
    "estimate": {"unit": "TOKENS", "amount": 100},
}

COMMIT_REQUEST = {
    "idempotency_key": "cmt-001",
    "actual": SAMPLE_AMOUNT,
}

COMMIT_REQUEST_WITH_METRICS = {
    "idempotency_key": "cmt-002",
    "actual": SAMPLE_AMOUNT,
    "metrics": {
        "tokens_input": 512,
        "tokens_output": 128,
        "latency_ms": 340,
        "model_version": "gpt-4o-2024-05-13",
    },
}

RELEASE_REQUEST = {
    "idempotency_key": "rel-001",
    "reason": "user cancelled",
}

RELEASE_REQUEST_MINIMAL = {
    "idempotency_key": "rel-002",
}

EVENT_REQUEST = {
    "idempotency_key": "evt-001",
    "subject": SAMPLE_SUBJECT,
    "action": SAMPLE_ACTION,
    "actual": SAMPLE_AMOUNT,
}

EVENT_REQUEST_FULL = {
    "idempotency_key": "evt-002",
    "subject": {"tenant": "acme", "workspace": "ws1", "agent": "a1"},
    "action": {"kind": "llm.completion", "name": "openai:gpt-4o", "tags": ["prod"]},
    "actual": {"unit": "USD_MICROCENTS", "amount": 3200},
    "overage_policy": "ALLOW_WITH_OVERDRAFT",
    "metrics": {"tokens_input": 100, "tokens_output": 50, "latency_ms": 200},
    "client_time_ms": 1700000000000,
}

# ---- Response bodies ----

DECISION_RESPONSE_ALLOW = {"decision": "ALLOW"}

DECISION_RESPONSE_CAPS = {
    "decision": "ALLOW_WITH_CAPS",
    "caps": {"max_tokens": 1000, "cooldown_ms": 500},
    "affected_scopes": ["tenant:acme"],
}

DECISION_RESPONSE_DENY = {
    "decision": "DENY",
    "reason_code": "budget_exhausted",
    "retry_after_ms": 60000,
    "affected_scopes": ["tenant:acme"],
}

RESERVATION_RESPONSE = {
    "decision": "ALLOW",
    "reservation_id": "res_abc123",
    "reserved": SAMPLE_AMOUNT,
    "expires_at_ms": 1700000060000,
    "scope_path": "tenant:acme",
    "affected_scopes": ["tenant:acme"],
}

RESERVATION_RESPONSE_CAPS = {
    "decision": "ALLOW_WITH_CAPS",
    "reservation_id": "res_abc456",
    "reserved": SAMPLE_AMOUNT,
    "expires_at_ms": 1700000060000,
    "scope_path": "tenant:acme",
    "affected_scopes": ["tenant:acme"],
    "caps": {"max_tokens": 500},
}

COMMIT_RESPONSE = {
    "status": "COMMITTED",
    "charged": SAMPLE_AMOUNT,
}

COMMIT_RESPONSE_WITH_RELEASED = {
    "status": "COMMITTED",
    "charged": {"unit": "USD_MICROCENTS", "amount": 3000},
    "released": {"unit": "USD_MICROCENTS", "amount": 2000},
}

RELEASE_RESPONSE = {
    "status": "RELEASED",
    "released": SAMPLE_AMOUNT,
}

EVENT_RESPONSE = {
    "status": "APPLIED",
    "event_id": "evt_xyz789",
}

ERROR_RESPONSE = {
    "error": "BUDGET_EXCEEDED",
    "message": "Insufficient budget in scope tenant:acme",
    "request_id": "req-abc-123",
}

ERROR_RESPONSE_WITH_DETAILS = {
    "error": "OVERDRAFT_LIMIT_EXCEEDED",
    "message": "Debt exceeds overdraft limit",
    "request_id": "req-def-456",
    "details": {"current_debt": 50000, "overdraft_limit": 40000},
}


# ===========================================================================
# Request body contract tests
# ===========================================================================

class TestDecisionRequestContract:
    def test_valid_decision_request(self, spec: dict) -> None:
        _validate(DECISION_REQUEST, "DecisionRequest", spec)

    def test_decision_request_with_metadata(self, spec: dict) -> None:
        payload = {**DECISION_REQUEST, "metadata": {"trace_id": "t-123"}}
        _validate(payload, "DecisionRequest", spec)

    def test_decision_request_missing_required_field(self, spec: dict) -> None:
        incomplete = {k: v for k, v in DECISION_REQUEST.items() if k != "estimate"}
        with pytest.raises(ValidationError, match="estimate"):
            _validate(incomplete, "DecisionRequest", spec)

    def test_decision_request_extra_field_rejected(self, spec: dict) -> None:
        bad = {**DECISION_REQUEST, "bogus_field": True}
        with pytest.raises(ValidationError):
            _validate(bad, "DecisionRequest", spec)


class TestReservationRequestContract:
    def test_valid_full_reservation(self, spec: dict) -> None:
        _validate(RESERVATION_REQUEST, "ReservationCreateRequest", spec)

    def test_valid_minimal_reservation(self, spec: dict) -> None:
        _validate(RESERVATION_REQUEST_MINIMAL, "ReservationCreateRequest", spec)

    def test_reservation_with_dry_run(self, spec: dict) -> None:
        payload = {**RESERVATION_REQUEST_MINIMAL, "dry_run": True}
        _validate(payload, "ReservationCreateRequest", spec)

    def test_reservation_missing_subject(self, spec: dict) -> None:
        bad = {k: v for k, v in RESERVATION_REQUEST.items() if k != "subject"}
        with pytest.raises(ValidationError):
            _validate(bad, "ReservationCreateRequest", spec)


class TestCommitRequestContract:
    def test_valid_commit(self, spec: dict) -> None:
        _validate(COMMIT_REQUEST, "CommitRequest", spec)

    def test_commit_with_metrics(self, spec: dict) -> None:
        _validate(COMMIT_REQUEST_WITH_METRICS, "CommitRequest", spec)

    def test_commit_missing_actual(self, spec: dict) -> None:
        with pytest.raises(ValidationError, match="actual"):
            _validate({"idempotency_key": "x"}, "CommitRequest", spec)


class TestReleaseRequestContract:
    def test_valid_release(self, spec: dict) -> None:
        _validate(RELEASE_REQUEST, "ReleaseRequest", spec)

    def test_minimal_release(self, spec: dict) -> None:
        _validate(RELEASE_REQUEST_MINIMAL, "ReleaseRequest", spec)


class TestEventRequestContract:
    def test_valid_event(self, spec: dict) -> None:
        _validate(EVENT_REQUEST, "EventCreateRequest", spec)

    def test_event_full(self, spec: dict) -> None:
        _validate(EVENT_REQUEST_FULL, "EventCreateRequest", spec)

    def test_event_missing_action(self, spec: dict) -> None:
        bad = {k: v for k, v in EVENT_REQUEST.items() if k != "action"}
        with pytest.raises(ValidationError):
            _validate(bad, "EventCreateRequest", spec)


# ===========================================================================
# Response body contract tests
# ===========================================================================

class TestDecisionResponseContract:
    def test_allow(self, spec: dict) -> None:
        _validate(DECISION_RESPONSE_ALLOW, "DecisionResponse", spec)

    def test_allow_with_caps(self, spec: dict) -> None:
        _validate(DECISION_RESPONSE_CAPS, "DecisionResponse", spec)

    def test_deny(self, spec: dict) -> None:
        _validate(DECISION_RESPONSE_DENY, "DecisionResponse", spec)


class TestReservationResponseContract:
    def test_allow(self, spec: dict) -> None:
        _validate(RESERVATION_RESPONSE, "ReservationCreateResponse", spec)

    def test_allow_with_caps(self, spec: dict) -> None:
        _validate(RESERVATION_RESPONSE_CAPS, "ReservationCreateResponse", spec)


class TestCommitResponseContract:
    def test_committed(self, spec: dict) -> None:
        _validate(COMMIT_RESPONSE, "CommitResponse", spec)

    def test_committed_with_released(self, spec: dict) -> None:
        _validate(COMMIT_RESPONSE_WITH_RELEASED, "CommitResponse", spec)


class TestReleaseResponseContract:
    def test_released(self, spec: dict) -> None:
        _validate(RELEASE_RESPONSE, "ReleaseResponse", spec)


class TestEventResponseContract:
    def test_applied(self, spec: dict) -> None:
        _validate(EVENT_RESPONSE, "EventCreateResponse", spec)


class TestErrorResponseContract:
    def test_error_basic(self, spec: dict) -> None:
        _validate(ERROR_RESPONSE, "ErrorResponse", spec)

    def test_error_with_details(self, spec: dict) -> None:
        _validate(ERROR_RESPONSE_WITH_DETAILS, "ErrorResponse", spec)

    def test_error_missing_message(self, spec: dict) -> None:
        with pytest.raises(ValidationError, match="message"):
            _validate({"error": "NOT_FOUND", "request_id": "r1"}, "ErrorResponse", spec)

    def test_error_invalid_code(self, spec: dict) -> None:
        with pytest.raises(ValidationError):
            _validate(
                {"error": "MADE_UP_CODE", "message": "nope", "request_id": "r1"},
                "ErrorResponse",
                spec,
            )


# ===========================================================================
# Enum value tests
# ===========================================================================

class TestEnumValues:
    def test_unit_enum_values(self, spec: dict) -> None:
        expected = {"USD_MICROCENTS", "TOKENS", "CREDITS", "RISK_POINTS"}
        actual = set(spec["components"]["schemas"]["UnitEnum"]["enum"])
        assert actual == expected

    def test_error_code_values(self, spec: dict) -> None:
        expected = {
            "INVALID_REQUEST",
            "UNAUTHORIZED",
            "FORBIDDEN",
            "NOT_FOUND",
            "BUDGET_EXCEEDED",
            "BUDGET_FROZEN",
            "BUDGET_CLOSED",
            "RESERVATION_EXPIRED",
            "RESERVATION_FINALIZED",
            "IDEMPOTENCY_MISMATCH",
            "UNIT_MISMATCH",
            "OVERDRAFT_LIMIT_EXCEEDED",
            "DEBT_OUTSTANDING",
            "MAX_EXTENSIONS_EXCEEDED",
            "INTERNAL_ERROR",
        }
        actual = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
        assert actual == expected

    def test_decision_enum_values(self, spec: dict) -> None:
        expected = {"ALLOW", "ALLOW_WITH_CAPS", "DENY"}
        actual = set(spec["components"]["schemas"]["DecisionEnum"]["enum"])
        assert actual == expected

    def test_reservation_status_values(self, spec: dict) -> None:
        expected = {"ACTIVE", "COMMITTED", "RELEASED", "EXPIRED"}
        actual = set(spec["components"]["schemas"]["ReservationStatus"]["enum"])
        assert actual == expected

    def test_overage_policy_values(self, spec: dict) -> None:
        expected = {"REJECT", "ALLOW_IF_AVAILABLE", "ALLOW_WITH_OVERDRAFT"}
        actual = set(spec["components"]["schemas"]["CommitOveragePolicy"]["enum"])
        assert actual == expected
