# Advanced execution: design and release disposition

Status: **design criteria, with a first worker/event implementation added under
[#53](https://github.com/dariuszpanas/iperf3-lib/issues/53)**. Reviewed on 2026-09-26.
The [native-control guide](../../docs/guides/native-controls.md) documents that narrower
public contract. This design's broader acceptance criteria are not established
by implementation or by the historical native-event probe alone. The [roadmap](../roadmap.md) and
[release-scope issue #26](https://github.com/dariuszpanas/iperf3-lib/issues/26)
track the overall release decision.

The experiment layer uses finite, sequential trials and explicit parameter
sweeps. Basic clients retain the direct CFFI path; expanded controls and explicit
worker requests use isolated Python/CFFI execution. The proposals below retain
their broader qualification requirements. Their issues remain open until the
individual criteria have evidence.

| Proposal | Decision | Evidence needed for complete qualification |
| --- | --- | --- |
| [Adaptive UDP #34](https://github.com/dariuszpanas/iperf3-lib/issues/34) | Follow bounded explicit sweeps with a separate, evidence-driven planner. | Deterministic decision tests, bounded native mechanics, and a controlled impaired-link experiment. |
| [Live events #35](https://github.com/dariuszpanas/iperf3-lib/issues/35) | Keep a separate event API and retain the complete-result contract. | Minimum/latest event assembly, bounded delivery, failure/overflow/abandonment tests, and lifecycle qualification. |
| [Process isolation #36](https://github.com/dariuszpanas/iperf3-lib/issues/36) | A first worker supports #53; qualify broader worker reuse/concurrency separately. | Deadline/cancellation/crash qualification, transport bounds, and measured cleanup across the supported matrix. |

Native event shapes have been observed in a small loopback experiment. The
[observation report](native-event-observations.md) records exactly what was
tested. Adaptive selection, production event delivery, isolated workers,
impairment, stress, and cancellation have **not** been qualified by that probe.

## Existing guarantees remain in force

`Client.run()` returns a complete result, including when a worker delivers live
events. Async methods now always use isolated workers and propagate cancellation
after worker cleanup. An active callback must return before the await completes;
unconfirmed cleanup raises `IperfCleanupError`. This post-0.3.0 change does not
establish plan cancellation or retained partial artifacts. Linux workers now
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

### Qualification still required

Exercise non-monotonic curves, sender under-delivery, missing/zero packet
counts, partial failures, exact threshold boundaries, integer-grid exhaustion,
and all admission limits with controlled observations. Then qualify bounded
native mechanics on libiperf 3.19.1 and 3.21 and a controlled impaired link
with recorded rate/loss settings. Clean loopback alone cannot validate the
loss-threshold algorithm. Explicit finite sweeps remain the planned 0.3.0
mechanism for these experiments.

## Typed live events

### Event and result contracts

Explore a separate `events()` API; the name and transport are provisional.
An asynchronous iterator is a candidate, subject to ownership and delivery
qualification. Existing methods keep their complete-result behavior.

Each event should retain run ID, increasing callback-arrival sequence, copied
raw payload, event kind, monotonic arrival offset, and optional native
measurement timestamps. Arrival order describes delivery, not packet chronology.
Direction, endpoint observation, scope, and local stream identity must follow
the shared result semantics, with unknown values explicit.

Candidate kinds are start, interval, native error, end, complete document,
server output, malformed input, delivery gap, and worker terminal state.
Unknown future kinds remain advisory raw events. A native end event and the
wrapper's terminal state are distinct: the probe observed end after an error.
A monolithic document is not an additional interval and is not guaranteed
on every supported native version.

The [native observation report](native-event-observations.md) shows that
3.19.1 lacks the full-output setter and returned no complete document in
streaming mode. Supporting that endpoint requires qualified event assembly
or an explicitly narrower event-only terminal contract. On 3.21, retained
complete JSON can duplicate event data and must count against memory bounds.
Choose and document this compatibility strategy before exposing the API.

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

Malformed input should produce a bounded diagnostic. Copy failures become
explicit terminal data-quality failures. Neither parsing errors nor user
callback exceptions may escape through C. Consumer abandonment stops delivery
and discards future nonterminal payloads; the owner must still drain/clean up
the direct run or await an isolated worker's terminal state. Queue bounds do
not cancel a native call.

### Qualification still required

The 22 exploratory probes establish small event-shape observations only.
Production qualification must cover malformed/oversized payloads, copy and
consumer failures, queue saturation, dropped-event reporting, abandonment,
late/error terminal events, and minimum/latest final-result behavior across
protocols and directions. Verify callback lifetime and exactly-once native
cleanup on every normal/error path. Stronger cancellation and concurrency
guarantees depend on qualified isolation.

## Isolated Python/CFFI workers

### Startup and transport

Start with one fresh Python process per trial. The worker loads the selected
libiperf and owns one native test; C pointers and live callbacks never cross
the process boundary. An explicit spawn context is the initial design
candidate for the supported Linux matrix. Do not change the application's
global multiprocessing context. A caller-provided context would need to meet
the documented, tested start-method contract.

Python's defaults vary by platform and version, including a POSIX default
change in 3.14. Selecting and testing a context explicitly avoids relying on
those defaults. See [Python's start-method documentation](https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods).

Version the IPC envelope independently of the
[result artifact](../../docs/guides/artifacts.md): request ID, worker ID, protocol
version, validated configuration/intent, selected library identity, and
serialized artifacts/events. Use bounded length-prefixed JSON bytes and
dedicated per-worker channels. Validate the request in both processes and
acknowledge readiness only after native loading and capability inspection.
Retain producer identity and worker PID in terminal receipts.

Bound admitted workers and total traffic before spawning. Each worker runs
one native operation at a time. Applications must also account for contention
at the destination server. Concurrent measurements change the experiment;
their comparability with sequential baselines requires an explicit policy.

### Deadlines, cancellation, and cleanup

Use monotonic parent deadlines covering startup, connection, transfer, IPC,
and shutdown. Soft shutdown stops new admissions and requests orderly
completion; no arbitrary in-flight libiperf cancellation hook is qualified.
After a grace period, terminate, then kill if necessary, and reap the owned
worker. Once forced termination starts, a late success frame cannot replace
the parent's terminal decision.

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
Qualify installed wheels and sdists across Python 3.12–3.14 and both native
endpoints. This proposal keeps Python/CFFI as the execution backend; it does
not use the iperf3 CLI to execute application benchmarks.

Linux support remains explicit. Windows is a development host, and the
availability of multiprocessing does not establish native platform support.
The worker implementation is documented separately; the observations in this
design do not establish its cancellation, stress or orphan-cleanup qualification.

## Release follow-through

Keep #34–36 open until their implementation and qualification criteria are
met. This document records a design disposition, not completion of those
criteria. The native-controls implementation must preserve complete results and
explicit lifecycle/concurrency limits while its combined source is qualified.
Revisit each broader proposal with its own acceptance evidence and release
decision; adaptive UDP selection remains separate from explicit sweeps.
