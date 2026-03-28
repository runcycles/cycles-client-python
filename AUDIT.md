# Cycles Protocol v0.1.23 — Client (Python) Audit

**Date:** 2026-03-14
**Spec:** `cycles-protocol-v0.yaml` (OpenAPI 3.1.0, v0.1.23)
**Client:** `runcycles` (Python 3.10+ / httpx / Pydantic v2)
**Server audit:** See `cycles-server/AUDIT.md` (all passing)

---

## Summary

| Category | Pass | Issues |
|----------|------|--------|
| Endpoints & HTTP Methods | 9/9 | 0 |
| Request Schemas (field names & JSON keys) | 6/6 | 0 |
| Response Schemas (field names & JSON keys) | 10/10 | 0 |
| Enum Values | 5/5 | 0 |
| Nested Object Schemas | 8/8 | 0 |
| Auth Header (X-Cycles-API-Key) | — | 0 |
| Idempotency (header ↔ body sync) | — | 0 |
| Subject Validation | — | 0 |
| Response Header Capture | — | 0 |
| Client-Side Spec Constraint Validation | — | 0 |
| Lifecycle Orchestration | — | 0 |

**Overall: Client is protocol-conformant.** All endpoints, schemas, field names, JSON keys, and enum values match the OpenAPI spec. No open issues.

---

## Audit Scope

Compared the following across spec YAML and client Python source:
- All 9 endpoint paths, HTTP methods, and path/query parameters
- All 6 request body serializations vs spec schemas
- All 10 response model deserializations vs spec schemas
- All 5 enum types and their values
- Nested object schemas (Subject, Action, Amount, SignedAmount, Caps, CyclesMetrics, Balance, ErrorResponse)
- Auth and idempotency header handling
- Subject constraint validation (`anyOf` / at least one standard field)
- Pydantic Field constraints vs spec min/max bounds
- Lifecycle orchestration (reserve → execute → commit/release)

---

## PASS — Correctly Implemented

### Endpoints (all 9 match spec)

| Spec Endpoint | Client Method | HTTP Method | Match |
|---|---|---|---|
| `/v1/decide` | `client.decide()` | POST | PASS |
| `/v1/reservations` (create) | `client.create_reservation()` | POST | PASS |
| `/v1/reservations` (list) | `client.list_reservations()` | GET | PASS |
| `/v1/reservations/{reservation_id}` | `client.get_reservation()` | GET | PASS |
| `/v1/reservations/{reservation_id}/commit` | `client.commit_reservation()` | POST | PASS |
| `/v1/reservations/{reservation_id}/release` | `client.release_reservation()` | POST | PASS |
| `/v1/reservations/{reservation_id}/extend` | `client.extend_reservation()` | POST | PASS |
| `/v1/balances` | `client.get_balances()` | GET | PASS |
| `/v1/events` | `client.create_event()` | POST | PASS |

### Request Schemas (all match spec JSON keys)

**ReservationCreateRequest** — spec required: `[idempotency_key, subject, action, estimate]`
- Pydantic fields: `idempotency_key`, `subject`, `action`, `estimate`, `ttl_ms`, `grace_period_ms`, `overage_policy`, `dry_run`, `metadata` — all snake_case, all match spec

**CommitRequest** — spec required: `[idempotency_key, actual]`
- Pydantic fields: `idempotency_key`, `actual`, `metrics`, `metadata` — all match spec

**ReleaseRequest** — spec required: `[idempotency_key]`
- Pydantic fields: `idempotency_key`, `reason` — all match spec

**DecisionRequest** — spec required: `[idempotency_key, subject, action, estimate]`
- Pydantic fields: `idempotency_key`, `subject`, `action`, `estimate`, `metadata` — all match spec

**EventCreateRequest** — spec required: `[idempotency_key, subject, action, actual]`
- Pydantic fields: `idempotency_key`, `subject`, `action`, `actual`, `overage_policy`, `metrics`, `client_time_ms`, `metadata` — all match spec

**ReservationExtendRequest** — spec required: `[idempotency_key, extend_by_ms]`
- Pydantic fields: `idempotency_key`, `extend_by_ms`, `metadata` — all match spec

### Response Schemas (all match spec JSON keys)

