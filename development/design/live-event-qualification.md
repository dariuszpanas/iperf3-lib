# Typed live-event qualification

This map defines the audit for [issue #35](https://github.com/dariuszpanas/iperf3-lib/issues/35).
It is not evidence that an arbitrary checkout passed. The
[public guide](../../docs/guides/live-events.md) defines the consumer contract;
current CI and retained artifacts must identify the exact candidate.

| Requirement | Evidence |
| --- | --- |
| Typed provenance and canonical association | `test_live_event_types.py` compares interval and summary projections with both endpoint captures across protocols, directions, and native versions. Raw, direction, observation, scope, stream identity, and measurement time remain distinct. Fragment-relative TCP pointers do not need equal final-document interval indices. |
| Copy boundary and memory accounting | `test_event_capture.py` proves bounded copying and parsing on a separate thread, strict JSON, payload/item/byte/retention limits, and bounded diagnostics. Retained reconstruction charges both native envelopes and projected raw evidence. These are serialized evidence bounds, not allocator RSS bounds. |
| Native lifetime and frame order | `test_worker_protocol.py` covers getter copying before free, callbacks alive through free, parser settlement before control frames, fresh server capture state, and parser failures. An unsettled parser cannot publish a normal terminal. Every non-null native test is freed once on ordinary paths. |
| Capture quality and final results | Tests separate malformed/copy/retention loss from later progress loss. Minimum-version reconstruction fails closed on missing input. A valid independent full document can recover evidence without erasing an observed native error. Late error/end and unknown advisory messages do not establish wrapper success. |
| Transport and projection | `test_execution_worker.py` checks identity, run order, separate legacy and typed counters, shared byte/item limits, reserved controls, malformed projection diagnostics, and oversize projection drops. Complete results require terminal, EOF, and reaping. |
| Consumer ownership | `test_event_stream.py` covers inert construction, admission snapshots, one consumer, early break, explicit close, body failure, repeated cancellation, cleanup failure, bounded queued bytes, coalesced wakeups, and reserved terminal delivery after the owner settles. |
| Compatibility | `NativeEvent` retains its exact four fields. Existing callbacks and result/assessment/sweep/plan/concurrent/adaptive report fixtures remain unchanged. New progress envelopes are a separate runtime type, with separate namespaced result metadata. |
| Actual native behavior | `test_live_events_integration.py` records both client and server for TCP/UDP/SCTP × forward/reverse/bidirectional. It also covers refused connection and client/server closure after positive interval traffic. Every case retains artifacts, event envelopes, owned exits/closed pipes, listener and admission release, and a measured subsequent TCP run. |

Actual server start JSON on both native endpoints repeats `target_bitrate`.
The native parser permits exactly two equal nonnegative integer values only
in a streamed start envelope's `/data` or a full document's `/start` object.
Captured native bytes supply regression coverage; conflicting duplicates,
other fields/paths and third repetitions are rejected. This normalization does
not relax saved artifact or IPC decoding.

## Installed evidence

`scripts/qualify_lifecycle.py` builds and seals one wheel/sdist pair once, then
qualifies both distributions outside the source checkout on Python 3.12–3.14
and libiperf 3.19.1/3.21. The live-event cases are added to the existing
cancellation, transport, plan, resource, and adaptive cases: **49 selected
cases per distribution**, with setup/call/teardown required to pass. Across
the twelve installed receipts this is 588 cases and 1764 phases. A selected
skip or missing receipt fails qualification.

Native live-event receipts include serialized endpoint artifacts, typed
envelopes, exact producer/run identity, native protocol/direction, capture
and delivery limits/counters, actual process return codes, and measured reuse.
The validator compares raw intervals and end summaries with the final document
and their typed projections with canonical measurements. It rejects
contradictory identity, timing, loss, terminal, ownership, and reuse evidence;
`test_live_event_evidence.py` exercises such corrupted receipts.

SCTP uses an explicit kernel protocol probe. Only a specific unsupported
address-family/socket/protocol error can be recorded as unsupported; permission
errors and arbitrary exceptions fail. Such a receipt does not count as
successful SCTP traffic and must be reported separately in the audit. A normal
supported matrix retains positive measurements for every protocol and direction.

Inspect the library-populated process return code before calling test-side
`poll()` or `wait()`; those calls could otherwise hide missing library reaping.
Fallback harness cleanup is for failed tests and cannot qualify a successful
ownership receipt. Forced termination establishes process-owned OS cleanup,
not execution of C finalizers. Existing
[isolated execution evidence](isolated-execution-qualification.md) retains the
broader process/resource and parent-death contract.
