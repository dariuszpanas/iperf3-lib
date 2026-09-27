# Repeated trials and assessments

These APIs are available in 0.3.0. See [installation](../getting-started.md)
for package setup.

A trial plan runs a finite, declared sequence. It keeps warm-up, failed,
incomplete, and unstarted runs so the report describes the entire experiment.
Execution is synchronous and sequential; each trial calls `Client.run()` once.
There are no hidden retries.

## Admit a plan before generating traffic

```python
from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import RateIntent
from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials, run_plan

plan = prepare_trials(
    ClientConfig("127.0.0.1", duration=2, parallel=2),
    rate_intent=RateIntent(aggregate_bps_per_direction=2_000_000),
    policy=TrialPolicy(repetitions=3, warmup_runs=1, pause_seconds=0.5),
    budget=PlanBudget(max_active_seconds=8, max_payload_bytes=2_000_000),
)
print(plan.estimate)
print(plan.trials[0].config)           # detached caller configuration
print(plan.trials[0].rate_intent)      # retained original rate intent
print(plan.trials[0].resolved_config) # detached native-level configuration
execution = run_plan(plan)
```

The four runs request eight active seconds and two million payload bytes in
this example. The three pauses request another 1.5 seconds. Estimates include
all streams, active directions, warm-up runs, and each config's `omit` period.
Warm-up **runs** and a native `omit` period are different settings: warm-up runs
remain complete artifacts but are excluded from assessment.

`max_trials` defaults to 1,000 and is checked before expansion. Admission
validates every config, resolves rate intent, and detaches the full plan before
the first native call. Mutating a caller config or an executor's copy cannot
change later admitted trials. Execution revalidates nested configs because
`ClientConfig` remains mutable.

Each estimate cap accepts explicit `None` to leave that estimate uncapped.
An unlimited or unknown rate cannot satisfy a finite payload cap. These are
**admission estimates**, not hard traffic or wall-clock limits: native traffic
can differ from the target, and payload estimates exclude network overhead.
`PlanBudget(stop_after_elapsed_seconds=...)` stops admitting new runs between
calls, including after a pause. It cannot interrupt a blocking native call;
actual elapsed time can exceed the limit. The recorded monotonic elapsed time,
observed pauses, and endpoint byte measurements are separate evidence.

The expanded client controls preserve count-based runs with unknown time and
payload estimates. Such trials require uncapped estimate budgets; an unlimited
`duration=0` is rejected by finite plans. New transport settings remain part of
the retained request and must be considered when comparing measurements.

The existing [non-reentrant execution contract](running-tests.md) applies.
The runner adds no concurrent execution or cancellation API; individual client
configurations select the direct or isolated-worker path described there.
`stop_on_error=True` stops after failed, incomplete, or exceptional execution;
remaining trials are retained as `not_run`. Process-control exceptions such
as `KeyboardInterrupt` propagate.

## Assess measurements independently of execution

```python
from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.assessments import AssessmentPolicy, assess_plan, ci_exit_code
from iperf3_lib.reports import dumps_report, render_junit, render_text

report = assess_plan(
    execution,
    policy=AssessmentPolicy(
        direction="client_to_server",
        observation="receiver",
        minimum_valid_trials=3,
        minimum_throughput_bps=1_500_000,
    ),
    comparison=ComparisonPolicy(
        group_id="local-throughput",
        endpoint_pair=("benchmark-client", "benchmark-server"),
    ),
)
print(render_text(report))
print(ci_exit_code(report))
json_report = dumps_report(report, indent=2)
junit_xml = render_junit(report)
```

Each valid sample uses **summary bytes × 8 / measured summary seconds**, through
the public analysis API. The default observer is receiver. Native reported
bitrate is not a substitute for absent bytes or duration. The median of valid
trial rates is used, including actual zero rates; even populations average the
two central values. Warm-up, unsuccessful execution, and unavailable summary
measurements are excluded with recorded reasons. Too few valid trials produce
`inconclusive`, not a passing zero-valued result.

Execution success requires every planned run, including warm-up, to complete.
Performance can pass with enough valid measurements while execution remains
unsuccessful. The report preserves both conclusions and all underlying results.
A wrapper exception has admitted settings and observed timing but no fabricated
native artifact. If a returned `Result` cannot be serialized, JSON-safe fields
are retained separately with the serialization exception and omission diagnostics.

Compatibility uses actual verified protocol, method, native version/platform,
endpoint, duration, omit, stream count, rate, block size, TOS, and rate intent.
Group names alone do not prove compatibility. Unknown settings make assessment
inconclusive. An intentional difference needs a recorded
`ComparisonPolicy.allowed_differences` reason; that reason does not make unknown
evidence known. Keep comparison policies narrow for regression assessments.