| Spec Schema | Client Class | JSON Keys | Match |
|---|---|---|---|
| `ReservationCreateResponse` | `ReservationCreateResponse` | `decision`, `reservation_id`, `affected_scopes`, `expires_at_ms`, `scope_path`, `reserved`, `caps`, `reason_code`, `retry_after_ms`, `balances` | PASS |
| `CommitResponse` | `CommitResponse` | `status`, `charged`, `released`, `balances` | PASS |
| `ReleaseResponse` | `ReleaseResponse` | `status`, `released`, `balances` | PASS |
| `DecisionResponse` | `DecisionResponse` | `decision`, `caps`, `reason_code`, `retry_after_ms`, `affected_scopes` | PASS |
| `EventCreateResponse` | `EventCreateResponse` | `status`, `event_id`, `balances` | PASS |
| `ReservationExtendResponse` | `ReservationExtendResponse` | `status`, `expires_at_ms`, `balances` | PASS |
| `BalanceResponse` | `BalanceResponse` | `balances`, `has_more`, `next_cursor` | PASS |
| `ReservationDetail` | `ReservationDetail` | `reservation_id`, `status`, `idempotency_key`, `subject`, `action`, `reserved`, `committed`, `created_at_ms`, `expires_at_ms`, `finalized_at_ms`, `scope_path`, `affected_scopes`, `metadata` | PASS |
| `ReservationSummary` | `ReservationSummary` | `reservation_id`, `status`, `idempotency_key`, `subject`, `action`, `reserved`, `created_at_ms`, `expires_at_ms`, `scope_path`, `affected_scopes` | PASS |
| `ReservationListResponse` | `ReservationListResponse` | `reservations`, `has_more`, `next_cursor` | PASS |

### Nested Object Schemas (all match)

| Spec Schema | Client Class | JSON Keys | Match |
|---|---|---|---|
| `Subject` | `Subject` | `tenant`, `workspace`, `app`, `workflow`, `agent`, `toolset`, `dimensions` | PASS |
| `Action` | `Action` | `kind`, `name`, `tags` | PASS |
| `Amount` | `Amount` | `unit`, `amount` | PASS |
| `SignedAmount` | `SignedAmount` | `unit`, `amount` | PASS |
| `Caps` | `Caps` | `max_tokens`, `max_steps_remaining`, `tool_allowlist`, `tool_denylist`, `cooldown_ms` | PASS |
| `StandardMetrics` | `CyclesMetrics` | `tokens_input`, `tokens_output`, `latency_ms`, `model_version`, `custom` | PASS |
| `Balance` | `Balance` | `scope`, `scope_path`, `remaining`, `reserved`, `spent`, `allocated`, `debt`, `overdraft_limit`, `is_over_limit` | PASS |
| `ErrorResponse` | `ErrorResponse` | `error`, `message`, `request_id`, `details` | PASS |

### Enum Values (all match spec)

| Spec Enum | Client Enum | Values | Match |
|---|---|---|---|
| `DecisionEnum` | `Decision` | `ALLOW`, `ALLOW_WITH_CAPS`, `DENY` | PASS |
| `UnitEnum` | `Unit` | `USD_MICROCENTS`, `TOKENS`, `CREDITS`, `RISK_POINTS` | PASS |
| `CommitOveragePolicy` | `CommitOveragePolicy` | `REJECT`, `ALLOW_IF_AVAILABLE`, `ALLOW_WITH_OVERDRAFT` | PASS |
| `ReservationStatus` | `ReservationStatus` | `ACTIVE`, `COMMITTED`, `RELEASED`, `EXPIRED` | PASS |
| `ErrorCode` | `ErrorCode` | All 12 spec values + `UNKNOWN` (client fallback) | PASS |

Note: Client `ErrorCode` adds `UNKNOWN` as a fallback for unrecognized server error codes. This is a client-side convenience and does not violate the spec.

### Auth & Idempotency (correct)

- **X-Cycles-API-Key**: Set on all requests via `httpx.Client` base headers in `CyclesClient.__init__()` (`client.py`)
- **X-Idempotency-Key**: Extracted from request body `idempotency_key` field via `_extract_idempotency_key()` and set as header in `_post()`. Header and body values always match (copied from body to header), satisfying the spec rule: "If X-Idempotency-Key header is present and body.idempotency_key is present, they MUST match."

