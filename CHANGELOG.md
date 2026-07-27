# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-07-27

### Fixed

- **Heartbeat redesign (conservative lead lower bound)**: two adversarial review rounds refined the design. The heartbeat maintains `lead_min = Σ measured grants − monotonic elapsed` (grants = differences of successive returned `expires_at_ms` — the same server frame; no cross-clock arithmetic anywhere), starting at 0 so the first extension fires early (bounded by `min(ttl/2, 30s, half the Date-derived hint)`), measuring the real per-extend grant. Cadence derives from the measured grant, so tenant-policy clamps automatically tighten the beat; skip when `lead_min ≥ 1.5×last_grant`. The HTTP `Date` derivation is a first-beat cadence HINT only (per RFC 9110 it is a whole-second best-effort origination timestamp on a possibly different clock — in the reference server `expires_at_ms` comes from Redis TIME) — never load-bearing, never clamped upward. Failed extends retry with the same idempotency key; permanent codes (`RESERVATION_EXPIRED`/`RESERVATION_FINALIZED`/`MAX_EXTENSIONS_EXCEEDED`/`TENANT_CLOSED`/`NOT_FOUND`, raw 404/410) stop the heartbeat; no interval floor. Spec guidance: cycles-protocol#148.
- **Heartbeat (superseded intermediate designs, kept for history)**: the initial alternate-beat fix traded outward drift for inward-drift liveness hazards (single-failure lead-0 retry, guaranteed lapse for ttl < 2s under the old 1s interval floor, RTT slippage) found by adversarial self-review. The heartbeat now estimates its remaining lead from the authoritative `expires_at_ms` the server returns — compared clock-skew-free (server-frame differences + client-monotonic elapsed only) — and extends when lead < 1.5×ttl. It also: derives the **effective TTL** from `expires_at_ms − Date` header (tenant policy `max_reservation_ttl_ms`, default 1h, silently caps grants — a 24h request would otherwise heartbeat 12h late); reuses the same idempotency key when retrying a failed extend (a lost response cannot double-extend); stops permanently on `RESERVATION_EXPIRED`/`RESERVATION_FINALIZED`/`MAX_EXTENSIONS_EXCEEDED`/`TENANT_CLOSED`/`NOT_FOUND`; and drops the 1s interval floor. Spec guidance: cycles-protocol#148.
- **Heartbeat extend drift** (superseded by the redesign above, kept for history): `extend_by_ms` is relative to the reservation's *current* `expires_at_ms` (spec), but the heartbeat extended by the full `ttl_ms` on every `ttl/2` beat — drifting expiry outward by `ttl/2` per beat. A killed process left its reserved budget locked until the drifted expiry (a zombie-reservation window scaling with runtime, bounded only by `max_extensions`), and long runs burned the `max_extensions` budget twice as fast as needed, losing heartbeat protection mid-flight. All four heartbeats (both lifecycles, both streaming context managers) now use alternate-beat extension: extend on the first beat and every second beat after a success, retrying immediately after a failure. Expiry lead stays within `[ttl/2, 1.5×ttl]`; extension consumption is halved. Fleet-wide fix (TS/Java/Rust ship the same change); spec guidance added in cycles-protocol#148.

### Added

- `metadata.actual_source: "estimate"` is stamped on commits whose actual was structurally defaulted from the estimate (`@cycles` without an `actual` expression; streams with no recorded cost or a raised `cost_fn`), so audit evidence distinguishes measured spend from assumed spend. Defaults are unchanged; the marker flows into `/v1/events` recovery bodies via the shared metadata.

## [0.5.0] - 2026-07-27

Durable commit retries. Previously a commit that failed transiently lived only in an in-memory daemon thread (or an unreferenced asyncio task): a process exit — even a clean one — dropped it, and once the reservation's grace period elapsed the server's expiry sweep returned the reserved budget to the pool, permanently under-counting spend that had already happened. Pending commits are now journaled to disk before retry, replayed on the next run, flushed (bounded) at interpreter exit, and — when the reservation has already expired — recovered via `POST /v1/events`, the spec's post-hoc direct-debit endpoint.

### Added

