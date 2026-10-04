# Roadmap

This is retained planning background from the 0.3.0 development cycle.
[GitHub issues](https://github.com/dariuszpanas/iperf3-lib/issues) are the source
for current scope and completion status; the tables below preserve the planning
context rather than certify the current release candidate.

## Next release: 0.4.0

The current development scope adds owned async and bounded concurrent plans,
adaptive UDP experiments, and typed live-event consumption. Issues
[#36](https://github.com/dariuszpanas/iperf3-lib/issues/36) and
[#34](https://github.com/dariuszpanas/iperf3-lib/issues/34) are complete with
retained exact-source native and installed-package qualification. The
[live-event contract](../docs/guides/live-events.md) is tracked in
[#35](https://github.com/dariuszpanas/iperf3-lib/issues/35).

Python 3.15 support and qualification are tracked in
[#63](https://github.com/dariuszpanas/iperf3-lib/issues/63). Final 0.4.0 release
preparation must qualify the combined source, retained wheel and sdist,
documentation, and Grafana integration. The `Unreleased` changelog is a
development record; it does not establish that these features are published.

## Historical 0.3.0 planning

The project direction is a programmable network benchmarking and analysis
library powered by libiperf. Native code generates traffic and measurements;
the Python layer makes the results easier to interpret, compare, automate,
and publish.

The selected **0.3.0** scope builds on the dataclass migration with portable
measurement evidence, deterministic analysis, operational metrics, finite
sequential trials, and explicit parameter sweeps. GitHub issues carry acceptance
criteria and implementation evidence. A selected feature is not a completed
release qualification, and candidate metadata does not establish publication.

## 0.3.0 dispositions

| Area | Decision | Evidence or remaining work |
| --- | --- | --- |
| Dataclasses and result correctness | Include | Strict inputs, explicit missing values, reporting roles, direction and lifecycle qualification; [#27](https://github.com/dariuszpanas/iperf3-lib/issues/27). |
| Portable result artifacts | Include | Artifact v1 preserves native/normalized evidence, settings and timing; [#28](https://github.com/dariuszpanas/iperf3-lib/issues/28). |
| Operational metrics | Include | Snapshot gauges, freshness and atomic textfiles; [#29](https://github.com/dariuszpanas/iperf3-lib/issues/29). Repeat Grafana qualification for the final candidate. |
| Configuration and capabilities | Include | Explicit rate intent, SI units, admission estimates and layered capability reports; [#30](https://github.com/dariuszpanas/iperf3-lib/issues/30). Profiles remain application-owned. |
| Analysis and diagnostics | Include | Duration-aware statistics, scaling/asymmetry and qualified TCP/CPU evidence; [#31](https://github.com/dariuszpanas/iperf3-lib/issues/31). |
| Trials, baselines and CI reports | Include | Finite sequential execution, retained failures, explicit assessments and frozen report v1; [#32](https://github.com/dariuszpanas/iperf3-lib/issues/32). |
| Explicit parameter sweeps | Include | [Finite reproducible cells](../docs/guides/sweeps.md) with observed setting qualification and retained partial outcomes; [#33](https://github.com/dariuszpanas/iperf3-lib/issues/33). |
| Adaptive UDP selection | Defer beyond 0.3.0 | Controlled impaired-link and non-monotonic operating-range qualification remains open; [#34](https://github.com/dariuszpanas/iperf3-lib/issues/34). |
| Expanded native controls | Add under #53 | [Complete option coverage](../docs/reference/native-options.md), typed client/server configuration, bound listeners and native behavior need combined qualification; [#53](https://github.com/dariuszpanas/iperf3-lib/issues/53). |
| Typed live events | First bounded worker API under #53 | Callback delivery and final results have separate contracts; broader stress and abandonment criteria remain in [#35](https://github.com/dariuszpanas/iperf3-lib/issues/35). |
| Isolated execution | Worker required for expanded native parsing | Python/CFFI process ownership and explicit timeouts are implemented; broader lifecycle qualification remains in [#36](https://github.com/dariuszpanas/iperf3-lib/issues/36). |
| Documentation and qualification | Include | Migration guide, public docs, YAGA policies, stable local Docker staging and retained distribution checks; [#25](https://github.com/dariuszpanas/iperf3-lib/issues/25), [#38](https://github.com/dariuszpanas/iperf3-lib/issues/38), [#43](https://github.com/dariuszpanas/iperf3-lib/issues/43). Final candidate evidence remains required. |

The experiment scope remains finite sequential trials and explicit sweeps.
Adaptive UDP selection is deferred. The later native-option review added an
isolated worker and bounded event API because safe native-parser integration
needs process ownership. The [execution guide](../docs/guides/native-controls.md)
defines that concrete contract; the [advanced design](design/advanced-execution.md)
retains broader qualification criteria. Implementation alone does not close
those issues or establish release readiness for the combined revision.

## What exists today

Documentation tracks the development branch. The following foundation is on
`main`; it includes changes made after the published 0.2.0 release.

| Area | Current foundation | Remaining work |
| --- | --- | --- |
| Configuration | Validated client/server dataclasses, rate intent and admission estimates, expanded native controls and layered capability reports. | Qualify the combined option coverage; retain application-owned profiles and defer asymmetric simultaneous budgets. |
| Results | Directional and per-stream summaries, interval scope/bytes/duration/warm-up metadata, explicit missing values, and execution provenance. | Preserve this evidence contract as analysis and experiments expand. |
| Serialization | Strict version-1 result artifacts, assessment and sweep reports, portable loading, and retained trial/cell evidence. | Preserve frozen interpretation and historical producers as new contracts evolve. |
| Metrics | Latest-run Prometheus gauges, omitted unavailable values, freshness fields, atomic replacement, and a local Grafana qualification fixture. | Repeat integration qualification for the final release candidate. |
| Execution | Basic direct clients plus isolated Python/CFFI workers for expanded controls, servers, event delivery and explicit timeouts. | Preserve direct-call limits; complete the broader stress, cancellation and cleanup criteria separately. |
| Analysis | Measured throughput, interval stability, stream balance/scaling, directional comparisons, and retained transport/CPU evidence. | Repeat installed analysis and report qualification for the final candidate. |
| Plans | Finite sequential trials, retained baselines, median assessments, and bounded sweeps with verified cell settings and versioned reports. | Final candidate qualification; advanced adaptive selection remains deferred. |

Result correctness is covered by captures from both reporting endpoints on
the minimum and latest libiperf versions. Missing values remain distinct from
zero, reverse summaries agree, and bidirectional stream mapping uses explicit
native role evidence. Richer aggregation metadata and a durable
[artifact contract](../docs/guides/artifacts.md) provide the foundation for analysis.

## 1. Documentation and release decisions

Tracking: [documentation setup #25](https://github.com/dariuszpanas/iperf3-lib/issues/25)
and [next-release scope review #26](https://github.com/dariuszpanas/iperf3-lib/issues/26).

Installation and usage guides, reference material, contributor/release
instructions, strict documentation checks, and GitHub Pages are implemented.
Issues connect scope decisions to implementation PRs and acceptance evidence.

The [migration guide](../docs/guides/migration-0.3.md) was checked against the published
0.2.0 models. Final 0.3.0 qualification still requires the exact combined candidate:
source and installed-native tests, public documentation, and real Grafana
integration. [Release instructions](releasing.md) keep preparation, qualification,
and separately authorized publication explicit.

## 2. Results and portable artifacts

Tracking: [normalization correctness #27](https://github.com/dariuszpanas/iperf3-lib/issues/27)
and [canonical artifacts #28](https://github.com/dariuszpanas/iperf3-lib/issues/28).

Implemented normalization distinguishes direction, observation, missing data,
and measured zero. Exporters and analysis consume one canonical model, with
portable artifact import preserving its recorded interpretation and native JSON.

The version-1 artifact includes:

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

Fixtures cover protocols, directions, both reporting endpoints, and supported
native versions. Observed completion stays separate from estimates inferred
from saved native output. Historical artifact producer identities remain part
of the preserved evidence when the package version changes.

## 3. Operational metrics

Tracking: [Prometheus and textfile qualification #29](https://github.com/dariuszpanas/iperf3-lib/issues/29).

The renderer and textfile writer have qualified metric-family metadata,
labels, units, missing-data behavior, complete exposition and atomic replacement
error paths. The final release candidate must repeat the integration with real
native benchmarks, node_exporter's textfile collector, Prometheus ingestion,
and an API-checked and rendered Grafana dashboard. Preserve reproducible native
values and freshness evidence for that exact revision.
The [local Grafana guide](../docs/guides/grafana.md) provides the repository's executable
qualification path, including native values and success-to-failure transitions.
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

Implemented: strict per-stream/aggregate-per-direction intent, uniform floor
allocation with remainder, explicit simultaneous-direction estimates, exact SI
unit parsing, sequential admission budgets, and layered capability reports.
See [the configuration guide](../docs/guides/configuration-intent.md) for the public
contract and concrete native-option qualification decisions. Named profiles
remain application-owned; built-in opinionated defaults and asymmetric
simultaneous budgets are deferred.

Requested settings and verified effective settings remain separate, with
unavailable verification explicit. Capability reports distinguish wrapper
support, native symbol availability, tested platform/version constraints, and
supplied runtime outcomes.

The [native option review #53](https://github.com/dariuszpanas/iperf3-lib/issues/53)
extends coverage to binding, transport/pacing, count termination, payload,
authentication and server controls. The [coverage reference](../docs/reference/native-options.md)
accounts for all tagged flags and keeps native/platform limits explicit.
Every accepted configuration field must be applied exactly once
or rejected explicitly, with native behavior checked through returned JSON
or a matching getter.

## 5. Analysis and diagnostic evidence

Tracking: [analysis implementation #31](https://github.com/dariuszpanas/iperf3-lib/issues/31).

Implemented in the development `iperf3_lib.analysis` module. The
[analysis guide](../docs/guides/analysis.md) documents formulas, selectors, evidence
quality, comparison policies, and diagnostic limits. Independent functions
provide:

- **Throughput stability:** minimum/median interval throughput, variability,
  and measured-time fraction below a chosen threshold. Exclude omitted warm-up intervals,
  account for unequal duration, and derive overall throughput from bytes
  and elapsed time.
- **Stream balance and scaling:** compare per-stream measurements and find
  the smallest tested stream count reaching a configurable fraction of the
  best observed throughput.
- **Directional asymmetry:** compare recorded methodologies while keeping
  sequential forward/reverse tests distinct from simultaneous bidirectional
  tests.
- **Diagnostics:** retain available TCP RTT/window and endpoint CPU evidence
  with units and provenance, without asserting a physical root cause.

An interval-average percentile is not a packet-performance percentile.
Retransmissions are not an exact packet-loss percentage, jitter is not
application latency, and a small collection of throughput observations does
not establish the physical bottleneck.

## 6. Repeated trials, baselines, and CI assessments

Tracking: [trial plans and assessment reports #32](https://github.com/dariuszpanas/iperf3-lib/issues/32).

Implemented [trial plans](../docs/guides/trials.md) record repetitions, separate warm-up
runs, pauses, and finite admission budgets. Every trial remains visible, including
failures, incomplete output, wrapper exceptions, and unstarted work. Execution
is sequential with no hidden retries; admission estimates cannot bound or
cancel an already running native call.


Execution success and performance acceptance are separate outcomes. The
assessment uses median summary bytes/time throughput, explicit minimum valid
counts, and absolute or relative baseline thresholds. Insufficient or incompatible
evidence is inconclusive. Baseline comparison checks actual verified settings;
a deliberate difference requires a retained reason.

Report v1 retains the plan, each artifact or exception, baseline artifacts,
comparison fingerprints, exclusions, criteria, and the fixed assessment algorithm.
Strict JSON import validates v1 evidence without invoking newer analysis rules.
Text, JUnit, and a pure CI classifier preserve execution failure, performance
rejection, and inconclusive outcomes. Full cell reports for bounded experiments
build on this shared execution and serialization layer.

## 7. Bounded experiments

### Parameter sweeps

Tracking: [bounded parameter sweeps #33](https://github.com/dariuszpanas/iperf3-lib/issues/33).

Implemented for the development release: [bounded parameter sweeps](../docs/guides/sweeps.md)
validate every finite configuration before traffic, require an active-time
admission budget, and retain declared or seeded cell order. They reuse the
sequential trial engine and preserve every failure and unstarted record.
Receiver summaries remain separate by method and direction. Actual axis and
allocation evidence qualifies cell medians; explicit policies govern cross-cell
comparison. Sweep-v1 reports retain all artifacts and verify frozen selection and
arithmetic without choosing a winner. Budgets estimate admitted active time and
payload; between-run elapsed limits stop admission. They cannot interrupt a
blocking native call or cap actual wire traffic.

### Adaptive UDP exploration

Tracking: [adaptive UDP exploration #34](https://github.com/dariuszpanas/iperf3-lib/issues/34).

Explore a bounded offered-load sweep with repeated trials around promising
rates. Do not assume observations are perfectly monotonic. Report requested
load, achieved sender/receiver rates, loss, and variation as a **tested
operating range**, not the network's exact physical capacity.

The unreleased implementation follows 0.3.0 and has a separate pure planner,
sequential batch runner and standalone adaptive-UDP report. The
[adaptive UDP design](design/advanced-execution.md#adaptive-udp-exploration)
specifies admission, non-monotonic selection, under-driven senders, and
inconclusive outcomes. Its [guide](../docs/guides/adaptive-udp.md) documents
mandatory confirmation, complete failure retention, observed cooldowns and
explicit budgets. Controlled impaired-link evidence is required alongside the
supported native and installed-distribution matrix before issue34 closes.

Both ideas build on trial plans and configuration intent. Run plans
sequentially under the current execution contract.

## 8. Live events and isolated execution

Tracking: [typed live events #35](https://github.com/dariuszpanas/iperf3-lib/issues/35)
and [isolated execution #36](https://github.com/dariuszpanas/iperf3-lib/issues/36).

The first public callback API and isolated Python/CFFI worker are part of
[#53](https://github.com/dariuszpanas/iperf3-lib/issues/53). Typed native events
are delivered outside the C callback, with bounded queues and dropped-event
counts. Result evidence is retained independently of delivery loss, with explicit
event reconstruction on native 3.19.1 and full native output on 3.21. Explicit timeouts
terminate and reap the worker and raise `TimeoutError`; they do not manufacture
a final native summary or claim native finalizer execution.

Basic direct calls remain non-reentrant. Async cancellation owns worker cleanup;
sequential and concurrent plans retain completed and partial histories. PR68
completed issue36's sequential crash/transport evidence qualification. A watchdog
stops the child independently of synchronous callbacks, but a blocked callback
still delays return to the caller.
Server stop remains cooperative between tests, with a fresh native test per iteration.

The
[execution design](design/advanced-execution.md#typed-live-events) and
[native observation report](design/native-event-observations.md) explain the
minimum/latest streaming difference and broader qualification criteria. Keep
#35/#36 open until their actual criteria have evidence; the historical small
loopback probe alone does not qualify production event delivery or cleanup.

## Working principles

Keep the runtime core small. Analysis and exporters consume the shared
result model independently; use standard-library JSON, statistics, CSV, XML,
and process facilities where sufficient. These proposals do not require
Pydantic or a web framework.

The dependency order is result correctness and artifacts, metrics, analysis,
trial plans and explicit sweeps, then separately qualified advanced execution.
Built-in opinionated profiles, asymmetric simultaneous rate budgets, unqualified
native setters, an HTTP metrics daemon, and cumulative counters remain outside
the 0.3.0 implementation claims. The configuration guide and advanced designs
record their boundaries and follow-up rationale.

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
| Configuration intent and capabilities | [#30](https://github.com/dariuszpanas/iperf3-lib/issues/30) | #28 for retained intent provenance. |
| Analysis and diagnostics | [#31](https://github.com/dariuszpanas/iperf3-lib/issues/31) | #28 |
| Repeated trials, baselines, and CI reports | [#32](https://github.com/dariuszpanas/iperf3-lib/issues/32) | #28, #30, #31 |
| Parameter sweeps | [#33](https://github.com/dariuszpanas/iperf3-lib/issues/33) | #30, #32 |
| Adaptive UDP exploration | [#34](https://github.com/dariuszpanas/iperf3-lib/issues/34) | #33 |
| Live events | [#35](https://github.com/dariuszpanas/iperf3-lib/issues/35) | #27 |
| Isolated execution | [#36](https://github.com/dariuszpanas/iperf3-lib/issues/36) | #28 |
| Native controls and discoverability | [#53](https://github.com/dariuszpanas/iperf3-lib/issues/53) | Typed configuration, worker lifecycle, client/server native qualification. |

Dependencies describe implementation order. Design discussions can proceed
in parallel, and issue status remains authoritative as plans evolve.
