# Python API reference

This reference describes the development API. See
[installation and release status](../getting-started.md) before using an
example with the published package.

## Clients and servers

| API | Return value | Behavior |
| --- | --- | --- |
| `Client(cfg: ClientConfig, *, rate_intent=None)` | `Client` | Retains configuration and optional `RateIntent`; admission snapshots and resolves them. |
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

## Rate intent and capability reports

Import rate APIs from `iperf3_lib.intent` and capability APIs from
`iperf3_lib.capabilities`. All configuration integers remain strict; parsing
text is an explicit separate operation. See [rate intent and capabilities](../guides/configuration-intent.md)
for allocation, unit grammar, admission limits, profiles, and native-option decisions.

| API | Return value | Contract |
| --- | --- | --- |
| `RateIntent(per_stream_bps=None, aggregate_bps_per_direction=None)` | `RateIntent` | Exactly one strict integer intent; frozen dataclass. |
| `resolve_rate(config, intent=None)` | `ResolvedRate` | Uniform floor allocation per direction; preserves unused remainder and unlimited unknowns. Does not mutate config. |
| `parse_rate(text)` | `int` | Exact decimal SI bits/s or bytes/s conversion with explicit units. |
| `estimate_plan(configs, intents=None, *, max_payload_bytes=None, max_active_seconds=None)` | `PlanEstimate` | Finite sequential admission estimates including warm-up and both directions; raises on exceeded/unknown bounded costs. |
| `get_capabilities(*, probe_native=True, result=None)` | `CapabilityReport` | Separate wrapper/ABI/native/qualification evidence and optional supplied execution outcome. Offline mode never loads native code. |

`ResolvedRate` fields are `native_per_stream_bps`,
`aggregate_bps_per_direction`, `aggregate_bps_all_directions`,
`unused_bps_per_direction`, `active_directions`, and `source`. `to_dict()`
returns detached JSON-safe metadata. `PlanEstimate` contains a tuple of `runs`,
total `active_seconds`, `estimated_payload_bits`, and
`estimated_payload_bytes`; each `RunEstimate` has `rate`, `active_seconds`,
and `estimated_payload_bits`. Neither type reports observed traffic.

`CapabilityReport` contains `library`, `features`, `execution`, current host
platform/Python, and explicit tested platform/Python/native-version tuples.
The nested frozen dataclasses are `LibraryCapability`, `SymbolCapability`,
`FeatureCapability`, and `ExecutionEvidence`. Use `dataclasses.asdict` when an
application needs a serializable snapshot; this is not a versioned result artifact.

## Result dataclasses

Import `Result`, `FlowStats`, `SumStats`, `IntervalStats`, and `Diagnostic`
from `iperf3_lib`. `EndStats` and `StreamStats` are available from
`iperf3_lib.result`.
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
| `streams: list[StreamStats]` | Terminal per-stream observations, including mixed UDP summaries marked unattributed. |
| `execution: ExecutionMetadata \| None` | Status, methodology, timing, configuration evidence, and producer environment. |
| `availability: dict[str, FieldAvailability]` | Explicit uncertainty or absence keyed by normalized JSON pointer. |
| `extensions: dict[str, JSONValue]` | Namespaced application metadata preserved by artifacts. |
| `diagnostics: list[Diagnostic]` | Missing-data, incomplete-output, native-error, and ambiguous-mapping diagnostics. |
| `started_at_seconds: float \| None` | Native start Unix timestamp. |
| `duration_seconds: float \| None` | Duration from native test configuration. |
| `completed_at_seconds: float \| None` | Observed completion Unix timestamp; unknown for directly parsed native JSON. |

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
| `bytes` | `None` | Native measured byte count. |
| `duration_seconds` | `None` | Measured summary duration in seconds. |
| `start_seconds`, `end_seconds` | `None` | Native elapsed boundaries in seconds. |
| `packets`, `lost_packets` | `None` | Native packet counts. |
| `omitted` | `None` | Whether native output marks this observation as warm-up. |

`EndStats(sum_sent=None, sum_received=None)` holds two optional `SumStats`
objects. Both are observations of the primary flow, not separate traffic
directions.

### IntervalStats and Diagnostic

`IntervalStats(start_seconds, end_seconds, bits_per_second=None,
direction="unknown", observation=None, stream_id=None)` uses
optional elapsed interval boundaries in seconds and an optional bitrate in bits/s.
`stream_id` is the native socket identifier when available.
Additional fields are `scope`, `bytes`, `duration_seconds`, `omitted`,
`packets`, `lost_packets`, `retransmits`, `lost_percent`, and `jitter_ms`.
Scope comes from the native container; all optional measurements retain `None`
when absent. `StreamStats` groups terminal `sender`, `receiver`, or `unattributed`
summaries by direction and optional stream identifier.

`Diagnostic(message, severity="info", code="unspecified", path=None,
evidence_paths=[])` stores a message, severity (`"info"`, `"warning"`, or
`"error"`), stable machine-readable code, and JSON pointer evidence.

### Execution provenance

Import these dataclasses from `iperf3_lib.result`:

- `ExecutionMetadata`: `status` (`completed`, `failed`, or `incomplete`),
  `method`, `timing`, `configuration`, native version/system information,
  Python version, and platform.
- `RunTiming`: observed UTC start/completion, monotonic elapsed time, native
  start, requested duration, and separately named estimated completion.
- `ConfigurationSnapshot`: optional `requested` values and `effective` settings.
- `VerifiedSetting`: value, verification state, and native evidence paths.
- `FieldAvailability`: absent, unsupported, malformed, or unknown state and
  evidence paths. Absence alone does not establish unsupported behavior.

See [portable artifacts](../guides/artifacts.md) for field interpretation and
strict interchange validation. Direct dataclass construction remains permissive;
the artifact encoder validates constructed and mutated instances.

## Versioned result artifacts

Import from `iperf3_lib.artifacts`:

```text
artifact_from_result(result) -> ResultArtifact
artifact_to_dict(artifact) -> dict
artifact_from_dict(value) -> ResultArtifact
dumps_artifact(artifact, *, indent=None) -> str
loads_artifact(text: str | bytes) -> ResultArtifact
artifact_from_legacy_dict(value) -> ResultArtifact
```

`ResultArtifact` contains schema version, kind, `ArtifactProducer`, normalized
result, and extension metadata. `ArtifactValidationError` is a `ValueError`;
`UnsupportedArtifactVersion` identifies unknown versions. Decoding does not run
a benchmark, load libiperf, or reinterpret `raw` using the current native parser.

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
Accessing `HAS_BIDIR`, `HAS_JSON_OUTPUT`, `HAS_JSON_CALLBACK`,
`HAS_PROTOCOL_SELECTION`, or `HAS_BIND_ADDRESS` performs a lazy symbol probe.
`HAS_MPTCP` and `HAS_JSON_STREAM` are always `False` for this backend.

These flags do not prove operating system support or a successful native run.
Importing the module does not load libiperf. `get_capabilities(probe_native=False)`
provides an offline report; an explicit native probe distinguishes library and
symbol availability from wrapper support and qualification.