- `runcycles.journal`: file-per-commit `CommitJournal` (atomic write, idempotent replay). Config: `journal_enabled` (default `True`), `journal_dir` (default `~/.runcycles/commit-journal`), `retry_flush_timeout` (default 10 s); env `CYCLES_JOURNAL_ENABLED`, `CYCLES_JOURNAL_DIR`, `CYCLES_RETRY_FLUSH_TIMEOUT`. Records are partitioned into per-identity subdirectories (directories `0700`, files `0600` where supported) keyed by a non-secret PBKDF2-HMAC-SHA256 fingerprint of the server plus principal — the configured `tenant` when set (rotation-safe: any same-tenant credential can settle the records), else the API key — so clients with different servers or principals sharing a journal directory never replay each other's records, and one identity's replay claim cannot starve another's. The first engine created per identity replays surviving entries; corrupt files are renamed `*.corrupt` for operator triage.
- Event fallback: when a commit (first attempt or retry) returns `RESERVATION_EXPIRED`, the SDK posts the spend to `/v1/events` reusing the commit's idempotency key, with `metadata.recovered_reservation_id` / `metadata.recovery_reason` markers and no `overage_policy` (spec default `ALLOW_IF_AVAILABLE` never rejects). Applies to the `@cycles` lifecycles and both streaming context managers. `RESERVATION_FINALIZED` is still treated as settled.
- `flush()` on both retry engines; a process-wide `atexit` hook flushes sync engines under one shared `retry_flush_timeout` deadline (not per engine) so daemon retry threads aren't killed mid-backoff on clean exit and shutdown time stays bounded regardless of engine count.
- Rate-limit awareness end to end: HTTP 429 / `LIMIT_EXCEEDED` on the *first* commit attempt schedules a retry instead of releasing the reservation (a release would return budget for spend that already happened) in all four lifecycle variants, passing the server's `Retry-After` into the engine; on retried commit/event attempts the journal entry is retained and the next attempt waits at least `Retry-After` (consistent with `ErrorCode.is_retryable`). The `Retry-After` floor is persisted in the journal record as an absolute `not_before_ms`, so a restart during a long server-mandated wait does not replay into the window early.
- Authentication failures (401/403) on any commit attempt — first or retried — and on event fallbacks journal the spend instead of releasing or discarding it, so spend recorded during a key misconfiguration or rotation window replays once credentials are fixed.

### Fixed

- Self-review hardening (fleet-wide adversarial review): filename sanitization is ASCII-explicit, matching the TS/Java SDKs, so sibling SDKs sharing a tenant identity directory can always discard records this SDK wrote (and vice versa); the two cross-SDK PBKDF2 fingerprint vectors are now pinned in this suite; a whitespace-only `tenant` falls back to the key principal (matching Java); honored `Retry-After` values and restored journal floors are clamped to 1 hour; HTTP 410 triggers the expired/event-fallback path even when the response body was mangled in transit; a 4xx with no recognizable protocol error code (proxy error pages, forward-compat future codes) is no longer treated as a genuine rejection — the journal entry is retained and the reservation is never released; the base journal directory is also permission-tightened and stale temp files from crashed writers are reaped after 1 hour.
- With `retry_enabled=False`, failed commits were dropped with only a warning; they are now journaled for replay (the old drop behavior remains only when the journal is also disabled).
- `AsyncCommitRetryEngine` created retry tasks without holding a reference, so a pending retry could be garbage-collected mid-flight; task references are now held until completion.
- Commit retries exhausting, or landing after expiry, no longer lose the spend record silently: the journal entry is retained (transient exhaustion) or the event fallback records it (expiry).

---

