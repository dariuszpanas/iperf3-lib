# Python API reference

This reference describes the development API. See
[installation and release status](../getting-started.md) before using an
example with the published package.

## Clients and servers

| API | Return value | Behavior |
| --- | --- | --- |
| `Client(cfg: ClientConfig)` | `Client` | Retains the supplied configuration. |
| `Client.run()` | `Result` | Performs one blocking native client run. |
| `await Client.arun()` | `Result` | Executes `run()` in an executor thread. |
| `Server(port=5201, bind_host=None)` | `Server` | Creates the Python server wrapper; native allocation happens when serving. |
| `Server.run_once()` | `None` | Blocks for one native server test. |
| `await Server.aserve_once()` | `None` | Executes `run_once()` in an executor thread. |
| `Server.serve_forever()` | `None` | Reuses a native test across sequential iterations until stopped. |
| `Server.stop()` | `None` | Signals the serving loop to stop between iterations. |

Import `Client`, `ClientConfig`, `Protocol`, and `Server` from `iperf3_lib`.
Read [running tests](../guides/running-tests.md) for error handling, async
cancellation, and process-isolation limits.

## Result dataclasses

Import `Result`, `FlowStats`, `SumStats`, `IntervalStats`, and `Diagnostic`
from `iperf3_lib`. `EndStats` is available from `iperf3_lib.result`.
These are ordinary dataclasses; manually constructing a result does not
perform native-JSON validation.

### Result

| Attribute | Meaning |
| --- | --- |
| `ok: bool` | Client execution status. Required when constructing a result. |
| `error: str \| None` | Failure message, when available. |
| `raw: dict[str, Any]` | Original native JSON, or an empty dictionary when no JSON was returned. |
| `end: EndStats \| None` | Compatibility view of primary native end summaries. |
| `protocol: str \| None` | Native protocol string normalized to lowercase. |
| `bidirectional: bool` | Native simultaneous-bidirectional flag. |
| `reporting_role: str \| None` | Reporting endpoint (`"client"` or `"server"`) when established. |
| `flows: list[FlowStats]` | Directional flows with sender/receiver observations. |
| `intervals: list[IntervalStats]` | Aggregate and per-stream interval observations. |
| `diagnostics: list[Diagnostic]` | Missing-data, incomplete-output, native-error, and ambiguous-mapping diagnostics. |
| `started_at_seconds: float \| None` | Native start Unix timestamp. |
| `duration_seconds: float \| None` | Duration from native test configuration. |
| `completed_at_seconds: float \| None` | Live completion Unix timestamp, or an estimate for directly parsed JSON. |

`to_dict()` returns a recursive dataclass dictionary. `summary_mbps` is a
read-only convenience property in decimal megabits per second. Consult the
[results guide](../guides/results.md) for their interpretation and limits.

### FlowStats and SumStats

`FlowStats(direction, sender=None, receiver=None)` identifies a traffic
direction and optional `SumStats` objects for each observation point.

`SumStats` has these attributes:

| Attribute | Default | Units |
| --- | --- | --- |
| `bits_per_second` | `None` | Optional bits/s; a measured zero remains zero. |
| `retransmits` | `None` | Native retransmission count. |
| `lost_percent` | `None` | Percentage; `1.0` means one percent. |
| `jitter_ms` | `None` | Milliseconds. |
| `direction` | `None` | Direction metadata, consistent with the parent flow. |
| `observation` | `None` | `"sender"` or `"receiver"` metadata. |

`EndStats(sum_sent=None, sum_received=None)` holds two optional `SumStats`
objects. Both are observations of the primary flow, not separate traffic
directions.

### IntervalStats and Diagnostic

`IntervalStats(start_seconds, end_seconds, bits_per_second=None,
direction="unknown", observation=None, stream_id=None)` uses
optional elapsed interval boundaries in seconds and an optional bitrate in bits/s.
`stream_id` is the native socket identifier when available.

`Diagnostic(message, severity="info")` stores a message and severity
(`"info"`, `"warning"`, or `"error"`).

### Native JSON normalization

`iperf3_lib.result.result_from_iperf_json(raw, *, reporting_role=None)` normalizes a native JSON
dictionary without running a benchmark. Invalid shapes or numeric values raise
`ValueError`. Native error documents and incomplete output with no numeric
end-of-test endpoint evidence return `ok=False`, preserving the raw data and
diagnostics. Valid partial measurements remain available; successful parsing
does not certify that every requested measurement was reported.

`reporting_role` accepts `"client"`, `"server"`, or `None`. `Client.run()`
provides `"client"`. Saved JSON can establish its role through native
`start.connecting_to` or `start.accepted_connection` markers; a generic
`start.connected` list alone does not establish a role. Contradictory evidence
produces an unknown role and a diagnostic. Bidirectional stream direction
requires both reporting role and local sender evidence; aggregate summary
keys remain relative to the client on either reporting endpoint.

Direction parsing recognizes native `start.test_start.bidir` and the earlier
`bidirectional` spelling. Both must agree when present together. Direction
flags accept booleans or the integers zero and one; malformed or conflicting
flags raise `ValueError`.

## Exporters

Import both functions from `iperf3_lib.exporters.prometheus`:

```text
render_text(result, labels=None, *, last_success_timestamp_seconds=None) -> str
write_textfile(path, result, labels=None, *, last_success_timestamp_seconds=None) -> None
```

`labels` is an optional mapping of strings to strings. `path` accepts a
string or path-like object. See [Prometheus snapshots](../guides/prometheus.md)
for metric units, freshness, label validation, and collector integration.
The [results guide](../guides/results.md) explains direction and missing-data
semantics.

## Exceptions and capabilities

`IperfError`, `IperfLibraryError`, and `UnsupportedFeatureError` are independent
`RuntimeError` subclasses exported from the package root. Catching
`IperfError` does not catch the other two.

`iperf3_lib.capabilities.has_symbol(name)` reports whether the loaded CFFI
interface exposes a symbol, returning `False` on loading/detection failures.
Importing this module computes `HAS_BIDIR`, `HAS_JSON_OUTPUT`,
`HAS_JSON_CALLBACK`, `HAS_PROTOCOL_SELECTION`, and `HAS_BIND_ADDRESS`.
`HAS_MPTCP` and `HAS_JSON_STREAM` are always `False` for this backend.

These flags are an import-time symbol snapshot. They do not prove operating
system support or a successful native run. Importing the capabilities module
can trigger a native load attempt even though importing `iperf3_lib` alone
does not.

