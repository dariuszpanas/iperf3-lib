# Adaptive UDP experiments

This exploratory API is **unreleased**. It builds on [bounded trials](trials.md)
and [explicit sweeps](sweeps.md) to find the highest tested offered rate that
meets declared receiver-loss and sender-delivery criteria. Its result describes
the tested conditions; it does not estimate exact physical network capacity.

## Declare the experiment and its limits

```python
from iperf3_lib.adaptive import AdaptiveUDPPolicy, prepare_adaptive_udp
from iperf3_lib.adaptive_execution import run_adaptive_udp
from iperf3_lib.config import ClientConfig
from iperf3_lib.trials import PlanBudget, TrialPolicy

prepared = prepare_adaptive_udp(
    ClientConfig(
        "127.0.0.1", protocol="udp", duration=2, blksize=1200,
    ),
    initial_rates=(500_000, 1_000_000, 2_000_000),
    policy=TrialPolicy(
        repetitions=3, warmup_runs=1, pause_seconds=0.5, max_trials=32,
    ),
    budget=PlanBudget(max_active_seconds=64, max_payload_bytes=16_000_000),
    adaptive_policy=AdaptiveUDPPolicy(
        min_rate_bps=500_000,
        max_rate_bps=2_000_000,
        max_distinct_rates=6,
        max_refinement_depth=2,
        receiver_loss_percent=1.0,
        minimum_valid_trials=3,
        minimum_sender_fraction=0.95,
        confirmation_batches=1,
    ),
)
result = run_adaptive_udp(prepared)
print(result.outcome, result.highest_eligible_bps, result.decision.reason)
```

Run an iperf3 server at the configured endpoint first. Rates are aggregate
requested bits per second in the selected direction. Parallel streams share
that target using the existing [rate allocation](configuration-intent.md)
contract. Integer division can leave part of the request unallocated; the
report keeps the requested total, per-stream native allocation, allocated total
and remainder separately.

The configuration must use UDP, one direction, a positive fixed duration and
an explicit block size. Forward and reverse experiments are separate. Endpoints,
stream count, block size, duration, omit and other native controls remain fixed.
Leave `ClientConfig.rate` unset: the planner supplies aggregate rate intent for
each trial. Count-terminated and unlimited-duration requests cannot be admitted.

The initial grid must be finite, distinct and within the declared rate bounds,
including both endpoints. A single point is allowed when the endpoints are
equal. Its declared order is recorded. No hidden randomization changes that order.

`minimum_valid_trials` must not exceed `TrialPolicy.repetitions`, and
`confirmation_batches` must be at least one. The receiver-loss threshold is
between 0 and 100 percent, inclusive; the minimum sender fraction is greater
than zero and at most one.

All limits are explicit: offered-rate bounds, distinct rates, refinement depth,
total trial count, estimated active seconds and estimated payload bytes.
Warm-ups, repetitions and native omit periods count against admission estimates.
The complete initial grid must fit before execution starts, and each subsequent
batch must fit the remaining allowance. An admitted failed or unstarted trial
does not refund its reservation.

These are admission limits. Payload estimates exclude network overhead; they
cannot enforce actual wire-byte ceilings or interrupt a blocking native call.
The first adaptive API rejects `PlanBudget.stop_after_elapsed_seconds` rather
than resetting an elapsed limit for every batch. Execution is sequential and
retains the library's non-reentrant native contract across callers.

## Follow the recorded decisions

The pure planner accepts a preparation and retained completed batches through
`next_adaptive_batch`. It proposes a finite `PreparedPlan` with a selection reason
or returns a terminal reason. `run_adaptive_udp` executes those proposals using
the existing trial runner. An optional `executor(spec) -> Result` supports a
caller-owned transport or controlled observations.

The algorithm starts with the declared coarse grid. It then confirms the
highest provisionally acceptable candidate with the configured number of
additional batches. All previous measurements remain in its assessment.
Confirmation is a declared measurement stage, not a retry that erases a failure.
If confirmation fails, a lower provisional candidate can be considered with its
own complete history.

Refinement inserts integer midpoints between adjacent tested rates with
differing observed outcomes, subject to all limits. It selects the highest such
interval first and retains every tested point. Unresolved gaps remain unknown
when a limit stops exploration. An acceptable lower point and an acceptable
higher point do not establish anything about the untested rates between them.
Likewise, a lower-rate failure does not invalidate a measured higher-rate pass.
Confirmation bookkeeping alone does not create a differing loss outcome.