### Subject Validation (correct)

- `validate_subject()` in `_validation.py` calls `Subject.has_at_least_one_standard_field()` which checks all 6 standard fields — matches spec `anyOf` constraint
- Pydantic Field constraints enforce `maxLength: 128` on all Subject fields and `maxLength: 256` on dimension values

### Response Header Capture (correct)

- `_extract_response_headers()` in `client.py` captures `x-request-id`, `x-ratelimit-remaining`, `x-ratelimit-reset`, `x-cycles-tenant`
- Exposed via `CyclesResponse` properties: `request_id`, `rate_limit_remaining`, `rate_limit_reset`, `cycles_tenant`

### Client-Side Spec Constraint Validation (correct)

All spec constraints are validated both via Pydantic Field validators (on typed request models) and via explicit validation functions (on dict-based lifecycle path):

- `validate_non_negative()`: `Amount.amount >= 0` (spec `minimum: 0`)
- `validate_ttl_ms()`: 1000–86400000 (spec `minimum: 1000, maximum: 86400000`)
- `validate_grace_period_ms()`: 0–60000 (spec `minimum: 0, maximum: 60000`)
- `validate_extend_by_ms()`: 1–86400000 (spec `minimum: 1, maximum: 86400000`)
- Pydantic `Field(ge=1, le=86_400_000)` on `ReservationExtendRequest.extend_by_ms`
- Pydantic `Field(max_length=64)` on `Action.kind`, `Field(max_length=256)` on `Action.name`
- Pydantic `Field(min_length=1, max_length=256)` on all `idempotency_key` fields

### Lifecycle Orchestration (correct)

- Reserve → Execute → Commit flow with proper cleanup (release on failure)
- Heartbeat-based TTL extension at `max(ttl_ms / 2, 1000)` ms interval using `extend` endpoint
- Commit retry engine for transient failures (transport errors, 5xx) with exponential backoff
- Dry-run handling returns `DryRunResult` without executing guarded function
- `DENY` decision correctly raises typed `CyclesProtocolError`
- `ALLOW_WITH_CAPS` correctly propagates `Caps` via `CyclesContext`
- Lifecycle instance cached at decoration time (deferred client resolution on first call)
- `ContextVar`-based context propagation (safe for both sync threads and async tasks)

### HTTP Status Code Handling (correct)

- `is_success` correctly handles 2xx range (200 for most endpoints, 201 for events)
- Error responses parsed via `ErrorResponse.model_validate()` with `ErrorCode` mapping
- Typed exceptions: `BudgetExceededError`, `OverdraftLimitExceededError`, `DebtOutstandingError`, `ReservationExpiredError`, `ReservationFinalizedError`

---

## Verdict

The client is **fully protocol-conformant** with the Cycles Protocol v0.1.23 OpenAPI spec. All 9 endpoints, 6 request schemas, 10 response schemas, 5 enum types, and all nested object serializations match the spec exactly. JSON field names use correct snake_case throughout. Auth headers, idempotency handling, subject validation, response header capture, and spec constraint validation all follow spec normative rules. No open issues.

---

## OpenAPI Contract Tests (added 2026-03-28)

**Spec version:** v0.1.24
**Test file:** `tests/test_contract.py` (34 tests, all passing)

Automated contract tests validate sample request/response payloads against the OpenAPI spec schemas using `jsonschema.Draft202012Validator` with recursive `$ref` resolution:

- **Request schemas validated:** DecisionRequest, ReservationCreateRequest, CommitRequest, ReleaseRequest, EventCreateRequest
- **Response schemas validated:** DecisionResponse, ReservationCreateResponse, CommitResponse, ReleaseResponse, EventCreateResponse, ErrorResponse
- **Negative tests:** missing required fields, extra fields (additionalProperties), invalid enum values
- **Enum value tests:** UnitEnum, ErrorCode, DecisionEnum, ReservationStatus, CommitOveragePolicy
- **Spec fixture:** `tests/fixtures/cycles-protocol-v0.yaml` (copy of canonical spec)
