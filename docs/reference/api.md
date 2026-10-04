# Python API reference

The site follows `main`; use documentation matching your installed version.
Async worker cancellation and `IperfCleanupError` are unreleased additions after
0.3.0. See [installation](../getting-started.md)
for package setup.

## Clients and servers

| API | Return value | Behavior |
| --- | --- | --- |
| `Client(cfg: ClientConfig, *, rate_intent=None, password=None)` | `Client` | Retains configuration and optional `RateIntent`; admission snapshots and resolves them. Password is separate from retained configuration. |
| `Client.run(*, timeout=None, on_event=None)` | `Result` | Basic calls use direct CFFI; expanded controls, MPTCP, streaming, timeout or callback select an isolated Python/CFFI worker. |
| `await Client.arun(*, timeout=None, on_event=None)` | `Result` | Always uses an isolated worker; cancellation waits for worker cleanup before propagating `CancelledError`. |
| `Server(port=5201, bind_host=None)` or `Server(config=ServerConfig(...))` | `Server` | Legacy address/port arguments or detached typed configuration; do not combine the two forms. |
| `Server.run_once(*, timeout=None, on_event=None)` | `Result` | Returns one server result from an isolated worker. |
| `await Server.aserve_once(*, timeout=None, on_event=None)` | `Result` | Runs one isolated server operation with cancellation cleanup. |
| `Server.serve_forever(*, on_result=None, on_event=None, max_runs=None, timeout=None)` | `None` | Sequential results delivered after freeing each native test; timeout covers the complete serving session. |
| `Server.stop()` | `None` | Signals the serving loop to stop between iterations. |

Import `Client`, `ClientConfig`, `Protocol`, `Server`, and `ServerConfig` from
`iperf3_lib`. `ServerConfig` is also available from `iperf3_lib.server_config`.
Read [running tests](../guides/running-tests.md) for error handling, async
cancellation, and process-isolation limits.

`timeout` is a positive finite number of seconds, or `None`. It includes worker
startup and terminates/reaps the process before raising `TimeoutError`; forced
process termination does not promise native finalizer execution. An independent
watchdog stops the child even during a blocked callback; returning control to
the caller still waits for the callback to return. Basic direct
calls remain non-reentrant within the calling process.

Cancellation and deadline expiry share one process owner. Shutdown sends TERM,
waits up to two seconds, then escalates to KILL within a total four-second
process cleanup budget. This budget does not interrupt OS process creation or
an already running Python callback. No further callbacks or server iterations
are admitted after cancellation wins. Repeated task cancellation cannot abandon
cleanup. Completion requires the final protocol response and successful process
reaping; a late success cannot override a cancellation or deadline decision.
After confirmed cleanup, task cancellation delivered before the coroutine
finishes propagates `CancelledError` even if native completion or a deadline
won just before delivery. A cleanup failure retains the original worker
termination cause.

