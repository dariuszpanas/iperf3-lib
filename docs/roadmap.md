# Roadmap

The project direction is a programmable network benchmarking and analysis
library powered by libiperf. Native code generates traffic and measurements;
the Python layer makes the results easier to interpret, compare, automate,
and publish.

This roadmap records proposals to review before the next release. It does
**not** promise that every proposal will ship in that release. Release scope
and a version will be chosen after the review, with a decision to include,
defer, investigate, or decline each area. GitHub issues carry the actionable
work and acceptance criteria.

## What exists today

Documentation tracks the development branch. The following foundation is on
`main`; it includes changes made after the published 0.2.0 release.

| Area | Current foundation | Remaining work |
| --- | --- | --- |
| Configuration | Dataclasses with explicit type/value validation and a direct CFFI execution path. | Intent-based rates, profiles, verified effective configuration, and a structured capability report. |
| Results | Directional flow summaries, aggregate and per-stream interval rates, timestamps, retained native JSON, and a diagnostics container. | Correct edge-case direction/missing-data handling; add a durable schema, richer stream/interval metadata, and populated diagnostics. |
| Serialization | `Result.to_dict()` returns dataclass fields as a dictionary. | A versioned artifact contract and documented compatibility/reading behavior. |
| Metrics | Latest-run Prometheus gauges, freshness fields, and atomic textfile replacement. | Exposition conformance and failure-path qualification before release. |
| Execution | Synchronous APIs and asynchronous convenience methods using executor threads. | Live events and isolated execution for stronger deadline, cancellation, or concurrency guarantees. |
| Analysis and plans | Applications can compose individual runs. | Built-in statistics, repeated trials, assessments, baselines, and bounded sweeps. |

The richer result model remains a foundation in progress. In particular,
missing throughput in a present summary can currently become zero, reverse
nested observations can retain the wrong direction, and bidirectional stream
intervals need more precise direction mapping. These are tracked correctness
work, not stable analysis semantics to build on blindly.

## 1. Documentation and release decisions

