# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.3.0]: https://github.com/runcycles/cycles-client-python/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/runcycles/cycles-client-python/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/runcycles/cycles-client-python/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/runcycles/cycles-client-python/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/runcycles/cycles-client-python/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/runcycles/cycles-client-python/releases/tag/v0.1.0
