# Advanced execution: design and release disposition

Status: **implementation contracts for the unreleased 0.4.0 scope**. The original
design review and native-event exploration were recorded on 2026-09-26.
The [native-control guide](../../docs/guides/native-controls.md) and
[live-event guide](../../docs/guides/live-events.md) document the public interfaces.
The historical probe does not establish their broader acceptance criteria.
The [roadmap](../roadmap.md) and
[release-scope issue #26](https://github.com/dariuszpanas/iperf3-lib/issues/26)
track the overall release decision.

The experiment layer retains finite sequential trials and parameter sweeps,
with separate owned async plans and opt-in bounded concurrent execution. Basic
clients retain the direct CFFI path; expanded controls and explicit worker
requests use isolated Python/CFFI execution. The proposals below retain their
qualification requirements. Their issues remain open until the individual
criteria have evidence; implementation and collected tests alone are insufficient.

| Proposal | Decision | Evidence needed for complete qualification |
| --- | --- | --- |
| [Adaptive UDP #34](https://github.com/dariuszpanas/iperf3-lib/issues/34) | Qualified and merged through PR69; see the [unreleased guide](../../docs/guides/adaptive-udp.md). | The issue retains exact-source synthetic, minimum/latest native, isolated impairment, and installed wheel/sdist evidence. |
| [Live events #35](https://github.com/dariuszpanas/iperf3-lib/issues/35) | Owned typed async contexts preserve complete-result access and legacy callbacks. | Minimum/latest event assembly, bounded delivery, failure/overflow/abandonment tests, and lifecycle qualification. |
| [Process isolation #36](https://github.com/dariuszpanas/iperf3-lib/issues/36) | Qualified and merged through PR68. | The [acceptance map](isolated-execution-qualification.md) and issue retain the exact-source installed matrix, bounded transport, partial evidence and resource cleanup qualification. |

Native event shapes have been observed in a small loopback experiment. The
[observation report](native-event-observations.md) records exactly what was
tested. Adaptive selection, production event delivery, isolated workers,
impairment, stress, and cancellation have **not** been qualified by that probe.

## Existing guarantees remain in force

`Client.run()` returns a complete result, including when a worker delivers live
events. Async methods now always use isolated workers and propagate cancellation
after worker cleanup. An active callback must return before the await completes;
unconfirmed cleanup raises `IperfCleanupError`. Owned async plans also retain
completed artifacts and bounded diagnostic partial events after cancellation;
partial events are not complete native artifacts. Sequential histories use
schema 2 and concurrent reservation histories use schema 3. Linux workers now
install a parent-death signal before native execution; see the
[worker lifetime contract](../../docs/guides/running-tests.md#worker-lifetime-on-linux)
for bootstrap, creating-thread and descendant boundaries.
Concurrent direct native calls in one process
remain unsupported. A plan's admission estimates do not provide a hard deadline;
an explicit worker timeout has a separate process-termination contract.

Every accepted configuration field must be applied exactly once or rejected.
Normal native lifecycles must free every non-null test exactly once, including
error paths. The [compatibility reference](../../docs/reference/compatibility.md)
defines the current platform and native-version coverage.

## Adaptive UDP exploration

### Admission and evidence

The unreleased implementation is split between `adaptive.py` (pure decisions),
`adaptive_execution.py` (sequential batches) and `adaptive_reports.py` (standalone
schema-1 archives). The [user guide](../../docs/guides/adaptive-udp.md) states its
explicit limitations and public API. The following criteria remain the
qualification contract for that implementation.

Build on [finite sweeps #33](https://github.com/dariuszpanas/iperf3-lib/issues/33)
and [trial reports #32](https://github.com/dariuszpanas/iperf3-lib/issues/32).
Keep the planner pure: completed trial evidence goes in, and a proposed finite
batch with its admission reason comes out. Execution remains a separate step.

The initial contract should require:

- UDP and one direction; fixed endpoints, stream count, block size, duration,
  omit policy, repetitions, warm-ups, and cooldowns.
- Positive minimum/maximum aggregate offered rates and a finite initial grid.
  Zero or unlimited rates are invalid for this plan.
- Maximum distinct rates, trial count, refinement depth, active-time estimate,
  and payload estimate. Include warm-ups and repetitions in the whole-plan
  budget; each next batch must fit the remaining allowance.
- A receiver-loss threshold, minimum count of valid repetitions, and minimum
  achieved-sender/offered-rate fraction.

Retain requested aggregate/per-stream allocation, verified native target,
achieved sender bytes/time, receiver bytes/time, receiver lost/total packet
counts, native-reported loss, and execution/data-quality outcomes at every
rate. Count-derived and native-reported loss remain separate when they differ.
Unknown or zero packet denominators cannot establish a valid loss percentage.

A sender that fails to generate the requested load cannot establish success
at that requested rate merely because the receiver reports low loss. Keep
failed and under-driven trials in the report; never retry until a pass while
hiding prior observations.

### Selection without a monotonicity assumption

A conservative first policy can require at least the configured number of
valid trials, with every selected receiver-loss value at or below the loss
threshold and every selected sender/offered fraction at or above its minimum.
Any more tolerant aggregation must be explicitly chosen and recorded.

The first implementation additionally requires confirmation batches before a
provisional candidate becomes eligible. A failed confirmation remains in the
population. Initial grids include both declared endpoints; all batches reserve
their complete trial/time/payload estimates, including failures and unstarted
trials. Global elapsed admission limits are explicitly unsupported by this API
rather than reset for each batch. Observed cooldowns include batch boundaries.

Start with the declared grid. Refine adjacent tested rates with differing
outcomes using integer midpoints, and repeat promising upper candidates.
Stop when no new midpoint exists, a configured budget is exhausted, or the
next batch cannot be admitted. Preserve execution order, cooldowns, and the
reason for each selection.

Non-monotonic observations remain evidence. A lower-rate failure does not
invalidate an observed higher-rate pass; a higher-rate pass does not establish
that every lower rate passes. A binary-search assumption cannot turn these
measurements into a proof of a continuous capacity boundary.

Report the highest **tested eligible** offered rate and the acceptable tested
points, with gaps explicitly unknown. If the configured ceiling passes, the
upper boundary is censored by that ceiling. Other outcomes are
`no_acceptable_tested_rate` and `inconclusive` when evidence or budgets are
insufficient. An apparent operating range describes tested conditions;
interpolation and physical network capacity remain unmeasured.

### Qualification contract

Exercise non-monotonic curves, sender under-delivery, missing/zero packet
counts, partial failures, exact threshold boundaries, integer-grid exhaustion,
and all admission limits with controlled observations. Then qualify bounded
native mechanics on libiperf 3.19.1 and 3.21 and a controlled impaired link
with recorded rate/loss settings. Clean loopback alone cannot validate the
loss-threshold algorithm. Explicit finite sweeps remain the published 0.3.0
mechanism; adaptive UDP remains unreleased until separately selected and qualified.

## Typed live events

### Event and result contracts

`Client.events()` and `Server.events_once()` return inert `EventStream`
instances. Async context entry snapshots configuration and starts one owned
isolated operation. One consumer iterates typed `LiveEvent` values and obtains
the final result through `result()`. Context exit and explicit `aclose()` settle
ownership; an early break or cancelled wait cannot leave an unowned worker.
Existing complete-result methods and four-field `NativeEvent` callbacks keep
their public shape.

Each typed event retains run identity, a delivery sequence, optional native
capture sequence, copied raw evidence, kind, and separately named monotonic
arrival and native measurement timing. Arrival order describes delivery, not packet chronology.
Direction, endpoint observation, scope, and local stream identity must follow
the shared result semantics, with unknown values explicit.

Payload kinds distinguish start, interval, native error, end, complete document,
server output, malformed input, delivery gap, and worker terminal state.
Unknown future kinds remain advisory raw events. A native end event and the
wrapper's terminal state are distinct: the probe observed end after an error.
A monolithic document is not an additional interval and is not guaranteed
on every supported native version.

The [native observation report](native-event-observations.md) shows that
3.19.1 lacks the full-output setter and returned no complete document in
streaming mode. That endpoint reconstructs from bounded retained envelopes;
missing or malformed input makes a reconstructed result unsuccessful and
incomplete even when an end fragment exists. An original native error remains
a failure. On 3.21, an independently retained valid complete document can
recover final evidence despite lost progress. Combined serialized evidence
counts against the retention allowance. Neither representation changes the
existing result artifact schema or `iperf3_lib.native_json` extension shape.

### Callback ownership and delivery

The native callback may copy a bounded payload and enqueue without waiting.
Keep its CFFI reference alive until the native call has returned and cleanup
is complete. Parse JSON, normalize results, and invoke application code
outside that callback. Native source frees the serialized event buffer after
the callback returns; retaining its pointer is unsafe. See the
[3.21 callback implementation](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L3085-L3110).

Bound both item count and total bytes, and cap one event's size. Reserve error
and terminal bookkeeping separately so interval overflow cannot erase final
status. If intervals are dropped, retain sequence gaps and cumulative dropped
counts. An incomplete event sequence cannot be presented as a complete result.

Malformed input and copy failures produce bounded diagnostics and explicit
capture quality. Lossy reconstruction cannot report success; a separately
retained valid native complete document can recover final evidence.
Neither parsing errors nor user
callback exceptions may escape through C. Consumer abandonment stops delivery
and discards future nonterminal payloads; the owner must still drain/clean up
the direct run or await an isolated worker's terminal state. Queue bounds do
not cancel a native call.

### Qualification still required

The 22 exploratory probes establish small event-shape observations only.
The [live-event qualification map](live-event-qualification.md) connects these
requirements to executable tests and exact-source installed receipts.
Production qualification must cover malformed/oversized payloads, copy and
consumer failures, queue saturation, dropped-event reporting, abandonment,
late/error terminal events, and minimum/latest final-result behavior across
protocols and directions. Verify callback lifetime and exactly-once native
cleanup on every normal/error path. Stronger cancellation and concurrency
guarantees depend on qualified isolation.

## Isolated Python/CFFI workers

### Startup and transport

The implementation starts a fresh Python interpreter through `subprocess.Popen`
for each client operation or sequential server session. The worker loads the selected
libiperf and owns one native test; C pointers and live callbacks never cross
the process boundary. Sequential server runs allocate and free a separate native
test for each iteration. This leaves application multiprocessing settings alone.

Private protocol version 1 uses four-byte big-endian lengths and strict UTF-8
JSON objects on dedicated pipes. Request and worker nonces, contiguous transport
sequences and run indices bind every message to its session. Duplicate JSON
keys, nonfinite numbers, malformed/truncated/oversized frames, replay and wrong
ordering invalidate that channel. Server continuation commands authorize exactly
one next run. Native event sequence numbers remain separate and can have gaps
when delivery is dropped.

Frames are limited to 16 MiB; live events to 1 MiB. Each side admits at most
256 queued events and 8 MiB of serialized event data, plus a separate bounded
FIFO allowance for control messages. Native capture and decoded-object overhead
are not covered by those wire limits. Oversized final results fail explicitly.
Producer readiness follows configuration/native setup and records the actual
PID, package/Python/native versions and an honestly labelled library selector.
It establishes neither a listening socket nor binary authenticity. Results
retain that receipt separately from the independently versioned
[result artifact](../../docs/guides/artifacts.md).

Transport completion requires the terminal receipt, clean EOF and reaped exit
zero. The parent rejects bytes after terminal and bounds its wait for output
closure and process exit. Cancellation or a deadline that already won cannot
be replaced by a late result. Live callbacks and sequential result callbacks
can execute before the final session receipt; only a successful API return
establishes complete session shutdown.

Bound admitted workers and total traffic before spawning. Each worker runs
one native operation at a time. Applications must also account for contention
at the destination server. Concurrent measurements change the experiment;
their comparability with sequential baselines requires an explicit policy.

The concurrent API requires a positive worker limit and a finite aggregate
target-rate cap across all streams and directions. Reservations cover queueing,
startup, traffic and cleanup. Canonical endpoint exclusions and caller resource
keys serialize conflicts; per-cell dependencies preserve warm-up/repetition order.
The cap concerns admitted targets, not observed wire traffic or independent
invocations. Unlimited rates, fixed client ports and nonzero concurrent pauses
are explicitly rejected in this contract.

The [comparison methodology](../../docs/guides/concurrent-plans.md#compare-experiments-with-a-declared-contention-policy)
uses descriptive analysis by default. Applications making an inference must
retain a declared cohort/contention method, matched concurrent baseline and
measured overlap evidence. Native-setting compatibility alone does not establish
matching peer traffic; unknown contention makes the comparison inconclusive.
Existing v1 assessment/sweep consumers reject v2/v3 execution histories.

### Deadlines, cancellation, and cleanup

Use monotonic parent deadlines covering startup, connection, transfer, IPC,
and shutdown. The chosen shutdown contract has **zero natural-completion grace**
for cancellation, deadline expiry and unconfirmed cleanup. It requests cancellation
of all owned children, then each process owner sends TERM, waits up to two seconds,
and escalates to KILL within a four-second cleanup-attempt budget. Repeated
cancellation cannot shorten cleanup. No arbitrary in-flight libiperf cancellation
hook is qualified. Once forced termination starts, a late success frame cannot
replace the parent's terminal decision.

`stop_on_error` and the elapsed admission budget are admission-only decisions,
not shutdown requests: new trials stop, while admitted workers finish naturally.
The optional overall timeout remains active during this drain and forces shutdown
when reached. Without that timeout, a stalled native call can prolong the drain
indefinitely. This distinction preserves sequential admission semantics without
silently treating a between-trial budget as cancellation. The current contract
does not expose a configurable nonzero natural-completion grace period.

Cleanup budgets do not bound Python return in the presence of process-creation
delays, active application callbacks or unconfirmed reaping. Cleanup failures
retain original owners and take outward precedence. Concurrent reports retain
both the first admission-stop decision and the first hard-termination decision,
including a hard deadline that occurs after an admission-only stop.

Forced termination produces an incomplete execution outcome with preserved
available evidence. It cannot fabricate a native summary or an exactly-once
free call after a process is killed. Normal paths still free every allocated
native test exactly once. Forced exit relies on process-owned OS resource
release, which must be measured for workers, sockets, and descendants.

Execution failure stays separate from performance acceptance. Partial trials
remain in the plan and may make its assessment inconclusive; they cannot be
silently removed from a distribution. Parent cancellation must await terminal
cleanup before releasing an admission slot.

The parent owns process handles and bounded reaping on every path. Workers
should create no application subprocesses. The current Linux bootstrap installs
`PR_SET_PDEATHSIG` with `SIGKILL` and verifies the expected parent before loading
the worker. Linux binds this signal to the creating thread, which the library
retains throughout execution and cleanup. The init process or a subreaper owns
reaping after parent death. This does not supervise arbitrary descendants or
the interpreter before bootstrap. See the
[Linux parent-death contract](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html).
Python also documents that forced termination can skip cleanup and damage
pipes/queues. See
[the termination contract](https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.terminate).

Avoid shared mutable queues or locks across workers that may be killed.
Truncated or malformed frames invalidate only that worker's channel. Cap
pending output, drain while waiting, and reject replayed/wrong-request terminal
frames. Waiting for exit while a worker blocks sending its result can deadlock.

### Qualification still required

Test startup/import/native-load failure, malformed/oversized frames, crashes
before and after allocation, stalled connection/run, user cancellation,
deadline races, parent death, truncated output, saturation, repeated workers,
and worker/traffic admission limits. Measure worker/socket/orphan cleanup.
The [qualification map](isolated-execution-qualification.md) defines the required
contract evidence, repeated resource inventories and exact-candidate audit.
Qualify installed wheels and sdists across Python 3.12–3.14 and both native
endpoints. This proposal keeps Python/CFFI as the execution backend; it does
not use the iperf3 CLI to execute application benchmarks.

Linux support remains explicit. Windows is a development host, and the
availability of multiprocessing does not establish native platform support.
The worker implementation is documented separately; the observations in this
design do not establish its cancellation, stress or orphan-cleanup qualification.

## Release follow-through

Close each issue only when its implementation and qualification criteria are
met, with acceptance checkboxes and retained exact-source evidence reconciled.
Issues #34 and #36 record their completed qualification. The live-event issue
tracks the remaining candidate evidence separately. The native-controls implementation must preserve complete results and
explicit lifecycle/concurrency limits while its combined source is qualified.
Revisit each broader proposal with its own acceptance evidence and release
decision; adaptive UDP selection remains separate from explicit sweeps.
