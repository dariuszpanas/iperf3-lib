# Native event observations

This is a **bounded exploration**, recorded on 2026-09-26 to inform
[live-event design #35](https://github.com/dariuszpanas/iperf3-lib/issues/35).
It does not qualify a public streaming API. The
[advanced execution design](advanced-execution.md) describes the remaining
contracts and tests.

## Method and bounds

Two existing Linux qualification images supplied libiperf 3.19.1 and 3.21.
Each container had one CPU, 256 MiB memory, and Docker networking disabled;
client and fixture server communicated through container loopback. No host
mounts were used. The Python probe was delivered through standard input.

Each endpoint ran these 11 cases sequentially:

1. TCP, UDP, and SCTP, each in forward, reverse, and simultaneous
   bidirectional mode, with full streaming output requested where available.
2. One additional forward TCP run with full output disabled.
3. One forward TCP connection to an unused local port to observe native error
   delivery, again requesting full output where available.

Successful cases used one stream per direction, one second, and
1,000,000 bit/s per stream per direction. UDP used 1200-byte blocks.
Bidirectional cases therefore requested 2,000,000 bit/s across both
directions. These are requested pacing targets, not measured exact wire-byte
budgets. The probe did not impose impairment or simulate a slow consumer.

Each successful case started a fresh one-off CLI fixture server. Readiness
came from its flushed listening message, with a five-second readiness bound;
no extra connection consumed the one-off server. The **client called libiperf
directly through CFFI**. It applied defaults, role/endpoint/duration/streams/
rate/protocol/direction, JSON output, streaming, and an optional full-output
setter before registering the callback and running the client.

The callback copied bytes, retained monotonic arrival times, and capped each
payload at 1 MiB and each run at 64 retained callbacks. Parsing occurred after
the native test was freed. The probe retained a strong callback reference
through native cleanup and recorded its allocation/free call counts.
Fixture-server shutdown used a bounded wait, then terminate/kill escalation.
The host runner applied a 55-second subprocess timeout per endpoint; this
harness bound is not a library cancellation guarantee or an orphan-cleanup
qualification.

The [sanitized observation fixture](native-events-2026-09-26.json) contains
all 22 cases, event order, return codes, full-output capability, getter
presence, drop counts, and harness lifecycle counts. Receipt and probe hashes
identify the original evidence. Raw endpoint details and measurement payloads
are omitted. This historical summary is not an executable regression test.

## Observed results

| Native and mode | Successful cases | Callback order | Complete-result getter |
| --- | --- | --- | --- |
| 3.19.1; full-output setter unavailable | 10 | `start`, `interval`, `end` | Null |
| 3.21; full output enabled | 9 | `start`, `interval`, `end`, complete document | Document present |
| 3.21; full output disabled | 1 | `start`, `interval`, `end` | Null |

| Deliberate refused connection | Return code | Callback order | Complete-result getter |
| --- | --- | --- | --- |
| 3.19.1 | -1 | `error`, `end` | Null |
| 3.21; full output enabled | -1 | `error`, `end`, complete document | Document present |

All 20 successful cases returned zero. Both deliberate failures recorded
native error code 103. No copied payload was rejected by the probe's size or
count bounds. Each recorded case reported one allocation and one free call;
these harness counters do not establish cleanup under untested crash or
callback-failure conditions.

The extra complete document is a callback payload without the streaming
`event` envelope. Its presence is not itself a success signal: the refused
connection also produced one on 3.21. The report does not claim normalized
stream/flow reconstruction or equivalence between assembled events and the
ordinary complete-result API.

## Interpretation and limits

The first attempted minimum-version probe tried to call
`iperf_set_test_json_stream_full_output` and failed because the symbol was
absent. The completed probes recorded that capability as unavailable and
continued with ordinary streaming. A supported API cannot unconditionally
require that setter while claiming 3.19.1 compatibility.

The upstream [3.19.1 public header](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.h)
and [3.21 invocation reference](https://software.es.net/iperf/invoking.html)
are consistent with the observed capability difference. The
[3.21 event serializer](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L3085-L3110)
calls the callback before releasing its serialized buffer; consumers must
copy data before returning. The
[finish path](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L5308-L5356)
also explains the distinct end event and optional complete document.

This evidence covers one-second, low-rate loopback shapes, including native
error output. It does **not** cover sustained delivery, memory saturation,
dropped-event recovery, malformed input, consumer abandonment, user callback
exceptions, concurrency, process failure, or cancellation. No adaptive UDP
decision or impaired-link behavior was tested. Those remain explicit open
qualification work in [#34](https://github.com/dariuszpanas/iperf3-lib/issues/34),
[#35](https://github.com/dariuszpanas/iperf3-lib/issues/35), and
[#36](https://github.com/dariuszpanas/iperf3-lib/issues/36).
