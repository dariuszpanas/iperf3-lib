# Working with results

The normalized dataclass result model was introduced in 0.3.0.
See the [artifact guide](artifacts.md) for the versioned storage contract.

`Result` contains parsed native JSON in `raw`, end-of-test summaries,
normalized flows and intervals, and timing metadata. Normalization currently
covers a subset of native output; preserve `raw` when you need measurements
outside that subset.

Ordinary JSON capture preserves the parsed native document. Streaming capture
requests full final JSON on libiperf 3.21 and 3.22. On 3.19.1, the worker reconstructs
`raw` from native event envelopes and records
`result.extensions["iperf3_lib.native_json"]` with
`representation="reconstructed_events"` and the original `events` list.
The diagnostic `execution.reconstructed_json` identifies this case. Fields not
emitted in the event stream cannot be claimed as original document content;
inspect the capture evidence before depending on top-level native metadata.

Live delivery can drop events from its bounded queues without changing the
separately retained capture. Those delivery bounds are not a bound on retained
interval/result memory. See [event handling](native-controls.md#observe-events-and-bound-a-run).

## Keep direction and observation separate

A flow's `direction` describes where traffic travels:

- `client_to_server`
- `server_to_client`

Its `sender` and `receiver` contain the two endpoint observations of that
flow. They may differ. Adding them would count observations of the same
traffic twice.

```python
for flow in result.flows:
    for observer, stats in (("sender", flow.sender), ("receiver", flow.receiver)):
        if stats is not None:
            print(flow.direction, observer, stats.bits_per_second)
```

Flow and nested summary directions agree, including reverse runs.
Bidirectional end summaries have separate flow entries for each direction.

`result.end.sum_sent` and `result.end.sum_received` retain the compatibility
view of the primary native summaries. Use `flows` to inspect bidirectional
results rather than interpreting those two fields as opposite directions.

## Treat missing data explicitly

A missing endpoint summary is `None`. Missing bitrate, retransmissions, loss,
jitter, and interval boundaries are also `None`. A measured zero stays zero.
The Prometheus exporter omits an unavailable measurement while retaining
other available measurements for the same endpoint.

`summary_mbps` remains a compatibility convenience: it selects the first
available summary bitrate, preferring the sender, including a measured zero.
It returns `0.0` when no rate is available. Use the optional
`SumStats.bits_per_second` field when your application must distinguish those
two cases. The property never adds sender and receiver observations or sums
independent flows.

## Read intervals

```python
for interval in result.intervals:
    if interval.bits_per_second is None:
        continue
    print(
        interval.start_seconds,
        interval.end_seconds,
        interval.direction,
        interval.observation,
        interval.stream_id,
        interval.bits_per_second,
    )
```

The list contains both native aggregate summaries and per-stream records.
Use `scope` (`"aggregate"`, `"stream"`, or `"unknown"`) to distinguish them.
Per-stream entries use the native socket identifier when present; a missing
identifier does not make a record an aggregate.
Do not add aggregate and per-stream records together.

Bidirectional per-stream mapping uses reporting-endpoint and native role
evidence. Unproven direction is `"unknown"` and produces a diagnostic; it is
never silently assigned to the first flow. A missing observation point is
`None`. Byte counts, measured duration, and omitted warm-up flags are retained
when available. An absent omission flag remains `None`, not `False`.

`result.streams` contains terminal per-stream observations. Native UDP stream
summaries combine sender throughput with receiver loss/jitter, so those mixed
objects are retained as `unattributed`, with a diagnostic. Use attributed
flow summaries for endpoint comparisons and keep the original evidence in `raw`.

`Client.run()` supplies its known client role. When importing saved native
JSON, `result_from_iperf_json(raw, reporting_role="server")` can supply the
reporting endpoint explicitly. The parser also recognizes native
`start.connecting_to` and `start.accepted_connection` markers. It records the
resolved value in `result.reporting_role`; contradictory role evidence is
diagnosed and left unknown. The interval's local `sender` flag, or compatible
end-of-test evidence for the same socket, determines its direction relative
to that role. It does not depend on the order of stream records.

The [analysis module](analysis.md) calculates duration-weighted stability and
interval-average throughput quantiles with explicit selectors, warm-up policy,
and coverage evidence. These describe interval-average rates, not packet
latency or packet-throughput percentiles. Review its data-quality results
before using a calculation for a performance decision.

## Preserve results

```python
from pathlib import Path
from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact

Path("result.json").write_text(
    dumps_artifact(artifact_from_result(result), indent=2),
    encoding="utf-8",
)
restored = loads_artifact(Path("result.json").read_text(encoding="utf-8")).result
```

The [versioned artifact](artifacts.md) preserves the normalized model, native
JSON, provenance, diagnostics, and producer version without loading libiperf.
`to_dict()` remains an unversioned `dataclasses.asdict()` snapshot for Python
callers. Existing snapshots have an explicit legacy import path.

`started_at_seconds` comes from the native timestamp and `duration_seconds`
from native test configuration. A live client records `completed_at_seconds`
after the call completes. Direct use of `result_from_iperf_json()` leaves
completion unknown. Start plus requested duration is retained separately as
`execution.timing.estimated_completed_at_seconds` and never used for exporter
freshness. Live runs also record monotonic elapsed time, independently of UTC.

`diagnostics` records incomplete output, missing measurements, and ambiguous
direction or observation evidence. A saved native `error` document has
`ok=False`, retains its message and raw data, and cannot export its partial
measurements as a successful run. Output without any numeric end-of-test
endpoint evidence is also incomplete and has `ok=False`. A partial summary
with some valid measurements remains usable, with diagnostics for gaps.

Invalid object shapes, malformed flags, nonnumeric measurements, and
non-finite values raise `ValueError`; they do not become zero measurements.
`execution.configuration` separates the admitted request snapshot from native
settings verified through returned JSON or retained native getter receipts.
A getter confirms the stored native setting; it does not prove the kernel's
applied buffer size, actual pacing rate or device behavior. Unavailable
verification stays explicit; setting a native option alone is not proof of its
effective value. Structured diagnostics have stable codes and evidence paths.
See [TCP/CPU analysis](analysis.md#tcp-and-endpoint-cpu-evidence) and the
[advanced comparison rules](analysis.md#advanced-configuration-in-comparisons)
before comparing results with additional controls.

