# Changelog

These pages describe `main`. Released versions remain available through
[GitHub releases](https://github.com/dariuszpanas/iperf3-lib/releases) and
[PyPI](https://pypi.org/project/iperf3-lib/).

## Unreleased

- Replace Pydantic runtime models with standard-library dataclasses and explicit
  `ClientConfig` validation. Runtime dependencies now consist of CFFI.
- Add normalized directional flow and interval models alongside the original
  native JSON, execution metadata, and a `to_dict()` helper.
- Add a dependency-free Prometheus renderer and atomic textfile writer.
  Normalization and exporter conformance still have open pre-release criteria;
  see the [roadmap](roadmap.md).
- Refresh development dependencies and use current stable uv in CI and Docker.
- Adopt YAGA commit, workflow, repository, and source/test-change policies here.
- Add a Zensical site, GitHub Pages deployment, documentation checks, and
  an issue-based roadmap.

### Migration from 0.2.0

Pydantic APIs such as `model_dump()`, `model_validate()`, and Pydantic validation
errors are no longer provided by these models. Use dataclass constructors,
`Result.to_dict()`, or `dataclasses.asdict()` as appropriate. Configuration
validation raises `TypeError` or `ValueError`; unsupported native features raise
`UnsupportedFeatureError` when applied. Numeric strings and booleans are not
accepted as integer configuration values. See [configuration](reference/configuration.md).

`Result.to_dict()` returns the dataclass fields, including native JSON. A
versioned interchange schema and importer have not been introduced; see
[results](guides/results.md) for storing current snapshots.

## 0.2.0 — 2026-07-22

- Correctly apply and verify TCP, UDP, and SCTP selection.
- Fix server bind-address handling, lazy-load libiperf, and keep JSON output out
  of the host process's stdout.
- Validate native option limits and reject unsupported MPTCP and streaming JSON.
- Add Python 3.14 and libiperf 3.21 support, refreshed dependencies, reproducible
  CI/release tooling, and stronger native integration coverage.

This release uses Pydantic configuration and result models. See the
[tagged README](https://github.com/dariuszpanas/iperf3-lib/blob/v0.2.0/README.md).

## 0.1.0

- Initial CFFI ABI wrapper for libiperf clients and servers.
- Pydantic configuration/results and asynchronous convenience methods.
- Docker compatibility tests and PyPI release automation.