Tracking: [documentation setup #25](https://github.com/dariuszpanas/iperf3-lib/issues/25)
and [next-release scope review #26](https://github.com/dariuszpanas/iperf3-lib/issues/26).

Set up installation and usage guides, reference material, contributor and
release instructions, a strict documentation build, and GitHub Pages
deployment. Use issues to track decisions and connect implementation PRs
to their acceptance criteria.

Before the next release, review every area below and record the selected
scope. Correctness gaps affecting advertised features need an explicit
resolution. Review the Pydantic-to-dataclass migration, supported native
versions, and installed/public documentation as part of release qualification.

## 2. Results and portable artifacts

Tracking: [normalization correctness #27](https://github.com/dariuszpanas/iperf3-lib/issues/27)
and [canonical artifacts #28](https://github.com/dariuszpanas/iperf3-lib/issues/28).

First correct direction and missing-measurement semantics. Then define one
canonical model that exporters and analysis can consume without independently
interpreting native JSON.

The proposed artifact includes:

- Requested configuration, execution metadata, and available native-version
  and platform information.
- Distinct flow directions and sender/receiver observations of each flow.
  Those observations describe one transfer and must not be summed together.
- Per-stream summaries and intervals with bytes, elapsed duration, and
  omitted/warm-up state.
- Data-quality diagnostics and explicit absent, unsupported, failed, and
  incomplete states.
- The original native JSON and a versioned normalized JSON representation
  with a documented evolution policy.

Fixture coverage across protocols, directions, and supported native versions
is part of that contract. Actual completion metadata should remain
distinguishable from values inferred from a requested duration.

## 3. Operational metrics

Tracking: [Prometheus and textfile qualification #29](https://github.com/dariuszpanas/iperf3-lib/issues/29).

Qualify the current Prometheus renderer and textfile writer before release.
One metric family needs consistent metadata, labels, units, and missing-data
behavior across all its samples. Validate complete exposition output and
atomic replacement error paths.

Exercise the full integration with a real native benchmark, node_exporter's
textfile collector, Prometheus ingestion, and a local Grafana datasource and
rendered dashboard. Keep the setup and metric/freshness evidence reproducible.
The local qualification uses Docker Desktop's `docker-desktop` Kubernetes
context in the dedicated `iperf3-lib-observability` namespace.

Keep the integration pattern explicit:

```text
Scheduled benchmark -> completed result -> cached metrics -> scrape
```

Latest-run measurements remain gauges in base units, with completion and
last-success timestamps represented as values. Failed runs must not present
stale throughput as the latest measurement. Labels identify stable targets or
profiles rather than unique run IDs, timestamps, or error messages.

A consuming application may serve the rendered text or use a node_exporter
textfile collector. An HTTP daemon is outside this core scope. Any future
cumulative counters need a separately defined lifetime and must advance per
completed run, not per scrape.

## 4. Configuration intent and capability reporting

Tracking: [configuration and capability design #30](https://github.com/dariuszpanas/iperf3-lib/issues/30).

Evaluate per-stream bitrate versus aggregate bitrate per direction, explicit
bidirectional budgets, named profiles, unit-aware inputs, and plan-wide
duration/traffic limits. Reject contradictory intent and record the derived
native settings.

Requested settings and verified effective settings should be separate.
Verification may be unavailable; the result must say so. Capability reporting
should distinguish wrapper support, native symbol availability, tested
platform/version constraints, and runtime outcomes.

Additional wrapper coverage may include pacing, socket buffers,
congestion-control selection, and server output. These expose native
functionality. Higher-level analysis and experiments are a separate
contribution. Every accepted configuration field must be applied exactly once
or rejected explicitly, with native behavior checked through returned JSON
or a matching getter.

## 5. Analysis and diagnostic evidence

Tracking: [analysis design #31](https://github.com/dariuszpanas/iperf3-lib/issues/31).

Proposed independent analysis functions include:

- **Throughput stability:** minimum/median interval throughput, variability,
  and time below a chosen threshold. Exclude omitted warm-up intervals,
  account for unequal duration, and derive overall throughput from bytes
  and elapsed time.
- **Stream balance and scaling:** compare per-stream measurements and find
  the smallest tested stream count reaching a configurable fraction of the
  best observed throughput.
- **Directional asymmetry:** compare recorded methodologies while keeping
  sequential forward/reverse tests distinct from simultaneous bidirectional
  tests.
- **Diagnostics:** retain available TCP RTT/window and endpoint CPU evidence,
  separating observations, derivations, and hypotheses.

An interval-average percentile is not a packet-performance percentile.
Retransmissions are not an exact packet-loss percentage, jitter is not
application latency, and a small collection of throughput observations does
not establish the physical bottleneck.

## 6. Repeated trials, baselines, and CI assessments

Tracking: [trial plans and assessment reports #32](https://github.com/dariuszpanas/iperf3-lib/issues/32).

A proposed plan records repetitions, warm-up policy, pauses/cooldowns,
budgets, and acceptance criteria. Keep every trial, including failures;
retries must never conceal unreliable execution.

Execution success and performance acceptance are separate outcomes.
Insufficient evidence should be inconclusive. Baseline comparisons check
protocol, direction/methodology, stream count, duration, and relevant
configuration unless a deliberate difference is recorded.

Reports should retain the individual artifacts and assessment method,
with versioned machine-readable output and evaluated CI formats such as
JUnit XML. Define how execution failure, performance rejection, and
inconclusive evidence map to CI outcomes.

## 7. Bounded experiments

### Parameter sweeps

Tracking: [bounded parameter sweeps #33](https://github.com/dariuszpanas/iperf3-lib/issues/33).

Explore finite matrices such as stream count, direction, and repetitions.
Record order and any randomization seed, provide cooldowns, and enforce
total duration and traffic limits. Preserve failed cells and partial
completion instead of returning only the winning result.

### Adaptive UDP exploration

Tracking: [adaptive UDP exploration #34](https://github.com/dariuszpanas/iperf3-lib/issues/34).

Explore a bounded offered-load sweep with repeated trials around promising
rates. Do not assume observations are perfectly monotonic. Report requested
load, achieved sender/receiver rates, loss, and variation as a **tested
operating range**, not the network's exact physical capacity.

Both ideas build on trial plans and configuration intent. Run plans
sequentially under the current execution contract.

## 8. Live events and isolated execution

Tracking: [typed live events #35](https://github.com/dariuszpanas/iperf3-lib/issues/35)
and [isolated execution #36](https://github.com/dariuszpanas/iperf3-lib/issues/36).

A typed live-event API is proposed separately from the complete-result API.
Native callbacks should copy and enqueue quickly, with parsing and user
callbacks outside the native callback. Queue bounds, event loss, malformed
events, and consumer abandonment need explicit contracts.

Reliable deadlines, cancellation, and concurrent plans need an isolation
design. Explore bounded Python worker processes calling libiperf through
CFFI, with clear lifecycle ownership and result transport. Forced termination
must produce an incomplete outcome; it cannot manufacture a final native
summary.

Until those designs are implemented and qualified, cancelling an await does
not stop the blocking native call and concurrent native operations within
one process remain unsupported.

## Working principles

Keep the runtime core small. Analysis and exporters consume the shared
result model independently; use standard-library JSON, statistics, CSV, XML,
and process facilities where sufficient. These proposals do not require
Pydantic or a web framework.

Build in dependency order: result correctness and artifacts, qualified
metrics, analysis and repeated trials, then advanced experiments and
execution. Review value and compatibility at each stage instead of treating
the entire roadmap as one implementation commitment.

## Issue tracking

The [project issue tracker](https://github.com/dariuszpanas/iperf3-lib/issues)
is the source for current status, decisions, and implementation links. This
page explains the direction; issues hold actionable scope and completion
criteria.

| Work | Issue | Implementation dependencies |
| --- | --- | --- |
| Documentation and contribution workflow | [#25](https://github.com/dariuszpanas/iperf3-lib/issues/25) | Current foundation work. |
| Review and choose release scope | [#26](https://github.com/dariuszpanas/iperf3-lib/issues/26) | Review every proposal; implementation of every proposal is not required. |
| Normalization correctness | [#27](https://github.com/dariuszpanas/iperf3-lib/issues/27) | Qualify the existing model first. |
| Canonical schema and versioned JSON | [#28](https://github.com/dariuszpanas/iperf3-lib/issues/28) | #27 |
| Prometheus and textfile qualification | [#29](https://github.com/dariuszpanas/iperf3-lib/issues/29) | #27 |
| Configuration intent and capabilities | [#30](https://github.com/dariuszpanas/iperf3-lib/issues/30) | No implementation prerequisite selected. |
| Analysis and diagnostics | [#31](https://github.com/dariuszpanas/iperf3-lib/issues/31) | #28 |
| Repeated trials, baselines, and CI reports | [#32](https://github.com/dariuszpanas/iperf3-lib/issues/32) | #28, #30, #31 |
| Parameter sweeps | [#33](https://github.com/dariuszpanas/iperf3-lib/issues/33) | #30, #32 |
| Adaptive UDP exploration | [#34](https://github.com/dariuszpanas/iperf3-lib/issues/34) | #33 |
| Live events | [#35](https://github.com/dariuszpanas/iperf3-lib/issues/35) | #27 |
| Isolated execution | [#36](https://github.com/dariuszpanas/iperf3-lib/issues/36) | #28 |

Dependencies describe implementation order. Design discussions can proceed
in parallel, and issue status remains authoritative as plans evolve.

