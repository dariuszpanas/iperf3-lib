# Changelog

These pages describe `main`. Released versions remain available through
[GitHub releases](https://github.com/dariuszpanas/iperf3-lib/releases) and
[PyPI](https://pypi.org/project/iperf3-lib/).

## Unreleased

- Add owned typed live-event streams through `Client.events()` and
  `Server.events_once()`, retaining complete-result access and awaiting cleanup
  after consumer abandonment or cancellation. Separate native measurement time,
  callback arrival, flow association, and wrapper terminal status.
- Move native JSON parsing and result assembly outside C callbacks. Bound
  copied payloads, queued bytes, retained evidence, and consumer delivery;
  distinguish capture loss from progress loss and mark lossy minimum-version
  reconstruction incomplete. Existing `NativeEvent` and report schemas remain
  unchanged.
- Add exploratory bounded adaptive UDP experiments with explicit admission
  limits, confirmation measurements, retained non-monotonic and failed
  observations, and standalone reports of tested operating points.
- Preserve bounded observed events when a sequential async trial encounters a
  worker crash, transport failure or another execution exception. Keep completed
  artifacts and unstarted trials in the history, and retain both event and
  unencodable-result diagnostics in JUnit. Existing v2 archives and fields remain
  unchanged; earlier strict readers must be upgraded for new exception records
  carrying events.
- Add opt-in bounded concurrent plans with worker and aggregate target-rate
  admission limits, endpoint and caller-resource exclusion, per-cell ordering,
  and cleanup-safe multi-worker cancellation. Standalone schema-v3 reports retain
  reservation and overlap histories. This mode requires zero pauses and rejects
  fixed client ports; sequential APIs and v1/v2 reports remain unchanged.
- Add owned sequential `arun_plan` execution with an overall deadline, cleanup-safe
  cancellation, retained completed artifacts, bounded interrupted-trial events
  and explicit unstarted records. Add strict standalone plan-execution-v2 JSON,
  text and JUnit reports while preserving assessment-v1 and sweep-v1 contracts.
- Version and bound isolated worker messages, validate session identities and
  ordering, retain readiness receipts, and require clean output shutdown before
  returning a completed result. Reserve control capacity under event overload;
  reject oversized results explicitly.
- Protect isolated Linux workers with a parent-death signal before loading the
  native library. Qualify worker lifetime and cancellation from installed wheels
  and sdists across the supported Python/libiperf matrix.
- Always isolate `Client.arun()` and propagate cancellation through client/server
  worker cleanup, including repeated cancellation and completion/deadline races.
  Preserve synchronous execution and cooperative `Server.stop()` behavior.
- Report unconfirmed process cleanup through `IperfCleanupError`, retaining
  ownership until the worker is reaped. Active callbacks still delay API return.
- Async methods use the built-in worker path instead of overridden synchronous
  methods; setup errors follow existing worker exception semantics.
- Simplify the public documentation and navigation; keep maintainer procedures
  and detailed design notes in the repository.
- Allow the Kubernetes/Grafana example to use the chosen kubeconfig context.

## 0.3.0 — 2026-09-27

Published on [PyPI](https://pypi.org/project/iperf3-lib/0.3.0/) and as
[GitHub release v0.3.0](https://github.com/dariuszpanas/iperf3-lib/releases/tag/v0.3.0).

### Expanded native controls and worker execution

- Add typed client controls and `ServerConfig` for local address/device binding,
  address family, source ports, transport tuning, count termination, pacing,
  payloads, connection policies, authentication and server policies. The
  [option reference](reference/native-options.md) maps all flags from the two
  supported native versions, including application-owned CLI concerns.
- Execute expanded controls, MPTCP and streaming through an isolated Python/CFFI
  worker using libiperf's public parser. Basic client configurations retain the
  direct native path; no `iperf3` executable is required by the worker.
- Add explicit execution timeouts and typed parent-side events with bounded
  delivery, drop accounting and independent final-result retention.
- Return normalized server results and expose sequential result callbacks;
  preserve the concise address/port constructor and cooperative stop behavior.
- Preserve expanded request metadata and native getter receipts while keeping
  passwords outside configuration/artifact metadata. Existing artifacts with
  the original configuration fields remain readable.

### Finite experiments and assessments

- Add [finite parameter sweeps](guides/sweeps.md) with preflight budgets, recorded cell order,
  per-cell warm-ups and measured trials, shared sequential execution, and full
  failure retention. Verify declared axes and native rate allocations before
  qualifying receiver medians or explicit method/direction comparisons.
- Add strict sweep-v1 reports preserving every artifact, setting check and
  exclusion; validate frozen selection and arithmetic without rerunning analysis.
- Add finite sequential trial plans with detached native settings, explicit
  admission budgets, warm-up runs, between-run pauses, retained failures and
  unstarted records, and no hidden retries.
- Add median bytes/time assessments with minimum sample counts, retained
  baselines, explicit compatibility policies and absolute/relative tolerance.
  Separate performance acceptance from execution success and provide pure CI
  classification plus strict report-v1 JSON, text and JUnit output.

### Configuration intent and capability evidence

- Add explicit per-stream/aggregate-per-direction rate intent, exact SI unit
  parsing, native allocation provenance, and sequential admission estimates.
  Preserve the original request in an artifact extension and record the resolved
  low-level config separately from verified native settings.
- Add capability reports separating wrapper coverage, ABI declarations, native
  symbols, tested environments, and supplied execution evidence. Offline import
  and reports do not load libiperf; legacy capability flags remain lazy.

### Measurement analysis

- Add duration-weighted interval stability, measured bytes/time throughput,
  stream balance/scaling, explicit comparison policies and directional asymmetry.
- Preserve qualified TCP seconds/bytes and endpoint process CPU evidence with
  exact provenance in the artifact-v1 format published with 0.3.0.
- Keep omissions, coverage gaps, unqualified producers and insufficient data
  explicit. Analysis uses the standard library and does not execute benchmarks.

### Canonical results and artifacts

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

### Operational metrics and result correctness

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
- Add a [Kubernetes/Grafana example](guides/grafana.md) with
  native JSON-to-metric comparison and success/failure freshness checks.

### Development tooling

- Add installed-package checks and retained wheel/sdist artifacts to release CI.
- Reuse a stable staging directory for local Docker validation.
- Refresh development dependencies and use current stable uv in CI and Docker.
- Adopt YAGA commit, workflow, repository, and source/test-change policies here.
- Add a Zensical documentation site with GitHub Pages deployment and link checks.

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