If cleanup cannot be confirmed, `IperfCleanupError` takes precedence over the
original failure and retains it as its cause. The library retains ownership for
subsequent reaping, and the server remains unavailable until its worker is
reaped. Forced termination returns no partial `Result` and does not establish
that native C finalizers executed. Linux workers install a parent-death signal
before native execution; see [worker lifetime](../guides/running-tests.md#worker-lifetime-on-linux)
for the bootstrap, creating-thread and platform boundaries.

Async methods use the built-in isolated path, without calling overrides of
`run()` or `run_once()`. Worker setup errors, including `IperfError`, propagate;
completed native failures remain failed results. Basic synchronous clients keep
their direct setup-error behavior. Wrapping a synchronous call in an
application-owned executor does not confer cancellation support.

A `Server` rejects reentrant operations on the same instance. `stop()` is
cooperative between tests; it does not interrupt an active listener. A stopped
serving loop starts no new worker, but `run_once()` remains available. Native
failed attempts are retained as results and passed to `on_result`; worker setup
errors raise. Without `on_result`, a native failed result raises `IperfError`
from `serve_forever()` instead of silently discarding the failure. An idle exit
without JSON produces an incomplete result and ends the serving loop. Each
iteration allocates, defaults, configures and frees a fresh native test in the
same worker process; results cannot inherit a prior iteration's JSON.

### Native events and worker evidence

Import `NativeEvent` from `iperf3_lib.events`. It is a frozen dataclass with
`kind: str`, detached `data`, `sequence: int`, and `received_at_seconds: float`.
The receipt timestamp is distinct from native measurement boundaries.

Supplying `on_event` enables streaming. Callbacks run in the calling Python
thread (the executor thread for async methods), never within a native C callback.
Both event queues are bounded at 256. Sequence gaps and the result extension
`iperf3_lib.event_delivery` record `emitted`, `dropped`, and `queue_capacity`.
Result capture is independent of dropped live delivery. Native 3.21 streaming
enables full final output. On 3.19.1, `raw` is explicitly labelled
`reconstructed_events`, with original event envelopes retained separately;
reconstruction does not claim fields absent from those native events.
The reconstruction marker is
`extensions["iperf3_lib.native_json"]["representation"]`; its `events` list
retains the copied envelopes. `execution.reconstructed_json` is the diagnostic
code. Full native-document capture has no reconstruction extension. Retained
result/event evidence is not subject to the live-delivery queue capacity.
After a callback raises, further callback delivery stops; the active native run
finishes and the error is raised after worker shutdown.

`iperf3_lib.native_configuration` contains available `{field: {getter, value}}`
receipts. These prove native stored requests, not kernel-applied settings or
achieved measurements. See [native-control examples](../guides/native-controls.md)
and the [complete option inventory](native-options.md).

Server requests are retained separately in `iperf3_lib.server_config`; native
getter receipts do not turn those requests into proof of kernel behavior.

## Rate intent and capability reports

Import rate APIs from `iperf3_lib.intent` and capability APIs from
`iperf3_lib.capabilities`. All configuration integers remain strict; parsing
text is an explicit separate operation. See [rate intent and capabilities](../guides/configuration-intent.md)
for allocation, unit grammar, admission limits, profiles, and native-option decisions.

| API | Return value | Contract |
| --- | --- | --- |
| `RateIntent(per_stream_bps=None, aggregate_bps_per_direction=None)` | `RateIntent` | Exactly one strict integer intent; frozen dataclass. |
| `resolve_rate(config, intent=None)` | `ResolvedRate` | Uniform floor allocation per direction; preserves unused remainder and unlimited unknowns. Does not mutate config. |
| `parse_rate(text)` | `int` | Exact decimal SI bits/s or bytes/s conversion with explicit units. |
| `estimate_plan(configs, intents=None, *, max_payload_bytes=None, max_active_seconds=None)` | `PlanEstimate` | Finite sequential admission estimates including warm-up and both directions; raises on exceeded/unknown bounded costs. |
| `get_capabilities(*, probe_native=True, result=None)` | `CapabilityReport` | Separate wrapper/ABI/native/qualification evidence and optional supplied execution outcome. Offline mode never loads native code. |

`ResolvedRate` fields are `native_per_stream_bps`,
`aggregate_bps_per_direction`, `aggregate_bps_all_directions`,
`unused_bps_per_direction`, `active_directions`, and `source`. `to_dict()`
returns detached JSON-safe metadata. `PlanEstimate` contains a tuple of `runs`,
total `active_seconds`, `estimated_payload_bits`, and
`estimated_payload_bytes`; each `RunEstimate` has `rate`, `active_seconds`,
and `estimated_payload_bits`. Neither type reports observed traffic.

`CapabilityReport` contains `library`, `features`, `execution`, current host
platform/Python, and explicit tested platform/Python/native-version tuples.
The nested frozen dataclasses are `LibraryCapability`, `SymbolCapability`,
`FeatureCapability`, and `ExecutionEvidence`. Use `dataclasses.asdict` when an
application needs a serializable snapshot; this is not a versioned result artifact.

## Result dataclasses

Import `Result`, `FlowStats`, `SumStats`, `IntervalStats`, and `Diagnostic`
from `iperf3_lib`. `EndStats` and `StreamStats` are available from
`iperf3_lib.result`.
These are ordinary dataclasses; manually constructing a result does not
perform native-JSON validation.

### Result

| Attribute | Meaning |
| --- | --- |
| `ok: bool` | Client execution status. Required when constructing a result. |
| `error: str \| None` | Failure message, when available. |
| `raw: dict[str, Any]` | Parsed native JSON, an explicitly labelled streaming-event reconstruction on 3.19.1, or an empty dictionary when no JSON was returned. |
| `end: EndStats \| None` | Compatibility view of primary native end summaries. |
| `protocol: str \| None` | Native protocol string normalized to lowercase. |
| `bidirectional: bool` | Native simultaneous-bidirectional flag. |
| `reporting_role: str \| None` | Reporting endpoint (`"client"` or `"server"`) when established. |
| `flows: list[FlowStats]` | Directional flows with sender/receiver observations. |
| `intervals: list[IntervalStats]` | Aggregate and per-stream interval observations. |
| `streams: list[StreamStats]` | Terminal per-stream observations, including mixed UDP summaries marked unattributed. |
| `cpu: list[EndpointCpuEvidence]` | Qualified process CPU observations with endpoint/locality evidence. |
| `execution: ExecutionMetadata \| None` | Status, methodology, timing, configuration evidence, and producer environment. |
| `availability: dict[str, FieldAvailability]` | Explicit uncertainty or absence keyed by normalized JSON pointer. |
| `extensions: dict[str, JSONValue]` | Namespaced application metadata preserved by artifacts. |
| `diagnostics: list[Diagnostic]` | Missing-data, incomplete-output, native-error, and ambiguous-mapping diagnostics. |
| `started_at_seconds: float \| None` | Native start Unix timestamp. |
| `duration_seconds: float \| None` | Duration from native test configuration. |
| `completed_at_seconds: float \| None` | Observed completion Unix timestamp; unknown for directly parsed native JSON. |

`to_dict()` returns a recursive dataclass dictionary. `summary_mbps` is a
read-only convenience property in decimal megabits per second. Consult the
[results guide](../guides/results.md) for their interpretation and limits.

### FlowStats and SumStats

`FlowStats(direction, sender=None, receiver=None)` identifies a traffic
direction and optional `SumStats` objects for each observation point.

`SumStats` has these attributes:

| Attribute | Default | Units |
| --- | --- | --- |
| `bits_per_second` | `None` | Optional bits/s; a measured zero remains zero. |
| `retransmits` | `None` | Native retransmission count. |
| `lost_percent` | `None` | Percentage; `1.0` means one percent. |
| `jitter_ms` | `None` | Milliseconds. |
| `direction` | `None` | Direction metadata, consistent with the parent flow. |
| `observation` | `None` | `"sender"` or `"receiver"` metadata. |
| `bytes` | `None` | Native measured byte count. |
| `duration_seconds` | `None` | Measured summary duration in seconds. |
| `start_seconds`, `end_seconds` | `None` | Native elapsed boundaries in seconds. |
| `packets`, `lost_packets` | `None` | Native packet counts. |
| `omitted` | `None` | Whether native output marks this observation as warm-up. |
| `tcp` | `None` | `TcpSummaryEvidence` for attributable TCP stream sender observations. |

`EndStats(sum_sent=None, sum_received=None)` holds two optional `SumStats`
objects. Both are observations of the primary flow, not separate traffic
directions.

### IntervalStats and Diagnostic

`IntervalStats(start_seconds, end_seconds, bits_per_second=None,
direction="unknown", observation=None, stream_id=None)` uses
optional elapsed interval boundaries in seconds and an optional bitrate in bits/s.
`stream_id` is the native socket identifier when available.
Additional fields are `scope`, `bytes`, `duration_seconds`, `omitted`,
`packets`, `lost_packets`, `retransmits`, `lost_percent`, `jitter_ms`, and
`tcp: TcpIntervalEvidence | None`.
Scope comes from the native container; all optional measurements retain `None`
when absent. `StreamStats` groups terminal `sender`, `receiver`, or `unattributed`
summaries by direction and optional stream identifier.

`Diagnostic(message, severity="info", code="unspecified", path=None,
evidence_paths=[])` stores a message, severity (`"info"`, `"warning"`, or
`"error"`), stable machine-readable code, and JSON pointer evidence.

### Execution provenance

Import these dataclasses from `iperf3_lib.result`:

- `ExecutionMetadata`: `status` (`completed`, `failed`, or `incomplete`),
  `method`, `timing`, `configuration`, native version/system information,
  Python version, and platform.
- `RunTiming`: observed UTC start/completion, monotonic elapsed time, native
  start, requested duration, and separately named estimated completion.
- `ConfigurationSnapshot`: optional `requested` values and `effective` settings.
- `VerifiedSetting`: value, verification state, and native evidence paths.
- `FieldAvailability`: absent, unsupported, malformed, or unknown state and
  evidence paths. Absence alone does not establish unsupported behavior.

See [portable artifacts](../guides/artifacts.md) for field interpretation and
strict interchange validation. Direct dataclass construction remains permissive;
the artifact encoder validates constructed and mutated instances.

## Versioned result artifacts

Import from `iperf3_lib.artifacts`:

```text
artifact_from_result(result) -> ResultArtifact
artifact_to_dict(artifact) -> dict
artifact_from_dict(value) -> ResultArtifact
dumps_artifact(artifact, *, indent=None) -> str
loads_artifact(text: str | bytes) -> ResultArtifact
artifact_from_legacy_dict(value) -> ResultArtifact
```

`ResultArtifact` contains schema version, kind, `ArtifactProducer`, normalized
result, and extension metadata. `ArtifactValidationError` is a `ValueError`;
`UnsupportedArtifactVersion` identifies unknown versions. Decoding does not run
a benchmark, load libiperf, or reinterpret `raw` using the current native parser.

### Native JSON normalization

`iperf3_lib.result.result_from_iperf_json(raw, *, reporting_role=None)` normalizes a native JSON
dictionary without running a benchmark. Invalid shapes or numeric values raise
`ValueError`. Native error documents and incomplete output with no numeric
end-of-test endpoint evidence return `ok=False`, preserving the raw data and
diagnostics. Valid partial measurements remain available; successful parsing
does not certify that every requested measurement was reported.

`reporting_role` accepts `"client"`, `"server"`, or `None`. `Client.run()`
provides `"client"`. Saved JSON can establish its role through native
`start.connecting_to` or `start.accepted_connection` markers; a generic
`start.connected` list alone does not establish a role. Contradictory evidence
produces an unknown role and a diagnostic. Bidirectional stream direction
requires both reporting role and local sender evidence; aggregate summary
keys remain relative to the client on either reporting endpoint.

Direction parsing recognizes native `start.test_start.bidir` and the earlier
`bidirectional` spelling. Both must agree when present together. Direction
flags accept booleans or the integers zero and one; malformed or conflicting
flags raise `ValueError`.

## Exporters

Import both functions from `iperf3_lib.exporters.prometheus`:

```text
render_text(result, labels=None, *, last_success_timestamp_seconds=None) -> str
write_textfile(path, result, labels=None, *, last_success_timestamp_seconds=None) -> None
```

`labels` is an optional mapping of strings to strings. `path` accepts a
string or path-like object. See [Prometheus snapshots](../guides/prometheus.md)
for metric units, freshness, label validation, and collector integration.
The [results guide](../guides/results.md) explains direction and missing-data
semantics.

## Exceptions and capabilities

`IperfError`, `IperfLibraryError`, and `UnsupportedFeatureError` are independent
`RuntimeError` subclasses exported from the package root. Catching
`IperfError` does not catch the other two.

`IperfCleanupError` subclasses `IperfLibraryError` and reports unconfirmed worker
cleanup. It is also exported from the package root. Successful task cancellation
raises `asyncio.CancelledError`; an asyncio timeout can translate that into
`TimeoutError` after cleanup. Cleanup errors are propagated instead of reporting
successful cancellation.

`iperf3_lib.capabilities.has_symbol(name)` reports whether the loaded CFFI
interface exposes a symbol, returning `False` on loading/detection failures.
Accessing `HAS_BIDIR`, `HAS_JSON_OUTPUT`, `HAS_JSON_CALLBACK`,
`HAS_PROTOCOL_SELECTION`, or `HAS_BIND_ADDRESS` performs a lazy symbol probe.
Legacy flags are narrow compatibility probes; they are not an exhaustive native
option inventory. Parser-backed worker controls do not require a dedicated
setter for every feature.

These flags do not prove operating system support or a successful native run.
Importing the module does not load libiperf. `get_capabilities(probe_native=False)`
provides an offline report; an explicit native probe distinguishes library and
symbol availability from wrapper support and qualification.



## Pure measurement analysis

Import from `iperf3_lib.analysis`:

```text
summary_throughput(result, *, direction, observation) -> ThroughputAnalysis
interval_stability(result, *, selection, threshold_bps=None,
                   quantiles=(0.5, 0.95), policy=IntervalPolicy()) -> StabilityAnalysis
stream_balance(result, *, direction, observation, source="summaries",
               policy=IntervalPolicy()) -> StreamBalanceAnalysis
check_compatibility(trials, *, policy) -> CompatibilityAnalysis
stream_scaling(trials, *, direction, observation, compatibility,
               best_fraction=0.95, minimum_valid_trials=1) -> ScalingAnalysis
simultaneous_asymmetry(result, *, observation) -> AsymmetryAnalysis
sequential_asymmetry(forward, reverse, *, observation,
                     methodology) -> AsymmetryAnalysis
```

Inputs include `Selection`, `IntervalPolicy`, `AnalysisTrial(trial_id, result)`,
`ComparisonPolicy`, and `SequentialMethodology`. Output dataclasses carry
`quality`, named units, `EvidenceRef(path, trial_id=None)`, and
`AnalysisDiagnostic(code, message, evidence=())`. `CompatibilityAnalysis` records
`compatible`, `policy`, concrete per-trial `fingerprints`, and diagnostics.
All are ordinary frozen dataclasses; nested mappings retain their usual mutability.
Invalid numbers, counts, option enums and comparison receipts raise `ValueError`.
Missing measurement evidence produces an explicit data-quality outcome.

Nondefault advanced requests and policy-named advanced fields activate
comparison dimensions for every trial. Each needs verified native evidence,
including peers using defaults; matching requests never supply that evidence.
See [advanced comparison rules](../guides/analysis.md#advanced-configuration-in-comparisons).

The [analysis guide](../guides/analysis.md) specifies formulas, default policies,
compatibility fields, coverage and methodology requirements.

### TCP and CPU evidence dataclasses

Import these from `iperf3_lib.result`:

- `TcpIntervalEvidence`: `smoothed_rtt_seconds`, `rtt_variation_seconds`,
  `send_congestion_window_bytes`, `advertised_send_window_bytes`, `path_mtu_bytes`.
- `TcpSummaryEvidence`: `minimum_sampled_rtt_seconds`,
  `maximum_sampled_rtt_seconds`, `native_mean_sampled_rtt_seconds`,
  `maximum_send_congestion_window_bytes`, `maximum_advertised_send_window_bytes`.
- `EndpointCpuEvidence(endpoint, locality)`: `total_percent`, `user_percent`,
  `system_percent`, and `scope="iperf_process"`. Endpoint is client/server/unknown;
  locality is local/remote. Percentages have no 100% ceiling.

All measurement fields default to `None`. Each object has `evidence_paths`, a
mapping from present measurement names to raw or namespaced receipt pointers.
Qualification, units and attribution limits are documented in the analysis guide.
## Repeated trials and assessments

Import from `iperf3_lib.trials`:

```text
TrialPolicy(repetitions=3, warmup_runs=0, pause_seconds=0, max_trials=1000, stop_on_error=False)
PlanBudget(max_active_seconds, max_payload_bytes, stop_after_elapsed_seconds=None)
TrialSpec(trial_id, cell_id, phase, repetition, config, rate_intent=None)
prepare_trials(config, *, budget, policy=TrialPolicy(), rate_intent=None, cell_id="default")
prepare_plan(trials, *, policy, budget, order_seed=None) -> PreparedPlan
run_plan(plan, *, executor=None) -> PlanResult
```

`TrialSpec.resolved_config` exposes a detached native configuration. `PreparedPlan`
retains declared order, policies, rate estimates and planned pauses. `TrialRecord`
retains completed/failed/incomplete artifacts, exceptions, unstarted reasons and
wrapper-observed timing. `PlanResult.execution_success` requires all planned runs
to complete. Budgets are admission estimates; elapsed stops cannot cancel a call.

Import from `iperf3_lib.assessments`:

```text
AssessmentPolicy(direction="client_to_server", observation="receiver",
    minimum_valid_trials=3, minimum_valid_baselines=1,
    minimum_throughput_bps=None, absolute_tolerance_bps=0, relative_tolerance=0)
assess_plan(execution, *, policy, comparison, baselines=None) -> AssessmentReport
ci_exit_code(report) -> int
```

`comparison` is the public analysis `ComparisonPolicy`. Assessment uses median
summary bytes/time throughput, preserving compatibility evidence, excluded trials
and retained baselines. Performance outcome is independent of execution success.

Import from `iperf3_lib.reports`:

```text
report_to_dict(report) -> dict
report_from_dict(mapping) -> AssessmentReport
dumps_report(report, *, indent=None) -> str
loads_report(text: str | bytes) -> AssessmentReport
plan_result_to_dict(execution) -> dict
plan_result_from_dict(mapping) -> PlanResult
compatibility_to_dict(comparison, *, artifacts) -> dict
compatibility_from_dict(mapping, *, artifacts) -> CompatibilityAnalysis
render_text(report) -> str
render_junit(report, *, inconclusive="failure") -> str
```

`ReportValidationError` identifies invalid canonical report fields with a JSON
pointer. `UnsupportedReportVersionError` identifies unknown schema/algorithm
versions. Report-v1 imports preserve archived analysis and validate frozen
`median-summary-v1` arithmetic. See [repeated trials](../guides/trials.md) for
baseline rules, finite budgets, report evolution, and CI status precedence.


## Owned async plans (unreleased)

Import `arun_plan` from `iperf3_lib.async_trials`:

```text
arun_plan(plan: PreparedPlan, *, timeout=None) -> PlanExecutionResult
```

Import `PlanExecutionResult`, `PlanTrialRecord`, `PlanExecutionReport`,
`PlanCancelledError`, `PlanTimeoutError`, and `PlanCleanupError` from
`iperf3_lib.plan_execution`. Each error exposes a detached `partial_result`;
cleanup errors also retain original worker owners in `cleanup_errors`.
The plan deadline is separate from the elapsed admission budget. Execution is
sequential with global completion-to-next-start pauses. Interrupted and exception records
carry bounded `partial_events`, `events_observed`, and `events_dropped`;
`cleanup_confirmed` and `execution_success` describe the full history.

Import from `iperf3_lib.plan_reports`:

```text
plan_report_from_execution(execution) -> PlanExecutionReport
plan_report_to_dict(report) -> dict
plan_report_from_dict(mapping) -> PlanExecutionReport
dumps_plan_report(report, *, indent=None) -> str
loads_plan_report(text: str | bytes) -> PlanExecutionReport
snapshot_plan_execution(execution) -> PlanExecutionResult
render_plan_text(report) -> str
render_plan_junit(report) -> str
```

These functions use standalone plan-execution schema 2, with
`execution_mode="sequential-owned"` and
`pause_semantics="global_completion_to_start"`. They share the report validation
exceptions but do not reinterpret assessment-v1 or sweep-v1 histories. See
[async plan evidence](../guides/trials.md#cancel-an-async-plan-and-retain-its-evidence)
for cancellation, retention limits and compatibility boundaries. Existing v2
archives remain readable; new exception records carrying event evidence require
the updated v2 reader.

## Bounded concurrent plans (unreleased)

Import `arun_concurrent_plan` from `iperf3_lib.concurrent_trials`:

```text
arun_concurrent_plan(plan: PreparedPlan, *, policy: ConcurrentExecutionPolicy,
                    resources: Mapping[str, tuple[str, ...]] | None = None,
                    timeout=None) -> ConcurrentPlanResult
```

`iperf3_lib.concurrent_execution` supplies `ConcurrentExecutionPolicy(max_workers,
max_active_target_bps)`, `ConcurrentTrialRecord`, `ConcurrentPlanResult`,
`ConcurrentPlanReport`, and `ConcurrentPlanCancelledError`,
`ConcurrentPlanTimeoutError`, `ConcurrentPlanCleanupError`. The exceptions
retain `partial_result`; cleanup failures additionally retain `cleanup_errors`.

Import `concurrent_report_from_execution`, `concurrent_report_to_dict`,
`concurrent_report_from_dict`, `dumps_concurrent_report`, `loads_concurrent_report`,
`render_concurrent_text`, and `render_concurrent_junit` from
`iperf3_lib.concurrent_reports`. These use standalone plan-execution schema 3.

See [bounded concurrent plans](../guides/concurrent-plans.md) for rate accounting,
resource keys, per-cell dependencies, stop behavior and reservation offsets.
This opt-in mode requires zero pauses and rejects fixed client ports. Existing
sequential APIs and assessment/sweep report contracts remain unchanged.

## Parameter sweeps

Import from `iperf3_lib.sweeps`:

```text
SweepAxis(name, values: tuple)
prepare_sweep(base_config, axes, *, policy, budget, rate_intent=None,
              order="declared", seed=None) -> PreparedSweep
run_sweep(prepared, *, executor=None, minimum_valid_trials=1,
          comparison_policy=None) -> SweepResult
summarize_sweep(prepared, execution, *, minimum_valid_trials=1,
                comparison_policy=None) -> SweepResult
```

`PreparedSweep` retains base settings, axes, ordered `SweepCell` objects, rate
intent and the shared `PreparedPlan`. `SweepResult` retains this preparation,
`PlanResult`, per-cell summaries, minimum sample count and optional method/direction
comparison groups. `SweepSample` retains bytes/time, measured rate, evidence path,
`eligible_for_cell` and `SettingCheck` values. A setting check records expected and
observed values, receipt paths, and matched/native_default/mismatch/unknown state.

Import from `iperf3_lib.sweep_reports`:

```text
report_from_sweep(result) -> SweepReport
sweep_report_to_dict(report) -> dict
sweep_report_from_dict(mapping) -> SweepReport
dumps_sweep_report(report, *, indent=None) -> str
loads_sweep_report(text: str | bytes) -> SweepReport
```

The strict sweep-v1 envelope embeds common plan-result and compatibility payloads.
It uses the shared `ReportValidationError` and `UnsupportedReportVersionError`.
See [bounded sweeps](../guides/sweeps.md) for admission, ordering, qualification,
method separation and the frozen report contract.

## Adaptive UDP experiments (unreleased)

Import from `iperf3_lib.adaptive`:

```text
AdaptiveUDPPolicy(min_rate_bps, max_rate_bps, max_distinct_rates,
                  max_refinement_depth, receiver_loss_percent,
                  minimum_valid_trials, minimum_sender_fraction,
                  confirmation_batches=1)
prepare_adaptive_udp(config, initial_rates, *, policy, budget,
                     adaptive_policy) -> PreparedAdaptiveUDP
next_adaptive_batch(prepared, history=()) -> AdaptiveDecision
summarize_adaptive_udp(prepared, history) -> AdaptiveResult
```

`AdaptiveBatch` retains a finite `PreparedPlan`, rate selection, depth and reason.
`AdaptiveBatchResult` pairs it with a complete `PlanResult` and observed preceding
cooldown. `AdaptiveObservation` keeps requested/native allocation, independent
sender and receiver measurements, both loss values, setting checks and exclusion
reasons. `AdaptiveRateSummary` preserves all observations and confirmation state.

Import `run_adaptive_udp(prepared, *, executor=None)` from
`iperf3_lib.adaptive_execution` for sequential execution. The pure planner and
runner share whole-experiment admission limits. Initial rate endpoints, explicit
UDP block size, finite duration and finite active/payload budgets are required.
`PlanBudget.stop_after_elapsed_seconds` is unsupported in this initial API.

Import from `iperf3_lib.adaptive_reports`:

```text
report_from_adaptive_udp(result) -> AdaptiveUDPReport
adaptive_udp_report_to_dict(report) -> dict
adaptive_udp_report_from_dict(mapping) -> AdaptiveUDPReport
dumps_adaptive_udp_report(report, *, indent=None) -> str
loads_adaptive_udp_report(text: str | bytes) -> AdaptiveUDPReport
render_adaptive_udp_text(report) -> str
```

The standalone schema-1 report uses kind `iperf3-lib.adaptive-udp` and algorithm
revision `conservative-tested-rates-v1`. See the [adaptive guide](../guides/adaptive-udp.md)
for confirmation, non-monotonic observations, inconclusive outcomes and finite
experiment limits. Its highest eligible rate is a tested point, not a physical
capacity estimate.
