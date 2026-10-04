# Consume live events

Live events let an application show progress while a benchmark runs. Use an
owned async context to consume them and retrieve the completed `Result` from
the same operation. This API is an unreleased addition after 0.3.0.

```python
import asyncio

from iperf3_lib import Client, ClientConfig


async def main():
    client = Client(ClientConfig(
        server="127.0.0.1", duration=2, interval_seconds=0.25,
    ))
    async with client.events(timeout=10) as stream:
        async for event in stream:
            print(event.kind, event.payload)
        result = await stream.result()
    print(result.ok, result.error)


asyncio.run(main())
```

The destination needs a listening iperf3 server. For a Python server, use
`async with server.events_once(timeout=10) as stream` to observe one accepted
test and obtain its server-side result. The timeout includes worker startup
and the wait for a client. Worker readiness alone does not establish that the
server socket is listening.

## Own the operation

Creating a stream starts no worker. Entering its async context admits the
operation and snapshots configuration. A stream has one consumer and cannot be
entered again. Keep consumption inside `async with`; its exit path handles an
early `break`, an application exception, and cancellation, and awaits owned
worker cleanup. `aclose()` provides explicit, idempotent closure.
Closure retains the bounded buffered observations and terminal event for
subsequent reads; an already completed result remains available through
`result()`. Closing ends native ownership without erasing retained evidence.

The iterator supplies progress; `await stream.result()` supplies the completed
result, including an unsuccessful native result. Native `end`, `error`, and
complete-document messages are observations. Only the wrapper's terminal
event describes the settled operation, after cleanup. An operation exception
does not become a successful result merely because native `end` arrived.

Iteration yields one terminal event and then stops. `result()` raises an
operation exception; context exit also raises an operation error that the
consumer has not observed. An early close cancels an unfinished operation
without treating that intentional cancellation as an unexpected error. If the
context body raises, its exception takes precedence over an ordinary operation
error; a cleanup failure takes precedence and retains the earlier cause.
Cancelling an iteration or result wait stops the owned operation and waits for
cleanup before propagating cancellation. Repeated cancellation cannot abandon
that cleanup.

