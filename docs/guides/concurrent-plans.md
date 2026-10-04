# Bounded concurrent plans

Available on `main` after merge (unreleased), `arun_concurrent_plan` runs
independent cells in isolated workers. You must provide both a worker limit
and an aggregate target-rate limit. The existing synchronous `run_plan` and
async `arun_plan` remain sequential.

## Declare independent cells and limits

```python
from iperf3_lib.config import ClientConfig
from iperf3_lib.concurrent_execution import ConcurrentExecutionPolicy
from iperf3_lib.concurrent_trials import arun_concurrent_plan
from iperf3_lib.trials import PlanBudget, TrialPolicy, TrialSpec, prepare_plan

plan = prepare_plan(
    [
        TrialSpec("a:0", "a", "measured", 0,
                  ClientConfig("host-a", duration=5, rate=1_000_000)),
        TrialSpec("b:0", "b", "measured", 0,
                  ClientConfig("host-b", duration=5, rate=1_000_000)),
    ],
    policy=TrialPolicy(repetitions=1),
    budget=PlanBudget(max_active_seconds=10, max_payload_bytes=1_250_000),
)

async def run_experiment():
    return await arun_concurrent_plan(
        plan,
        policy=ConcurrentExecutionPolicy(
            max_workers=2,
            max_active_target_bps=2_000_000,
        ),
        timeout=20,
    )
```

`max_active_target_bps` reserves each trial's aggregate target across parallel
streams and both directions, including warm-ups. For example, a bidirectional
trial with two streams and a per-stream rate of 1 Mbit/s reserves 4 Mbit/s.
Unlimited or unknown targets and trials larger than the cap are rejected
before any worker starts. The full plan and resource map are validated and
detached before execution.

This is a cap on **admitted target rates**, not measured wire traffic or network
overhead. `PlanBudget` continues to describe total work and payload estimates;
summed active seconds are not concurrent wall-clock duration. Limits apply to
one invocation. Independent calls and external traffic have separate ownership.

## Exclude shared resources and preserve cell order

Trials with the same canonical server host and port cannot overlap, regardless
of protocol or direction. IP addresses use compressed spelling; DNS names are
lowercased with trailing dots removed. This does not resolve DNS aliases or
discover shared infrastructure.

Use `resources` to declare additional exclusions by cell ID:

```python
execution = await arun_concurrent_plan(
    plan,
    policy=ConcurrentExecutionPolicy(2, 2_000_000),
    resources={"a": ("shared-link",), "b": ("shared-link",)},
)
```

These cells serialize even if they use different listeners. Caller keys add to
automatic endpoint exclusions. Unknown cell IDs, blank or duplicate keys, and
values other than tuples of strings are rejected. Use a shared key for DNS
aliases that may refer to one listener, or for resources whose contention must
be excluded from the experiment.

Every cell retains its declared warm-up and repetition dependencies. The
scheduler scans ready trials in declared order, admitting later independent
cells when an earlier one is temporarily blocked by a dependency, resource,
or rate reservation. It does not retry trials. Worker, rate and resource slots
remain reserved through executor queueing, startup, traffic and cleanup.

This first concurrent mode requires `pause_seconds=0` and rejects an explicit
`ClientConfig.client_port`. Fixed source-port ranges and nonzero concurrent
pause policies need separate native qualification. The sequential APIs retain
their existing configuration support and global pause semantics.

## Stop admission or terminate active work

| Trigger | New admissions | Already admitted trials |
| --- | --- | --- |
| `stop_on_error=True` and an unsuccessful trial | Stop | Finish normally |
| `stop_after_elapsed_seconds` reached | Stop | Finish normally |
| Parent task cancellation | Stop | Cancel once and await cleanup |
| Overall `timeout` expires | Stop | Cancel once and await cleanup |
| Unconfirmed worker cleanup | Stop | Cancel peers once and await cleanup |

The positive deadline starts after static validation, covers queueing and
execution, and still applies while peers finish after a soft admission stop.
Cleanup may extend beyond the deadline. Repeated cancellation cannot interrupt
the cleanup drain. Every completed child outcome is consumed before slots are
refilled, so an already-completed peer failure cannot be missed during admission.

Admission-only stops are not shutdown requests. Without an overall `timeout`,
active workers can take indefinitely long to finish, for example while a native
connection stalls. Supply a positive overall timeout when the drain must be
bounded. Cancellation, deadline expiry and unconfirmed cleanup instead use
**zero natural-completion grace**: they request cancellation of every live child
immediately. Each process owner then sends TERM, waits up to two seconds, and
escalates to KILL within its four-second cleanup-attempt budget. These cleanup
periods are distinct from a grace period for finishing a benchmark. Process
creation, callbacks and unconfirmed cleanup can extend Python API return time;
the library retains unconfirmed owners rather than claiming successful shutdown.

