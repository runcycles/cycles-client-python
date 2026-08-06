# Cycles Protocol v0.1.25 — Client (Python) Audit

**Date:** 2026-08-06 (v0.5.3 — managed streams accept caller-scoped
idempotency keys and an opt-in post-journal settlement error surface;
recognized commit rejections never release known spend in sync/async lifecycle
or streaming paths; decorator post-action failures are separated from guarded
function failures; and actual-evaluation failure safely commits an
estimate-marked fallback. The LangChain example now uses the managed heartbeat
and durable journal with normalized cached-token usage. Final verification:
630 tests pass, 5 live-server tests skip, coverage is 98.47%, and the shared
recovery profile passes 12/12 scenarios; Ruff, mypy, and the sdist/wheel build
are clean. Full evidence is recorded in the
2026-08-06 section below.),
**Date:** 2026-07-30 (no runtime change — durable recovery conformance now
emits the profile 0.3 machine-readable evidence report, uploads it from both
CI and release workflows even on failure, and links the public SDK matrix from
the README. The report binds all 12 shared scenarios and their exact native
tests to the SDK commit, protocol commit, catalog digest, and Actions run.),
**Date:** 2026-07-27 (v0.5.0 — durable commit retries: on-disk pending-commit journal with next-run replay, bounded atexit flush, and `POST /v1/events` recovery for commits that land after reservation expiry; async retry-task GC fix; `retry_enabled=False` now journals instead of silently dropping. Review hardening: per-identity journal partitioning (tenant-keyed when configured — rotation-safe), 429 transient with `Retry-After` incl. first-attempt commits (no more release-on-429), 401/403 retained, `0700`/`0600` journal permissions, process-wide flush deadline; round 3: first-attempt 401/403 journaled (not released), Retry-After persisted across restarts, PBKDF2 30k rounds, unique journal temp files. See the dated entry below. 491 tests pass at 100% coverage.),
2026-07-10 (v0.5.0 — `TENANT_CLOSED` + `LIMIT_EXCEEDED` error-code support. `TENANT_CLOSED` per runtime spec v0.1.25.13 (`cycles-protocol-v0.yaml`, runcycles/cycles-protocol#125): `ErrorCode.TENANT_CLOSED` enum member, `TenantClosedError` subclass wired into the lifecycle error-code→exception mapping (reservation-creation surfaces), `CyclesProtocolError.is_tenant_closed()` helper. `LIMIT_EXCEEDED` per runtime spec v0.1.25.12 (revision 2026-07-04, HTTP 429 rate limiting): enum-only member matching the `BUDGET_FROZEN`/`BUDGET_CLOSED` pattern, classified retryable at both the enum and exception layers (429 is transient; previously it fell through to `UNKNOWN`, which happened to be retryable, so semantics are unchanged — now typed). Enum reordered to mirror spec declaration order. Both purely additive; previously both codes fell through the `ErrorCode.from_string` forward-compat path to `UNKNOWN`. See the dated entries at the end of this file. 398 tests pass at 100% coverage.),
2026-07-09 (README + docstring transport-error documentation fix, no version bump — see the dated entry at the end of this file. `CyclesTransportError` is exported but never raised by the SDK; README and its docstring now describe the actual `status == -1` surfacing.),
2026-07-03 (integration-test-only, no version bump — `test_health_check` now probes the public `/actuator/health/readiness` endpoint instead of aggregate `/actuator/health`, which requires `X-Admin-API-Key` since cycles-server v0.1.25.45 and fails closed with 500 when the server has no admin key configured. The old assertion had failed the org nightly Full-Stack Integration every night since 2026-06-28. No library code change.),
2026-05-22 (v0.4.3 — `expires_from`/`expires_to` and `finalized_from`/`finalized_to` ISO-8601 window-filter passthrough on `list_reservations` per `cycles-protocol-v0.yaml` revision 2026-05-22; closes the Python-client side of runcycles/cycles-server#162. No code change — `**query_params` already forwards arbitrary kwargs. Added sync + async regression tests; unlike `from`/`to` the new param names are plain kwargs (no Python-reserved-word workaround needed). 393 tests pass at 100% coverage.),
2026-05-21 (v0.4.2 — `from` / `to` ISO-8601 window-filter passthrough on `list_reservations` per `cycles-protocol-v0.yaml` revision 2026-05-21; closes the client side of runcycles/cycles-server#159. No code change — the existing `**query_params` signature already forwards arbitrary kwargs to the URL query string. Added sync + async regression tests that lock the passthrough in (using the `**{"from": ..., "to": ...}` dict-unpack form because `from` is a Python reserved keyword). 391 tests pass at 100% coverage.),
2026-03-14
**Spec:** `cycles-protocol-v0.yaml` (OpenAPI 3.1.0, v0.1.25)
**Client:** `runcycles` (Python 3.10+ / httpx / Pydantic v2)
**Server audit:** See `cycles-server/AUDIT.md` (all passing)

---

## 2026-08-06 — Framework-safe managed settlement (v0.5.3)

Framework integrations can pass a stable `idempotency_key` into sync or async
`stream_reservation()`. The create request keeps that identity and commit /
release keys derive from it, while the journal keeps the exact commit body for
same-key replay and `/v1/events` fallback. `raise_on_commit_failure=True`
raises only after `persist_pending` and the appropriate recovery scheduling;
the default remains non-raising and exposes `settlement_error` for adapters
that want their own policy/logging surface.

Self-review found a separate known-spend violation in both lifecycle classes
and both stream managers: a recognized non-retryable commit 4xx discarded the
journal and then released the reservation even though the guarded action had
already completed. All four paths now discard only the unrecoverable journal
record, log the rejection, and never return the reservation's budget. Handler
exceptions before an actual is recorded still take the normal release path.

The LangChain callback example now holds managed reservation objects per
LangChain `run_id`, uses a lock for concurrent callbacks, reads provider-neutral
`AIMessage.usage_metadata`, prices cached GPT-4o input separately, and settles
through the heartbeat/journal/event-recovery path. It also sets LangChain's
`raise_error` handler flag so a reserve denial cannot be logged and ignored
while the model call proceeds.

## 2026-07-28 — Server-authoritative heartbeat via remaining_ttl_ms (v0.5.1, same PR)

Spec review round 5 proved the fallback heuristic's regime detection
undecidable (sticky band window [0.75×min(ttl/2,30s), 0.9×ttl)) and
immediate priming schedule-dependent under maximum-lead clamping, so the
protocol adopted remaining_ttl_ms on create+extend responses (spec
v0.1.25.16; cycles-server v0.1.25.59 emits it, recomputed fresh on
idempotent replays and excluded from evidence). The SDK implements the
spec's hardened NORMATIVE algorithm when the field is present: per-attempt
rtt, lead_floor = max(0, remaining − rtt), retry_reserve =
2×max(request_timeout_budget, 1s, 2×rtt_max) + max(1s, 2×rtt_max) (the
enforced httpx timeouts define the budget), next beat at lead_floor −
retry_reserve from response receipt, recomputed per schema-valid 200
(non-200 2xx is ambiguous → same-key recovery); recovery repeats while
retry_window = lead_estimate − attempt_budget − safety_margin stays
positive with a no-progress guard, 429 Retry-After honored only inside the
window, other 4xx stop without key rotation; a lease that cannot hold the
reserve gets one immediate fresh attempt then stop-and-surface; lead_min
skip bypassed; no primed extension when the create carries the field.
Bookkeeping keeps running so the v2.3+band heuristic (now explicitly
best-effort fallback) resumes seamlessly if the field disappears. Final
self-review also made the response contract uniform across both scheduling
modes: only a complete schema-valid HTTP 200 create/extend response succeeds;
ambiguous 2xx keeps the same key. The enforced timeout covers the whole
attempt, first-delay setup time is deducted, and reliable pre-field RTT
samples remain in the safety budget. 597 tests pass at 99.07% coverage.

## 2026-07-27 — Heartbeat conservative-lead redesign + actual_source marker (v0.5.1)

The heartbeat extended by full ttl_ms every ttl/2 beat while extend_by_ms
is relative to current expiry — drifting expiry outward +ttl/2 per beat
(zombie budget lockup on kill; max_extensions burned 2× too fast). Four
adversarial review rounds refined the replacement; final (v2.3) design:
conservative lead LOWER BOUND lead_min = Σ measured grants − monotonic
elapsed (grants from successive returned expires_at_ms — same server
frame only, no cross-clock arithmetic); FIRST extension fires immediately
(any bounded first delay can outlive a tenant-policy-capped lease);
cadence splits by regime — a grant tracking the lease drives
clamp(grant/2, 500ms, ttl/2), while a grant merely mirroring elapsed time
(maximum-lead clamping: grant ≤ 0, or grant < 0.9×ttl inside a
[0.75, 1.25]×elapsed band — the lower edge keeps a post-skip small grant
from sticking in the hold) carries no wire cadence signal, so the loop
holds min(ttl/2, 30s) and warns once instead of burning max_extensions at
the floor; a transient failure on the primed beat backs off to the held
cadence (no hot loop); skip at lead_min ≥ 1.5×last_grant; extend
idempotency key reused on retries; permanent stop on expired/finalized/
max-extensions/tenant-closed/not-found (and raw 404/410). The HTTP Date
header plays no heartbeat role (RFC 9110 §6.6.1; Redis TIME vs container
clock). Commits whose actual was defaulted from the estimate now carry
metadata.actual_source="estimate" for audit honesty. Spec guidance:
cycles-protocol#148. 533 tests pass at 100% coverage.

## 2026-07-27 — Durable commit retries (journal + /v1/events fallback)

Pending commits no longer exist only in memory: the retry engines journal
each one to disk (`~/.runcycles/commit-journal`, config/env overridable)
before retrying, replay survivors on the next run, and flush bounded at
interpreter exit. A commit answered `RESERVATION_EXPIRED` — where the server
has already returned the reserved budget to the pool — is recovered via
`POST /v1/events` (spec-conformant `EventCreateRequest`, commit idempotency
key reused, recovery markers in `metadata`). Also fixes the async engine's
unreferenced-task GC hazard and the silent drop under `retry_enabled=False`.
Post-review (PR #89) hardening, round 1: journal records are partitioned
into per-identity subdirectories so co-located clients with different
credentials never replay or 401-discard each other's records and replay
claims cannot cross identities; HTTP 429 / `LIMIT_EXCEEDED` on retried
attempts is transient (record retained, `Retry-After` honored) instead of
a terminal discard; the atexit flush enforces one process-wide
`retry_flush_timeout` deadline instead of per-engine. Round 2: a
rate-limited *first* commit attempt now schedules a retry in all four
lifecycle variants instead of releasing the reservation (a release
returned budget for spend that already happened); the identity fingerprint
uses the configured tenant when set, so API-key rotation no longer orphans
pending records, and 401/403 retains the journal entry instead of
discarding it; journal directories/files are created `0700`/`0600` where
supported. Round 3: first-attempt 401/403 also journals instead of
releasing (same class as the 429 gap, all four variants); the `Retry-After`
floor is persisted as an absolute `not_before_ms` and restored on replay;
PBKDF2 rounds reduced 600k → 30k (~20 ms cold, cache 256 — input is a
high-entropy machine credential, rounds only defend the weak-key
fallback); journal temp files get unique per-writer names so concurrent
processes cannot publish each other's partial writes. Fleet self-review
round: ASCII-explicit sanitizer + pinned cross-SDK fingerprint vectors
(interop with TS/Java identity dirs), blank-tenant normalization, 1-hour
clamp on honored Retry-After and restored floors, status-410 expired
trigger, unclassifiable-4xx retention (never release/discard on codeless
or unknown-code responses), base-dir permissions, stale-temp reaping.
506 tests pass at 100% coverage.

## 2026-07-26 — Python publishing workflow maintenance

Dependabot PRs #82–#86 update the SHA-pinned PyPI trusted-publishing action to
1.14.1, `actions/setup-python` to 7.0.0, the CodeQL SARIF uploader to 4.37.3,
checkout to 7.0.1, and OSSF Scorecard to 2.4.4. Setup Python 7 moves the action
runtime to ESM and removes the optional `pip-install` input; this workflow does
not use that input. All changes are confined to publishing and security
workflows, and the client API, package dependencies, generated artifacts, and
protocol behavior are unchanged. The full PR check set passed on Python 3.10
and 3.12 for all five heads.

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
- Typed exceptions: `BudgetExceededError`, `OverdraftLimitExceededError`, `DebtOutstandingError`, `ReservationExpiredError`, `ReservationFinalizedError`, `TenantClosedError` (added 2026-07-10, raised at reservation-creation time)

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

## README Transport-Error Documentation Fix (added 2026-07-09)

**Files:** `README.md`
**Version:** unreleased (docs-only, no version bump, no CHANGELOG entry per repo convention)

Documentation-only correction. The README's exception-hierarchy table described `CyclesTransportError` as "Network-level failure (connection, DNS, timeout)", implying the SDK raises it — but nothing in the package ever raises it. Actual behavior:

- **`@cycles` decorator / HOF paths:** a transport failure at reserve time raises `CyclesProtocolError` with `status == -1` and `error_code=None` (via `_build_protocol_exception` in `lifecycle.py`); commit-time transport failures are retried in the background by the commit retry engine, not raised.
- **Programmatic client:** never raises for transport failures — returns `CyclesResponse` with `is_transport_error == True` and `status == -1` (`CyclesResponse.transport_error` constructor in `response.py`).

The table row now states the class is exported for user code but never raised by the SDK, and a new "Transport errors" subsection documents the `status == -1` behavior for both API surfaces with a detection example. Wording matches the docs site (`cycles-docs/how-to/error-handling-patterns-in-python.md`). `CyclesTransportError` remains exported from `runcycles/__init__.py` for use in user code — no API change.

Protocol conformance: No protocol or wire-format changes. No SDK source touched. Test suite unaffected.

## TENANT_CLOSED Error-Code Support (added 2026-07-10)

**Files:** `runcycles/models.py`, `runcycles/exceptions.py`, `runcycles/lifecycle.py`, `runcycles/__init__.py`, `README.md`, `CHANGELOG.md`, tests
**Version:** unreleased

Additive support for the `TENANT_CLOSED` protocol error code, per runtime spec v0.1.25.13 of `cycles-protocol-v0.yaml` (runcycles/cycles-protocol#125). Servers return HTTP 409 `error=TENANT_CLOSED` on reservation create/commit/release/extend when the owning tenant's status is CLOSED — the runtime-surface mirror of governance spec Rule 2 (mutating operations on objects owned by a CLOSED tenant are rejected with 409 TENANT_CLOSED).

- `ErrorCode.TENANT_CLOSED` added to the enum in `models.py`, in spec declaration order: `… MAX_EXTENSIONS_EXCEEDED, LIMIT_EXCEEDED, TENANT_CLOSED, INTERNAL_ERROR` (initially placed after `BUDGET_CLOSED`; relocated when `LIMIT_EXCEEDED` landed so the enum mirrors the spec exactly).
- `TenantClosedError(CyclesProtocolError)` added to `exceptions.py` following the existing per-code subclass pattern; exported from `runcycles/__init__.py`.
- `_build_protocol_exception` in `lifecycle.py` maps `error == "TENANT_CLOSED"` to `TenantClosedError`. This mapping is invoked on the reservation-creation paths of the `@cycles` decorator, `CyclesLifecycle`, and streaming surfaces; commit/release-time error codes are handled by the existing commit-failure policy (logged + released) and are not raised as typed exceptions.
- `CyclesProtocolError.is_tenant_closed()` helper added alongside the existing `is_*` per-code helpers.
- README exception table gained a `TenantClosedError` row.

Forward-compat behavior before this change (verified): an unrecognized `TENANT_CLOSED` string was mapped to `ErrorCode.UNKNOWN` by `ErrorCode.from_string`, so lifecycle surfaces raised plain `CyclesProtocolError` with `error_code == "UNKNOWN"` — which `is_retryable()` reports as retryable. Deserialization never failed. With this change the code is recognized, typed, and correctly non-retryable.

The vendored spec fixture (`tests/fixtures/cycles-protocol-v0.yaml`, pinned at v0.1.24) is intentionally untouched; it will be refreshed when the v0.1.25.13 spec PR merges. The contract suite validates the fixture, not the client enum, so the client enum being a superset is by design.

Tests: new coverage for the enum member (`test_models.py`), the exception subclass + helper (`test_exceptions.py`), and the lifecycle mapping (`test_lifecycle.py`). 396 tests pass at 100% coverage (gate ≥95%); ruff + mypy --strict clean.

## LIMIT_EXCEEDED Error-Code Support (added 2026-07-10)

**Files:** `runcycles/models.py`, `runcycles/exceptions.py`, `CHANGELOG.md`, tests
**Version:** unreleased (same PR as the TENANT_CLOSED entry above)

Additive support for the `LIMIT_EXCEEDED` protocol error code, added to the runtime ErrorCode enum in spec revision v0.1.25.12 (2026-07-04). Per the spec's ERROR SEMANTICS, HTTP 429 is reserved for server-side throttling/rate limiting (declared on the public evidence/JWKS endpoints); 429 responses carry `error=LIMIT_EXCEEDED` plus the `Retry-After` and `X-RateLimit-Reset` headers, and clients retry after the indicated delay.

- `ErrorCode.LIMIT_EXCEEDED` added in spec declaration order (after `MAX_EXTENSIONS_EXCEEDED`); `TENANT_CLOSED` relocated after it so the client enum mirrors the spec order exactly.
- Retryability decision: **retryable at both layers** — added to `ErrorCode.is_retryable` and to the code tuple in `CyclesProtocolError.is_retryable()`. Rationale: a 429 rate limit is transient by definition and the spec instructs retry; this also preserves prior behavior, where the unrecognized string fell back to `ErrorCode.UNKNOWN` (retryable). The repo's status-based rule (`status >= 500`) does not cover 429, so the code-based classification carries it.
- Shape decision: **enum-only** — no `LimitExceededError` subclass, no lifecycle mapping. This matches the sibling pattern (`BUDGET_FROZEN`/`BUDGET_CLOSED` are enum-only): LIMIT_EXCEEDED is not a reservation-lifecycle denial, so a typed exception class is not warranted.

Forward-compat behavior before this change (verified): `ErrorCode.from_string("LIMIT_EXCEEDED")` returned `ErrorCode.UNKNOWN` — retryable by accident. Now typed and retryable by design; no semantic change.

Retry-After exposure (codex round-3): the client previously dropped the HTTP `Retry-After` header, so the spec's "retry after the indicated delay" was not SDK-visible for header-carried 429s. `retry-after` is now captured in `_RESPONSE_HEADERS` (`client.py`), exposed as `CyclesResponse.retry_after_ms_header` (seconds → ms, non-integer forms ignored), and `_build_protocol_exception` falls back to it for `retry_after_ms` when the body field is absent (body wins when both are present). No auto-retry behavior change — no internal path consumes code-level retryability; the delay is surfaced only.

Tests: enum member + retryable (`test_models.py`), exception-layer retryable with `retry_after_ms` (`test_exceptions.py`), Retry-After header conversion + precedence (`test_response.py`, `test_lifecycle.py`), and an end-to-end 429 LIMIT_EXCEEDED with a real `Retry-After` header through the client (`test_client.py`). Full suite green at 100% coverage.
