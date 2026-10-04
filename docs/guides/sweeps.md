# Bounded parameter sweeps

These APIs are available in 0.3.0. See [installation](../getting-started.md)
for package setup.

A sweep runs a finite Cartesian product through the [sequential trial runner](trials.md).
It records every cell, warm-up, measured run, failure and unstarted trial. Cell
summaries describe receiver measurements; they do not select a winner or estimate
an optimum network capacity.

For an unreleased experiment that selects and confirms offered UDP rates against
explicit receiver-loss criteria, see [adaptive UDP experiments](adaptive-udp.md).

## Admit the entire experiment

```python
from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import RateIntent
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy

prepared = prepare_sweep(
    ClientConfig("127.0.0.1", duration=1, blksize=4096),
    axes=(SweepAxis("parallel", (1, 2)),),
    rate_intent=RateIntent(aggregate_bps_per_direction=1_000_001),
    policy=TrialPolicy(repetitions=3, warmup_runs=1, pause_seconds=0.5),
    budget=PlanBudget(max_active_seconds=8, max_payload_bytes=1_000_001),
    order="randomized",
    seed=17,
)
print(prepared.plan.estimate)
print([cell.cell_id for cell in prepared.cells])
sweep = run_sweep(prepared, minimum_valid_trials=3)
```

The example admits eight one-second runs, including two warm-up runs. Allocation
is 1,000,001 bps for one stream and 500,000 bps per stream for two streams; the
second cell explicitly leaves one bps of the target unallocated. Seven pauses
request another 3.5 seconds. The estimate rounds total payload bytes upward.

Every axis needs a nonempty tuple of distinct values. The supported names are
`server`, `port`, `protocol`, `duration`, `parallel`, `omit`, `blksize`, `rate`,
`tos`, and `method`. Method values are `forward`, `reverse`, or `bidirectional`.
Protocol values are `tcp`, `udp`, or `sctp`; actual platform support still applies.
A `rate` axis cannot be combined with a base `RateIntent`.

Expanded native controls can be fixed in the base configuration; they do not
automatically become sweep axes. The finite active-time requirement excludes
count-terminated and unlimited-duration configurations with unknown time
estimates. Keep transport, payload and host controls consistent when defining
comparable cells.

The product size, repetitions and warm-ups must fit `TrialPolicy.max_trials`
(default 1,000) before expansion. Every combination must pass configuration and
rate validation before execution. Sweeps require a finite `max_active_seconds`.
Set `max_payload_bytes=None` explicitly to admit unknown or unlimited target
rates; those rates cannot satisfy a finite payload budget.

Budgets describe admitted requests, including native `omit` periods and both
traffic directions. They exclude network overhead and cannot enforce actual
traffic volume or cancel a blocking native call. `stop_after_elapsed_seconds`
stops admission between calls and clips intervening pauses; a running call may
finish after the limit. The runner uses one clock and pause policy across cells.

## Order and retained outcomes

Axis order and value order define stable IDs such as `cell-0000`. Randomization
shuffles whole cells, then runs each cell's warm-ups before its measured trials.
The seed and final order are retained; reading a report does not replay an RNG.
`order="declared"` uses declared Cartesian order and accepts no seed.

`PreparedSweep.cells` retains selected parameters, caller config, resolved native
config and trial IDs. All inputs are detached. `sweep.execution` is the common
`PlanResult`, retaining artifacts, exceptions, timings and unstarted reasons.
`stop_on_error=True` preserves the remaining trials as `not_run`; there are no
retries. These APIs preserve the existing non-reentrant native execution contract.

## Read observations and qualification separately

```python
for cell in sweep.cells:
    print(cell.cell_id, cell.execution_counts)
    for summary in cell.directions:
        print(summary.direction, summary.quality, summary.median_throughput_bps)
        for sample in summary.samples:
            print(sample.throughput_bps, sample.eligible_for_cell, sample.setting_checks)
```

Each sample uses `8 * receiver_bytes / measured_seconds`. Measured zero is a
valid sample. Warm-ups, unsuccessful execution, missing receiver bytes/time,
omitted summaries, and results with a different protocol or method have explicit
exclusions. Sender throughput is never substituted for missing receiver evidence.

A `SettingCheck` compares each declared axis to a verified native observation
with an actual raw or extension receipt. Rate intent also checks the resolved
native per-stream rate; aggregate intent verifies stream count. Missing receipts
produce `unknown`, and different values produce `mismatch`. A `None` axis value
requests a native default: `native_default` records its observed choice and
requires evidence. Caller settings and returned native settings remain distinct.

A successful run with the wrong or unknown setting retains its measured rate and
artifact, but its sample has `eligible_for_cell=False`. Such samples do not enter
the cell median or cross-cell comparison. The median uses only eligible measured
samples. Fewer than `minimum_valid_trials` produces `insufficient_data`; remaining
failures, exclusions or unqualified samples produce `partial`; otherwise quality
is `complete`. Execution success is independent of measurement qualification.

Forward, reverse and bidirectional cells remain separate methods. Bidirectional
cells have two separate receiver summaries. These populations are never combined.

## Compare cells only with an explicit policy

```python
from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.sweeps import summarize_sweep

compared = summarize_sweep(
    sweep.prepared,
    sweep.execution,
    minimum_valid_trials=3,
    comparison_policy=ComparisonPolicy("stream-count", ("client", "server")),
)
for group in compared.comparisons:
    print(group.method, group.direction, group.quality, group.reasons)
```

Declared axes become recorded varying fields. Other settings and producer
provenance must satisfy the [analysis comparison policy](analysis.md), with any
allowed difference justified by a retained reason. Varying fields still need
actual observations; a group label does not establish comparability. Caller
`varying_fields` must be a subset of declared axes. `allowed_differences` cannot
override failed cell qualification. A group needs at least two cells with enough
eligible samples; one cell does not become a cross-cell comparison merely because
it has multiple repetitions. No comparison is added when policy is omitted.

Advanced controls fixed in the base configuration also follow the
[advanced comparison evidence rule](analysis.md#advanced-configuration-in-comparisons).
They do not become sweep axes. When activated by a nondefault request or named
policy field, each compared sample needs a verified native observation, even
when all requested values match. A cell can have valid measurements and
qualified axes while its cross-cell comparison is incompatible because a fixed
advanced setting lacks returned-value or getter evidence.

## Save and validate a sweep report

```python
from pathlib import Path
from iperf3_lib.sweep_reports import (
    dumps_sweep_report, loads_sweep_report, report_from_sweep,
)

report = report_from_sweep(compared)
Path("sweep.json").write_text(dumps_sweep_report(report, indent=2), encoding="utf-8")
restored = loads_sweep_report(Path("sweep.json").read_text(encoding="utf-8"))
```

The `iperf3-lib.sweep` envelope has schema version 1 and algorithm revision
`receiver-cell-median-v1`. It embeds the shared plan-result and compatibility
payloads, including every constituent versioned result artifact. It preserves
producer identity, namespaced extensions, recorded order, allocations, setting
checks, exclusions and medians.

Import validates frozen v1 selection and arithmetic against retained artifacts.
It rejects unknown keys, duplicate JSON keys, nonfinite numbers, changed samples,
missing eligible zero samples, contradictory qualifications and unsupported
versions. It neither runs native code nor invokes current analysis or parsing.
The v1 format was first published in 0.3.0. Changes to its interpretation
require a new schema version.