`ConcurrentPlanCancelledError` subclasses `asyncio.CancelledError` and retains
the original cancellation arguments. `ConcurrentPlanTimeoutError` subclasses
`TimeoutError`. `ConcurrentPlanCleanupError` subclasses `IperfLibraryError`,
takes precedence when cleanup is unconfirmed, and retains every original
`IperfCleanupError` in `cleanup_errors`, including its process owner. All three
expose a detached `partial_result`.

The result records two decisions: `stop_reason` and `stopped_offset_seconds`
describe the first admission stop; `termination_reason` and
`termination_offset_seconds` describe the first forced termination. For example,
an error can stop admission first, followed by a deadline that terminates peers.
The report preserves both causes and the outward exception is a timeout.

## Retain overlap and cleanup evidence

Interrupted and failed trials retain bounded copied events observed before
cancellation, deadline, worker crash or transport failure. An `exception` record
can have no events when failure precedes delivery. These events are diagnostic
evidence, not a completed native result or a performance sample.

```python
from pathlib import Path
from iperf3_lib.concurrent_execution import (
    ConcurrentPlanCancelledError, ConcurrentPlanCleanupError,
    ConcurrentPlanTimeoutError,
)
from iperf3_lib.concurrent_reports import (
    concurrent_report_from_execution, dumps_concurrent_report,
)

async def save_experiment(plan):
    try:
        execution = await arun_concurrent_plan(
            plan, policy=ConcurrentExecutionPolicy(2, 2_000_000), timeout=20,
        )
    except (ConcurrentPlanCancelledError, ConcurrentPlanTimeoutError,
            ConcurrentPlanCleanupError) as error:
        report = concurrent_report_from_execution(error.partial_result)
        Path("concurrent-partial.json").write_text(
            dumps_concurrent_report(report, indent=2), encoding="utf-8",
        )
        raise
    return concurrent_report_from_execution(execution)
```

`ConcurrentPlanResult.trials` retains declared order even when outcomes arrive
out of order. Each admitted trial has a one-based `admission_index`, resource
keys, target-rate reservation, and admission/finish/release offsets on the
parent's monotonic clock. Finishing means the parent consumed the child outcome
and cleanup attempt. Release equals finish only when cleanup is confirmed;
an unconfirmed owner retains its reservation with no release offset. These
intervals include queueing and startup; they do not establish native traffic
overlap without corresponding measured evidence.

Every declared trial remains in the report, including `not_run` entries between
completed independent cells. Completed trials retain canonical result artifacts.
Interrupted trials retain the same bounded diagnostic event prefix as
[sequential async plans](trials.md#cancel-an-async-plan-and-retain-its-evidence).
Partial events do not become complete results or performance samples.

The standalone envelope uses `kind="iperf3-lib.plan-execution"`,
`schema_version=3`, `execution_mode="bounded-concurrent-owned"`, and
`pause_semantics="zero-only"`. The strict codec checks derived targets and
resource keys, dependencies, admission order, overlapping reservations, worker
and rate ceilings, stop decisions and cleanup state. Loading needs no libiperf.
`render_concurrent_text` and `render_concurrent_junit` summarize execution;
neither performs a throughput assessment.

## Compare experiments with a declared contention policy

The default interpretation is descriptive analysis of each completed run.
Assessment-v1, sweep-v1 and sequential plan-v2 consumers reject concurrent result
types. Extracting an artifact removes that type boundary: `check_compatibility()`
checks native settings, but does **not** inspect this plan's peer traffic,
resource map, admission limits or overlap history. A compatible result therefore
does not establish comparable contention, and must not be used as automatic
acceptance of a concurrent experiment against an isolated baseline.

An application that makes a cross-run comparison must declare and retain its
comparison method separately. A matched concurrent baseline should specify:

1. The endpoint topology, shared resources, worker and rate limits, peer roles,
   protocols, directions, targets, durations, warm-ups and repetition policy
   that must match. Record intended differences before examining outcomes.
2. The simultaneous traffic conditions required for each measured trial. Use
   positive native measurements and observed worker activity to establish those
   conditions. Reservation overlap includes queueing and startup and cannot
   substitute for observed traffic. A shared user key prevents overlap; different
   keys do not prove that network or host resources are independent.
3. The measurement endpoint, bytes/time statistic, sample eligibility, minimum
   valid repetitions and acceptance rule. Retain every failed, interrupted and
   unstarted trial. Exclude partial events from completed samples, and classify
   missing or unmatched contention evidence as inconclusive.

Store that method and the complete schema-v3 histories with both candidate and
baseline artifacts. A namespaced report extension can hold application evidence;
the report codec validates its JSON structure, not the comparison method. This
release provides execution evidence and descriptive result analysis, not an
automatic concurrent throughput assessment. Isolated sequential baselines require
a separately justified experiment design and are not matched concurrent controls.

Shared pools, nonzero concurrent pauses and additional assessment APIs are outside
this API's contract. [Issue #36](https://github.com/dariuszpanas/iperf3-lib/issues/36)
tracks qualification of its ownership, admission and resource-cleanup guarantees.
