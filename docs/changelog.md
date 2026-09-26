# Changelog

These pages describe `main`. Released versions remain available through
[GitHub releases](https://github.com/dariuszpanas/iperf3-lib/releases) and
[PyPI](https://pypi.org/project/iperf3-lib/).

## Unreleased

- Add finite sequential trial plans with detached native settings, explicit
  admission budgets, warm-up runs, between-run pauses, retained failures and
  unstarted records, and no hidden retries.
- Add median bytes/time assessments with minimum sample counts, retained
  baselines, explicit compatibility policies and absolute/relative tolerance.
  Separate performance acceptance from execution success and provide pure CI
  classification plus strict report-v1 JSON, text and JUnit output.

- Add explicit per-stream/aggregate-per-direction rate intent, exact SI unit
  parsing, native allocation provenance, and sequential admission estimates.
  Preserve the original request in an artifact extension and record the resolved
  low-level config separately from verified native settings.
- Add capability reports separating wrapper coverage, ABI declarations, native
  symbols, tested environments, and supplied execution evidence. Offline import
  and reports do not load libiperf; legacy capability flags remain lazy.
- Add duration-weighted interval stability, measured bytes/time throughput,
  stream balance/scaling, explicit comparison policies and directional asymmetry.
- Preserve qualified TCP seconds/bytes and endpoint process CPU evidence with
  exact provenance; extend the unreleased artifact-v1 development schema.
- Keep omissions, coverage gaps, unqualified producers and insufficient data
  explicit. Analysis uses the standard library and does not execute benchmarks.
- Replace Pydantic runtime models with standard-library dataclasses and explicit
  `ClientConfig` validation. Runtime dependencies now consist of CFFI.
- Add normalized directional flow and interval models alongside the original
  native JSON, execution metadata, and a `to_dict()` helper.
- Add strict version-1 JSON artifacts with producer identity, portable import,
  explicit legacy snapshot conversion, configuration evidence, structured
  diagnostics, per-stream summaries, and interval bytes/duration/warm-up state.
- Snapshot and revalidate configuration before each client run. Preserve native
  failure JSON and record observed UTC and monotonic timing separately from
  estimates derived from saved native output.
- Qualify both reporting endpoints across TCP, UDP, and SCTP directions and
  warm-up intervals. Keep mixed UDP stream summaries unattributed and mark
  unsupported SCTP retransmission values unavailable on qualified native versions.
- Add a dependency-free Prometheus renderer and atomic textfile writer.
- Emit each Prometheus metric family's metadata once, group its samples, and
  reject reserved caller labels and duplicate samples. Preserve existing files
  on failed atomic replacement.
- Read libiperf's native `bidir` flag so simultaneous bidirectional runs retain
  both flows. Keep the `bidirectional` spelling compatible and reject conflicts.
- Preserve missing throughput and interval boundaries as `None`, retain measured
  zero values, and omit unavailable Prometheus measurements. Keep reverse
  summary directions consistent and diagnose ambiguous bidirectional stream
  mappings. Saved native errors and incomplete output no longer appear successful.
- Add a [Docker Desktop Kubernetes/Grafana example](guides/grafana.md) with
  native JSON-to-metric comparison and success/failure freshness checks.
- Refresh development dependencies and use current stable uv in CI and Docker.
- Adopt YAGA commit, workflow, repository, and source/test-change policies here.
- Add a Zensical site, GitHub Pages deployment, documentation checks, and
  an issue-based roadmap.

### Migration from 0.2.0

The [dedicated migration guide](guides/migration-0.3.md) gives API substitutions,
strict-input examples, and separate paths for native JSON, Pydantic dumps,
development snapshots, and versioned artifacts.

Pydantic APIs such as `model_dump()`, `model_validate()`, and Pydantic validation
errors are no longer provided by these models. Use dataclass constructors,
`Result.to_dict()`, or `dataclasses.asdict()` as appropriate. Configuration
validation raises `TypeError` or `ValueError`; unsupported native features raise
`UnsupportedFeatureError` when applied. Numeric strings and booleans are not
accepted as integer configuration values. See [configuration](reference/configuration.md).

`SumStats.bits_per_second` and interval boundaries can now be `None` when
native measurements are absent. Check for `None` before arithmetic. The
legacy `summary_mbps` property still falls back to `0.0` when unavailable,
but it preserves a genuine zero instead of skipping to another endpoint.
Saved native errors and empty/incomplete documents produce failed results.
Applications importing bidirectional JSON without reporting-endpoint evidence
can pass `reporting_role="client"` or `"server"`; unproven stream direction
is reported as `"unknown"` with diagnostics.

`Result.to_dict()` is a new unversioned dataclass snapshot helper, including
native JSON. Use the [artifact API](guides/artifacts.md) for durable storage.
`artifact_from_legacy_dict()` imports only the documented historical development
snapshot shape; published Pydantic dumps need an explicit migration preserving
their original outcome. Timing is new since published 0.2.0. Saved native JSON
keeps inferred completion separately in `execution.timing`; Prometheus freshness
requires an observed completion event. Unverified completion fields in older
development snapshots remain estimates after conversion.

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