The [async execution contract](running-tests.md#integrate-with-asyncio) still
applies: cancellation requests process termination and waits for cleanup;
unconfirmed cleanup raises `IperfCleanupError` and retains ownership for later
reaping. Forced termination cannot prove that native C finalizers ran. A
server instance remains unavailable while its owned worker is unreaped.

These methods always use an isolated Python/CFFI worker. Basic synchronous
`Client.run()` calls remain non-reentrant in one process. Wrapping a direct
call in an application executor does not add cancellation support.

## Interpret progress and final evidence

Typed events distinguish worker state, native start, intervals, native errors,
native end, complete documents, server output, unknown or malformed input,
delivery gaps, and wrapper terminal status. A native complete document is not
an additional interval. Unknown future native kinds retain advisory raw
evidence without inventing measurements.

Run identity associates events with the eventual result's worker receipt.
Native callback order, transport order, and consumer delivery order are
different counters. Callback arrival uses a monotonic offset; native interval
start/end values describe measurement time. Arrival time must not be used to
derive throughput or packet chronology.

`LiveEvent` exposes `request_id`, `worker_id`, `run_index`,
`delivery_sequence`, optional `capture_sequence`, `arrival_offset_seconds`,
and wall-clock `received_at_seconds`, alongside `kind` and its typed `payload`.
Wrapper-generated events do not invent native capture or arrival values.

| Kind | Payload type | Meaning |
| --- | --- | --- |
| `worker_state` | `WorkerStatePayload` | Starting or ready; no measured traffic claim. |
| `native_start` | `NativeStartPayload` | Native protocol, method, role, and copied setup evidence. |
| `interval` | `IntervalPayload` | Raw interval and typed `Measurement` observations. |
| `native_error` | `NativeErrorPayload` | Native error evidence; the owner may still be cleaning up. |
| `native_end` | `NativeEndPayload` | Native summaries; not wrapper completion. |
| `native_document` | `NativeDocumentPayload` | Complete native document and its projected measurements. |
| `server_output` | `ServerOutputPayload` | Advisory server output. |
| `unknown` | `UnknownPayload` | Preserved future native kind and copied evidence. |
| `malformed` | `MalformedPayload` | Bounded parse or projection diagnostic. |
| `delivery_gap` | `DeliveryGapPayload` | Dropped count and the stage where loss occurred. |
| `terminal` | `TerminalPayload` | Settled operation outcome, result availability, native status, and cleanup/capture/delivery evidence. |

Import payload types and `Measurement` from `iperf3_lib.events`; `LiveEvent`
and `EventStream` are also available from the package root. A terminal
`outcome="completed"` means the operation returned a result. Check
`native_status` and `result.ok` to determine whether the native benchmark
succeeded.

Interval observations follow the [canonical result semantics](results.md):
flow direction, sender/receiver observation, aggregate/stream scope, and
local stream identity remain separate. Missing values and ambiguous association
stay unknown. A native socket identifier alone cannot identify a flow across
both reporting endpoints.
TCP provenance pointers in an interval payload refer to its local normalized
fragment; interval indices can differ from those in the final multi-interval
result. Compare measured values and associations, not pointer strings alone.

Envelopes and payload records have frozen attributes. Copied JSON evidence is
detached from native memory, other deliveries, and the final result; nested
JSON containers are ordinary Python values. Persist final evidence with the
existing [result artifact API](artifacts.md). Live events are runtime progress
objects, not a new persisted report schema.

## Slow consumers and capture quality

The native callback copies a bounded payload and records arrival metadata.
JSON parsing, result assembly, framing, and application consumption occur
outside that callback. Queues admit bounded item counts and byte totals;
excess progress is dropped and counted rather than blocking native execution
until an application catches up. Terminal bookkeeping has reserved capacity.

| Stage | Limit |
| --- | --- |
| One copied native payload or full-result getter | 8 MiB |
| Pending native copies | 256 items and 16 MiB |
| Combined serialized retained native evidence | 12 MiB |
| Worker and parent progress queues, each | 256 events and 8 MiB |
| One transported progress event | 1 MiB |
| Async consumer queue | 128 events and 2 MiB |

The async bridge coalesces loop wakeups, so a slow event loop does not accumulate
one scheduled callback per native event. Capture diagnostics retain at most
eight samples of 256 characters. These are separate limits: an 8 MiB complete
document can supply the final result while being too large for live delivery.

Native JSON parsing rejects nonfinite values and duplicate fields, with one
compatibility exception in libiperf 3.19.1, 3.21 and 3.22: a native start
record can repeat `target_bitrate` exactly twice with the same nonnegative
integer value. Capture retains that value once. Conflicting values, other
duplicate fields, and further repetitions remain invalid. Saved artifact and
worker transport decoders keep their strict duplicate-field rules.

Loss at native capture is different from loss while delivering already
captured progress. A slow consumer can miss intervals while the independent
final result remains complete. Check capture and delivery diagnostics before
treating a progress sequence as a complete history.

Native 3.21 and 3.22 supply an independent complete document when full streaming
output is available. A retained valid document can recover final evidence
despite earlier progress loss. Native 3.19.1 requires reconstruction from
retained event envelopes. Missing or malformed reconstruction input produces
an unsuccessful, incomplete result even if a native end fragment survived.
An original native failure remains a failure.

The established `iperf3_lib.native_json` extension still identifies
`reconstructed_events` and preserves their original envelopes. Separate
capture metadata records limits and data quality. Reconstruction never claims
fields absent from the original envelopes. Byte limits account for copied and
serialized evidence; they are not a bound on Python or native allocator RSS.

## Keep existing callbacks

`run(on_event=...)`, `arun(on_event=...)`, and the server callback methods
continue to deliver the four-field `NativeEvent`. Its `received_at_seconds`
retains its wall-clock meaning, and sequence gaps still expose delivery loss.
Typed wrapper state, gap, and terminal events do not enter that legacy callback
or alter archived plan-event fields. See
[callback delivery](native-controls.md#observe-events-and-bound-a-run) for
callback-error and active-callback cleanup behavior.