Each batch reuses `TrialPolicy` repetitions, warm-ups and cooldowns. A cooldown
also separates successive batches. `AdaptiveBatchResult.pause_before_seconds`
records that observed pause; `result.observed_pause_seconds` includes it and
the pauses recorded inside each `PlanResult`. Every batch retains complete
trial specifications, artifacts, exceptions, timings and unstarted reasons.
`stop_on_error=True` stops further admission after an execution error.

## Inspect evidence before accepting a rate

```python
for rate in result.summaries:
    print(rate.rate_bps, rate.status, rate.reasons)
    for sample in rate.observations:
        print(
            sample.trial_id, sample.sender_bps, sample.receiver_bps,
            sample.count_loss_percent, sample.native_loss_percent,
            sample.sender_fraction, sample.reasons,
        )
```

Sender and receiver throughput are derived independently from their own bytes
and measured seconds. A receiver measurement is never replaced by sender data.
Receiver loss uses `100 * lost_packets / packets` with a positive, valid packet
denominator. Native-reported loss is preserved separately, including a difference
from the count-derived value. Missing evidence remains unknown; measured zero
remains zero.

The sender fraction is achieved sender throughput divided by the **requested**
aggregate rate, not by a silently substituted effective target. Low loss from
a sender that did not generate enough traffic cannot qualify that requested rate.
Verified native target and fixed settings must have observed evidence. A caller
request alone is not a native observation.

Eligibility is conservative: every measured trial at a rate must have valid
evidence and satisfy both criteria, with at least `minimum_valid_trials` and
the required confirmation batches. Warm-ups remain in the history but do not
enter that population. Failed, incomplete, under-driven and unqualified trials
remain visible and cannot be discarded to make a rate pass. Variation is kept
as individual measurements, rather than hidden by an average.

The possible outcomes are:

- `acceptable_tested_rates`: at least one rate has confirmed qualifying evidence.
  `acceptable_rates_bps` lists those points and `highest_eligible_bps` selects
  their highest requested rate.
- `no_acceptable_tested_rate`: all tested points have conclusive rejection
  evidence under the declared criteria.
- `inconclusive`: evidence or admission allowance cannot establish an acceptable
  point or a conclusive all-points rejection. A candidate that cannot receive
  required confirmation remains provisional.

`ceiling_censored=True` means the configured maximum passed; it does not locate
an upper network boundary. All gaps remain unknown, and results outside the
tested conditions remain unmeasured. Inspect the terminal admission reason
alongside the outcome.

`run_adaptive_udp` returns when `result.decision.batch is None`. The pure
`summarize_adaptive_udp` function also accepts intermediate histories: a non-null
`decision.batch` is a proposed next batch, not an executed measurement. An
intermediate outcome describes only the evidence collected so far.

## Save a standalone report

```python
from pathlib import Path
from iperf3_lib.adaptive_reports import (
    dumps_adaptive_udp_report, loads_adaptive_udp_report,
    report_from_adaptive_udp, render_adaptive_udp_text,
)

report = report_from_adaptive_udp(result)
Path("adaptive.json").write_text(
    dumps_adaptive_udp_report(report, indent=2), encoding="utf-8",
)
restored = loads_adaptive_udp_report(Path("adaptive.json").read_text("utf-8"))
print(render_adaptive_udp_text(restored))
```

The standalone `iperf3-lib.adaptive-udp` report uses schema 1 and algorithm
revision `conservative-tested-rates-v1`. It retains preparation, batch decisions,
common `PlanResult` histories, observations and current assessment. Strict loading
checks the frozen schema's decisions and arithmetic against those histories.
It does not run native traffic, replay an RNG or invoke current analysis.
Existing assessment, sweep and owned-plan report schemas keep their meanings.

For a later experiment or retained baseline, use the existing artifact and
[comparison methodology](analysis.md) contracts. Record matching endpoints,
direction, payload, duration, omit policy, cooldown, producer versions and any
controlled impairment. This experiment does not make two separate sessions
automatically comparable.

Finite samples cannot establish a universal loss guarantee. Host scheduling,
receiver behavior, packet size, pacing, queues and concurrent traffic can change
the observations. A tested point is evidence under its recorded conditions;
interpolation, extrapolation and exact physical capacity remain unmeasured.
