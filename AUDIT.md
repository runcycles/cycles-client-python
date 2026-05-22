# Cycles Protocol v0.1.25 — Client (Python) Audit

**Date:** 2026-05-22 (v0.4.3 — `expires_from`/`expires_to` and `finalized_from`/`finalized_to` ISO-8601 window-filter passthrough on `list_reservations` per `cycles-protocol-v0.yaml` revision 2026-05-22; closes the Python-client side of runcycles/cycles-server#162. No code change — `**query_params` already forwards arbitrary kwargs. Added sync + async regression tests; unlike `from`/`to` the new param names are plain kwargs (no Python-reserved-word workaround needed). 393 tests pass at 100% coverage.),
2026-05-21 (v0.4.2 — `from` / `to` ISO-8601 window-filter passthrough on `list_reservations` per `cycles-protocol-v0.yaml` revision 2026-05-21; closes the client side of runcycles/cycles-server#159. No code change — the existing `**query_params` signature already forwards arbitrary kwargs to the URL query string. Added sync + async regression tests that lock the passthrough in (using the `**{"from": ..., "to": ...}` dict-unpack form because `from` is a Python reserved keyword). 391 tests pass at 100% coverage.),
2026-03-14
**Spec:** `cycles-protocol-v0.yaml` (OpenAPI 3.1.0, v0.1.25)
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

---

## Streaming Convenience Module (added 2026-04-08)

**Module:** `runcycles/streaming.py`
**Test file:** `tests/test_streaming.py` (64 tests, all passing)
**Version:** 0.3.0

Added `StreamReservation` and `AsyncStreamReservation` context managers that automate the reserve → commit/release lifecycle for streaming use cases. This is a DX convenience layer — no protocol changes.

- **`StreamReservation`** — sync context manager: reserves on `__enter__`, auto-commits on successful `__exit__`, auto-releases on exception
- **`AsyncStreamReservation`** — async equivalent using `__aenter__`/`__aexit__`
- **`StreamUsage`** — mutable accumulator for token counts and cost during streaming
- **Client convenience methods:** `CyclesClient.stream_reservation()` and `AsyncCyclesClient.stream_reservation()` — thin factories that build Subject from config defaults
- **Cost resolution:** explicit `usage.actual_cost` > `cost_fn(usage)` > estimate fallback
- **Heartbeat:** automatic TTL extension, same interval formula as decorator lifecycle (`max(ttl_ms / 2, 1000)` ms)
- **Commit retry:** uses existing `CommitRetryEngine`/`AsyncCommitRetryEngine`
- **Context propagation:** sets/clears `CyclesContext` via `ContextVar`, accessible via `get_cycles_context()`; respects user-set `ctx.metrics` during streaming
- **Spec validation:** `validate_ttl_ms()` (1000–86400000), `validate_grace_period_ms()` (0–60000), `validate_subject()` (at least one standard field) — matches lifecycle.py
- **Error handling:** `RESERVATION_FINALIZED`, `RESERVATION_EXPIRED`, and `IDEMPOTENCY_MISMATCH` do not trigger release; other 4xx client errors do trigger release — matches lifecycle.py behavior exactly

Protocol conformance: No new endpoints or protocol changes. All reservation, commit, release, and extend calls use the same client methods and body formats as the decorator path. Verified by 64 unit tests covering success, deny, error, retry, heartbeat, cost resolution, context propagation, spec validation, and all commit error-code branches.

---

## Dynamic Subject & Action Fields on `@cycles` (added 2026-04-27)

**Issue:** [#45](https://github.com/runcycles/cycles-client-python/issues/45)
**Files:** `runcycles/lifecycle.py`, `runcycles/decorator.py`
**Test files:** `tests/test_lifecycle.py`, `tests/test_decorator.py`
**Version:** 0.4.0

Widened the `@cycles` decorator to accept callables — in addition to constants — for every field that previously had to be static at decoration time. Mirrors the existing `estimate` / `actual` callable contract and re-aligns the Python client with the Java client's `@Cycles(workspace = "#workspaceId")` SpEL behavior shipped in `cycles-spring-boot-starter` 0.2.1 ([java#50](https://github.com/runcycles/cycles-spring-boot-starter/pull/50)).

- **Newly callable fields:** `tenant`, `workspace`, `app`, `workflow`, `agent`, `toolset`, `action_kind`, `action_name`, `action_tags`, `dimensions`. Each accepts `T | Callable[..., T | None] | None`.
- **Resolution:** new `_resolve_value(val, args, kwargs)` helper in `lifecycle.py` invokes the callable with the decorated function's `*args, **kwargs` at reservation time; constants pass through untouched.
- **Fallback semantics preserved:** subject callables returning `None` fall through to `default_subject_fields` (client config); `action_kind` / `action_name` returning `None` fall through to `"unknown"`; `action_tags` / `dimensions` returning `None` are omitted. Constants behave identically to today (regression-tested).
- **Fail-fast:** exceptions raised inside a user callable propagate to the decorator caller without creating a reservation.
- **Signature change:** `_build_reservation_body` now takes `args` and `kwargs` parameters; both `CyclesLifecycle.execute` and `AsyncCyclesLifecycle.execute` thread them through.

Protocol conformance: No protocol or wire-format changes. The reservation request body shape is unchanged — only the source of each field's value is widened. Verified by new unit tests in `TestCallableSubjectFields`, `TestCallableActionFields`, `TestCallableDimensions` plus an end-to-end decorator test asserting the captured request body.

## PyPI Metadata Refresh (added 2026-05-08)

**Files:** `pyproject.toml`
**Version:** 0.4.1

Metadata-only release retargeting the package for category-search discovery on PyPI. No code, no test, no protocol changes — wire format and API are identical to 0.4.0.

- **Description rewritten** to lead with the literal category-search phrase: `"Python AI agent budget control — enforce LLM cost limits, tool permissions, and multi-tenant policies before agent actions execute."`
- **Keywords expanded 12 → 21**, organized into category-search terms (`ai-agent`, `agent-budget`, `budget-control`, `cost-control`, `cost-enforcement`, `spending-limit`, `llm-cost`, `runtime-authority`, `action-control`, `multi-tenant`), framework targeting (`langchain`, `langgraph`, `crewai`, `autogen`, `openai-agents`, `mcp`, `openai`, `anthropic`), and brand (`cycles`, `runcycles`).
- **Classifier added:** `Topic :: Scientific/Engineering :: Artificial Intelligence`.

Driven by Python-side adoption diagnostic finding the biggest sub-gap was discovery, not SDK feature parity. Companion changes: GitHub topics on this repo (`governance` dropped, `mcp` added) and Python framework integration guide retitling on `runcycles/cycles-docs` (PR #568).

Protocol conformance: No protocol or wire-format changes. Existing test suite at 100% coverage; no test additions.

## LangChain Agent Middleware Integration Pointer (added 2026-05-10)

**Files:** `README.md`, `examples/langchain_integration.py`
**Version:** unreleased (next 0.4.x — docs/examples only)

Documentation-only update pointing users at the new sibling package [`langchain-runcycles`](https://github.com/runcycles/langchain-runcycles) for LangChain **agent middleware** integration. No SDK code changes; no protocol changes.

- **README.md**: Added a new `## Integrations` section listing `langchain-runcycles` (PyPI: `langchain-runcycles`) as the canonical path for `langchain.agents.create_agent` workflows. The existing `examples/langchain_integration.py` row is reframed as the right fit for **non-agent** LangChain runnables (bare `ChatOpenAI`, chains, RAG); middleware requires `create_agent` so the two patterns serve different surfaces and both remain supported.
- **examples/langchain_integration.py**: Updated the file-level docstring to point at `langchain-runcycles` for agent workflows while preserving the callback-handler example as-is. No code changes.

Background: LangChain 1.x introduced an `AgentMiddleware` API with `wrap_tool_call`, `before_model`, and `wrap_model_call` hooks. The new package wraps that API on top of this SDK's existing `decide` / `create_reservation` / `commit_reservation` / `release_reservation` surface — no new SDK methods needed. Splitting into a sibling repo follows LangChain's [publishing guidance](https://docs.langchain.com/oss/python/contributing/publish-langchain) ("New integrations should be published as standalone PyPI packages") and the `langchain-<service>` naming convention used by `langchain-anthropic`, `langchain-openai`, etc.

Protocol conformance: No protocol or wire-format changes. The new sibling package consumes this SDK as a normal dependency.

## Infrastructure Hardening (added 2026-05-12)

**Files:** `.claude/session-start-global-deny.sh`, `.github/workflows/python-publish.yml`
**Version:** unreleased (CI/Claude-config only — no package version change)

Cross-cutting hardening landed in response to org-wide tracking issues filed in `runcycles/.github`. Two distinct changes; both are infra-only.

- **`.claude/session-start-global-deny.sh`** synced from the new canonical at `runcycles/.github/shared-config/`. The script now (a) carries a top-of-file callout explaining that Part 2 mutates the `origin` remote of every sibling repo under `/home/user/*`, not just the current checkout, and (b) honors a `CYCLES_CLAUDE_SKIP_REMOTE_REWRITE=1` opt-out env var. Part 1 (MCP deny rules) is unchanged. Tracks `runcycles/.github#63`.

- **`.github/workflows/python-publish.yml`** gained a `Verify pyproject version matches tag` step that runs on tag-triggered builds (`refs/tags/v*`). The step parses `pyproject.toml` via `tomllib` and fails the workflow before the build phase if the declared version doesn't match the tag (e.g., tag `v0.5.0` against `pyproject.toml` still on `0.4.1` or a `dev0` pre-release). PyPI already rejects duplicate versions server-side, but this surfaces operator error earlier in the pipeline. Python analog of the Java SNAPSHOT-guard tracked in `runcycles/.github#61`.

Not included in this change: bumping the reusable-workflow ref `runcycles/.github/.github/workflows/ci-python.yml@main` to `@v1` (`runcycles/.github#60`). That bump is intentionally split into a separate follow-up PR — it depends on the `v1` tag existing in `runcycles/.github`, which is being cut after the canonical-script PR (`runcycles/.github#64`) merges.

Protocol conformance: No protocol or wire-format changes. No SDK source touched. Test suite unaffected.
