# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

`TENANT_CLOSED` error-code support. Implements the runtime spec v0.1.25.13 revision of `cycles-protocol-v0.yaml`, which adds `TENANT_CLOSED` to the ErrorCode enum: servers return HTTP 409 `error=TENANT_CLOSED` on reservation create/commit/release/extend when the owning tenant is CLOSED (mirrors governance spec Rule 2).

### Added

- `ErrorCode.TENANT_CLOSED` enum member.
- `TenantClosedError` (subclass of `CyclesProtocolError`), raised by the lifecycle/decorator surfaces when the server returns `TENANT_CLOSED`; exported from `runcycles`.
- `CyclesProtocolError.is_tenant_closed()` helper.

### Notes

- Purely additive; no wire-format change. Before this release, servers returning `TENANT_CLOSED` were handled via the existing forward-compat path: `ErrorCode.from_string` mapped the unrecognized string to `ErrorCode.UNKNOWN`, so the lifecycle surfaces raised plain `CyclesProtocolError` with `error_code == "UNKNOWN"` — which `is_retryable()` treats as retryable. With this release the code is recognized, surfaces as `TenantClosedError` with `error_code == "TENANT_CLOSED"`, and is correctly non-retryable.

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
