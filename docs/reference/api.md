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
| `flows: list[FlowStats]` | Directional flows with sender/receiver observations. |
| `intervals: list[IntervalStats]` | Aggregate and per-stream interval observations. |
| `diagnostics: list[Diagnostic]` | Diagnostic storage; currently not populated automatically. |
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
| `bits_per_second` | `0` | Bits/s. |
| `retransmits` | `None` | Native retransmission count. |
| `lost_percent` | `None` | Percentage; `1.0` means one percent. |
| `jitter_ms` | `None` | Milliseconds. |
| `direction` | `None` | Direction metadata; use the parent flow for reverse runs. |
| `observation` | `None` | `"sender"` or `"receiver"` metadata. |

`EndStats(sum_sent=None, sum_received=None)` holds two optional `SumStats`
objects. Both are observations of the primary flow, not separate traffic
directions.

### IntervalStats and Diagnostic

`IntervalStats(start_seconds, end_seconds, bits_per_second=None,
direction="client_to_server", observation="receiver", stream_id=None)` uses
elapsed interval boundaries in seconds and an optional bitrate in bits/s.
`stream_id` is the native socket identifier when available.

`Diagnostic(message, severity="info")` stores a message and severity
(`"info"`, `"warning"`, or `"error"`).

### Native JSON normalization

`iperf3_lib.result.result_from_iperf_json(raw)` normalizes a native JSON
dictionary. Invalid shapes or numeric values raise `ValueError`. The function
does not run a benchmark and does not independently verify execution success;
its normal return currently has `ok=True`. Preserve run status separately
when importing saved native data, especially native error payloads.

## Exporters

Import both functions from `iperf3_lib.exporters.prometheus`:

```text
render_text(result, labels=None, *, last_success_timestamp_seconds=None) -> str
write_textfile(path, result, labels=None, *, last_success_timestamp_seconds=None) -> None
```

`labels` is an optional mapping of strings to strings. `path` accepts a
string or path-like object. See [Prometheus snapshots](../guides/prometheus.md)
for metric units, freshness, and the current prerelease compatibility gap.

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