`TENANT_CLOSED` + `LIMIT_EXCEEDED` error-code support. `TENANT_CLOSED` implements the runtime spec v0.1.25.13 revision of `cycles-protocol-v0.yaml` ([runcycles/cycles-protocol#125](https://github.com/runcycles/cycles-protocol/pull/125)): servers return HTTP 409 `error=TENANT_CLOSED` on reservation create/commit/release/extend when the owning tenant is CLOSED (mirrors governance spec Rule 2). `LIMIT_EXCEEDED` closes the same class of gap for the runtime spec v0.1.25.12 revision (2026-07-04): HTTP 429 rate-limit responses carry `error=LIMIT_EXCEEDED` plus `Retry-After` / `X-RateLimit-Reset` headers.

### Added

- `ErrorCode.TENANT_CLOSED` enum member.
- `TenantClosedError` (subclass of `CyclesProtocolError`), raised at reservation-creation time by the `@cycles` decorator, `CyclesLifecycle`, and streaming reserve paths (the `_build_protocol_exception` surfaces) when the server returns `TENANT_CLOSED`; exported from `runcycles`. Commit/release-time `TENANT_CLOSED` responses follow the existing commit-failure policy (handled/released internally, not raised as typed exceptions); the programmatic client surfaces the code on `CyclesResponse` as usual.
- `CyclesProtocolError.is_tenant_closed()` helper.
- `ErrorCode.LIMIT_EXCEEDED` enum member (spec order, after `MAX_EXTENSIONS_EXCEEDED`), classified retryable by `ErrorCode.is_retryable` and `CyclesProtocolError.is_retryable()` — a 429 rate limit is transient and the spec instructs clients to retry after the indicated delay. Enum-only by design, matching the `BUDGET_FROZEN`/`BUDGET_CLOSED` pattern: it is not a reservation-lifecycle denial, so no exception subclass or lifecycle mapping is warranted.
- `Retry-After` header exposure: the client now captures the HTTP `Retry-After` header (how 429 rate-limit responses carry the delay per the spec) and exposes it as `CyclesResponse.retry_after_ms_header` (seconds → ms; non-integer forms ignored gracefully). `_build_protocol_exception` falls back to it for `retry_after_ms` when the body carries no `retry_after_ms` field (body wins when both are present). No auto-retry behavior change — the delay is surfaced, not consumed.

### Notes

- Purely additive; no wire-format change. Before this release, servers returning `TENANT_CLOSED` were handled via the existing forward-compat path: `ErrorCode.from_string` mapped the unrecognized string to `ErrorCode.UNKNOWN`, so the reservation-creation surfaces raised plain `CyclesProtocolError` with `error_code == "UNKNOWN"` — which `is_retryable()` treats as retryable. With this release the code is recognized, surfaces as `TenantClosedError` with `error_code == "TENANT_CLOSED"`, and is correctly non-retryable.
- `LIMIT_EXCEEDED` previously also fell through to `ErrorCode.UNKNOWN`, which happened to be retryable — so its retry semantics are unchanged; it is now typed instead of accidental. `TENANT_CLOSED` moved to sit after `LIMIT_EXCEEDED` in the enum to mirror the spec's declaration order exactly.

## [0.4.3] - 2026-05-22

Wire-passthrough verification for `expires_from`/`expires_to` and `finalized_from`/`finalized_to` query params on `list_reservations`. Implements `cycles-protocol-v0.yaml` revision 2026-05-22 ([runcycles/cycles-protocol#98](https://github.com/runcycles/cycles-protocol/pull/98)) on the client side; runcycles/cycles-server#163 ships the server impl.

### Added

- Sync + async regression tests confirming `list_reservations` forwards the four new ISO-8601 window params to the URL query string byte-exactly. Unlike `from` (a Python keyword), the new param names are plain kwargs:
  ```python
  client.list_reservations(
      expires_from="2026-05-22T00:00:00Z",
      expires_to="2026-05-23T00:00:00Z",
      finalized_from="2026-05-15T00:00:00Z",
      finalized_to="2026-05-22T00:00:00Z",
  )
  ```

### Notes

- No protocol or wire-format change. Servers older than v0.1.25.21 will silently ignore the params per the additive-parameter guarantee in `cycles-protocol-v0.yaml`.
- 393 tests pass at 100% coverage (gate ≥95%).

## [0.4.2] - 2026-05-21

Wire-passthrough verification for the new `from` / `to` query params on `list_reservations`. Implements `cycles-protocol-v0.yaml` revision 2026-05-21 ([runcycles/cycles-protocol#97](https://github.com/runcycles/cycles-protocol/pull/97)) on the client side; runcycles/cycles-server#160 ships the server impl.

### Added

- Sync + async regression tests confirming `list_reservations` forwards `from` / `to` ISO-8601 date-time values to the URL query string byte-exactly. The client's `**query_params` signature already accepted these — the tests lock that in so future tightening cannot drop the params silently.

### Notes

- **`from` is a Python reserved keyword.** Callers cannot write `client.list_reservations(from="...", to="...")` directly. The supported pattern is the dict-unpack form:
  ```python
  client.list_reservations(**{"from": "2026-05-21T00:00:00Z", "to": "2026-05-22T00:00:00Z"})
  ```
  The wire format is identical; only the Python call-site syntax differs.
- No protocol or wire-format change. Servers older than v0.1.25.20 will silently ignore the params per the additive-parameter guarantee in `cycles-protocol-v0.yaml`.
- 391 tests pass at 100% coverage (gate ≥95%).

## [0.4.1] - 2026-05-08

PyPI metadata refresh for category-search discovery. No code changes; package wire format and API are identical to 0.4.0.

### Changed

- `pyproject.toml`: rewrote `description` to lead with the literal category-search phrase ("Python AI agent budget control — enforce LLM cost limits, tool permissions, and multi-tenant policies before agent actions execute"). Expanded `keywords` from 12 to 21, organized into category-search terms, framework targeting (`langgraph`, `crewai`, `autogen`, `openai-agents`, `mcp`, etc.), and brand. Added `Topic :: Scientific/Engineering :: Artificial Intelligence` classifier for PyPI browse-by-category surfacing.

## [0.4.0] - 2026-04-27

Dynamic subject and action fields on the `@cycles` decorator.

### Added

- Subject fields (`tenant`, `workspace`, `app`, `workflow`, `agent`, `toolset`), action fields (`action_kind`, `action_name`, `action_tags`), and `dimensions` now accept callables in addition to constants. Callables are invoked with the decorated function's `*args, **kwargs` at reservation time, enabling per-call budget routing and dynamic action labeling. Mirrors the Java client's SpEL behavior. (#45)

### Changed

- `_build_reservation_body` signature widened to thread `args` / `kwargs` through to the new `_resolve_value` helper. Internal API only; no protocol or wire-format changes.

## [0.3.0] - 2026-04-08

Add streaming support.

### Added

- Claude settings and git workflow guidelines (#22)
- `CLAUDE.md`, `settings.json`, and SessionStart hook (#24)
- Coverage badge reflecting actual coverage (#32)
- Project URLs for PyPI sidebar links (#34)
- Real integration tests for nightly pipeline (#36)
- `StreamReservation` context manager for streaming DX (#39)

### Changed

- Standardize `CLAUDE.md` and `settings.json`: fix typo, add schema, add gitignore entries (#23)
- Refactor CI workflow to use shared workflow from `.github` repository (#25)
- Analyze codebase metrics (#26)
- Improve package metadata and discoverability (#33)
- Bump `actions/upload-artifact` from 4 to 7 (#31)
- Bump `actions/checkout` from 4 to 6 (#30)
- Bump `actions/setup-python` from 5 to 6 (#29)
- Bump `actions/download-artifact` from 4 to 8 (#28)

### Fixed

- Contract test UTF-8 encoding for Windows compatibility (#27)
- API response codes and parameter names in integration tests (#37)
- Guard `requests` import so CI collection doesn't fail (#38)

## [0.2.0] - 2026-03-24

Bug fixes, support for 0.1.24 spec, more tests.

### Added

- Comprehensive integration examples for Cycles Python client (#9)
- API key creation instructions to README (#13)
- Badges to README for PyPI, CI, and License (#15)
- Documentation links to README (#16)
- Documentation for nested `@cycles` decorator behavior and best practices (#17)
- Budget state and extension error codes, charged amount to response (#20)

### Changed

- Raise test coverage threshold from unconfigured to 95% (#10)
- Move coverage config to `[tool.coverage]` so pytest works without pytest-cov (#12)
- Analyze spring issue (#18)
- Default overage policy from `REJECT` to `ALLOW_IF_AVAILABLE` (#19)
- Bump version to 0.2.0 for protocol v0.1.24 (#21)

### Removed

- Redundant `--cov-fail-under=85` from CI workflow (#11)

### Fixed

- Broken docs URLs and add API key comment to examples (#14)

## [0.1.3] - 2026-03-15

Minor updates, bug fixes, test coverage.

### Added

- Comprehensive audit report and code quality improvements (#7)
- Enforce 85% pytest coverage threshold in CI (#8)

### Changed

- Review Python cycles client (#5)

### Fixed

- Close all coverage gaps, achieve 100% coverage (#6)

## [0.1.2] - 2026-03-13

Cleanup, bug fixes, spec alignment, test coverage.

### Added

- Comprehensive test coverage and input validation (#2)
- Validate Python client (#4)

### Fixed

- Enforce spec-required fields and fix estimate validation (#3)

## [0.1.1] - 2026-03-12

### Changed

- Minor doc updates.

## [0.1.0] - 2026-03-12

Initial public release.

### Added

- Comprehensive error handling and improved API model validation (#1)

[0.4.1]: https://github.com/runcycles/cycles-client-python/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/runcycles/cycles-client-python/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/runcycles/cycles-client-python/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/runcycles/cycles-client-python/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/runcycles/cycles-client-python/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/runcycles/cycles-client-python/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/runcycles/cycles-client-python/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/runcycles/cycles-client-python/releases/tag/v0.1.0