[Advanced settings](analysis.md#advanced-configuration-in-comparisons) also
participate when any retained request uses a nondefault value or the policy
names the field. All compared candidates and baselines need verified native
observations for that setting, including peers using defaults. Matching
requests or a reasoned allowance cannot replace missing receipts; the
assessment remains inconclusive when compatibility cannot be established.

## Compare with retained baselines

Pass `baselines=[artifact, ...]` to `assess_plan`. Each baseline is a
[versioned result artifact](artifacts.md), and the report retains a detached copy.
Candidates and baselines undergo the same summary selection and compatibility
checks. The default minimum is one valid baseline; set
`minimum_valid_baselines` for a larger reference population.

With `relative_tolerance=0.05`, the baseline criterion permits a 5% reduction
from its median. `absolute_tolerance_bps` is an additional numerical allowance.
When both an absolute minimum and a baseline are supplied, the v1 rule is:

```text
required_bps = max(0,
    max(minimum_throughput_bps, baseline_median_bps * (1 - relative_tolerance))
    - absolute_tolerance_bps)
pass when candidate_median_bps >= required_bps
```

An omitted absolute minimum is left out of that maximum. An empty supplied
baseline remains an insufficient baseline; it is not treated as no baseline.
Relative tolerance without a baseline is inconclusive. With neither a minimum
nor a baseline, there is no acceptance criterion and the result is inconclusive.
A zero baseline is valid for threshold arithmetic, but percentage change is
undefined: `relative_change` remains `None` with an explicit reason.

Trial identities use `trial:<caller-id>` and baseline identities use
`baseline:<index>`, avoiding collisions while preserving caller IDs in the plan.

## Store and load report v1

`dumps_report` / `loads_report` and `report_to_dict` / `report_from_dict` operate
on `kind="iperf3-lib.assessment", schema_version=1`. Reports contain the whole
plan, original and resolved configs, budgets, order, execution outcomes,
artifacts, baseline artifacts, analysis evidence, policies, exclusions, and CI
classification. Unknown canonical fields, versions, duplicate JSON keys,
nonfinite numbers, and inconsistent derived values are rejected.

Schema 1 was first published in 0.3.0 and binds assessment arithmetic to
`algorithm_revision="median-summary-v1"`. Changes to its interpretation require
a new schema version.
Loading validates that frozen selection, arithmetic, thresholds, and internal
references agree with retained evidence. It verifies recorded comparison
fingerprints against canonical settings and receipt presence, and checks the
recorded difference policy with frozen v1 compatibility rules, including any
activated advanced settings. Existing default-only fingerprints remain valid.
It never invokes the newest analysis implementation.
It neither loads libiperf nor reruns an experiment. An archived report is evidence
of the recorded decision, not a fresh compatibility check or an authenticity
signature. Reassess retained artifacts explicitly when adopting new analysis rules.

`plan_result_to_dict` / `plan_result_from_dict` expose the same strict nested
execution payload for other versioned reports. The containing report owns the
schema envelope. `compatibility_to_dict` / `compatibility_from_dict` share the
frozen v1 comparison codec with an explicit trial-ID-to-artifact mapping. The
plan codecs preserve the full declared trial order and every artifact;
they do not provide another native JSON parser.

## CI and JUnit policy

`ci_exit_code(report)` is pure and never exits the process. Precedence is:

| Code | Condition |
| --- | --- |
| 2 | Any native failure or wrapper exception, including warm-up. |
| 1 | Otherwise, a known performance rejection. |
| 3 | Otherwise, incomplete/unstarted execution or inconclusive assessment. |
| 0 | Every run completed and performance passed. |

JUnit emits one case per admitted trial plus a separate performance case.
Execution failures are errors; performance rejection is a failure. Incomplete
or inconclusive cases fail by default. `render_junit(report, inconclusive="skipped")`
opts into skipped JUnit cases without changing the JSON report or CI classifier.
Text is a concise summary; JSON retains full evidence.

## Application-owned orchestration

`prepare_plan(sequence_of_TrialSpec, policy=..., budget=...)` admits an explicit
finite order for application workflows. Each spec stores its cell ID, phase,
repetition, config, and optional intent. Every cell must have the declared
number of warm-up and measured runs, using zero-based repetition indexes in
each phase. A cell's warm-ups must precede its measured runs; cross-cell order
remains explicit. `run_plan(..., executor=callable)` can
use an application-owned transport or deterministic executor returning a `Result`.
The executor receives a detached spec. This interface shares admission and
failure handling with future finite sweep helpers, without interpreting a set
of independent runs as a capacity optimum.
