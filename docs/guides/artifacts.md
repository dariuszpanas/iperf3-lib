# Portable result artifacts

Artifact schema v1 was first published with iperf3-lib 0.3.0. Schema versions
are independent of package versions. Incompatible changes to canonical fields,
units or interpretation require a new schema version. Historical unreleased
development snapshots are separate from this published contract.

Use a versioned artifact when saving a benchmark for later analysis or sharing
it with another application. `Result.to_dict()` remains an unversioned
dataclass snapshot; it does not acquire a durable format implicitly.

## Save and read a result

```python
from pathlib import Path

from iperf3_lib.artifacts import (
    artifact_from_result,
    dumps_artifact,
    loads_artifact,
)

artifact = artifact_from_result(result)
Path("benchmark.json").write_text(dumps_artifact(artifact, indent=2), encoding="utf-8")

loaded = loads_artifact(Path("benchmark.json").read_text(encoding="utf-8"))
assert loaded.result.raw == result.raw
print(loaded.producer.version, loaded.result.execution.status)
```

These conversion functions do not run a benchmark, load libiperf, probe the
current machine, or reparse the original native JSON. Reading an archive
preserves the normalized measurements and the producer recorded by its writer.
File handling belongs to the application.

The envelope contains `kind="iperf3-lib.result"`, `schema_version=1`,
`producer`, `result`, and `extensions`. The writer emits every canonical field,
including explicit nulls and empty collections. Both `artifact_to_dict()` and
`artifact_from_dict()` support applications that already handle JSON transport.

## Execution and evidence

`result.execution.status` separates `completed`, `failed`, and `incomplete`
execution. `result.ok` is true only for completed execution. A completed run
can still lack individual measurements; diagnostics describe those gaps.
Performance acceptance is a separate application decision.

`execution.configuration.requested` records the admitted, resolved `ClientConfig`.
When rate intent or protocol defaults derive a native rate, the original caller
config, intent, and allocation remain in the `iperf3_lib.rate_intent` result
extension. See [rate resolution](configuration-intent.md).
Each run validates and executes a detached snapshot. Mutating the caller's
configuration during execution cannot alter that snapshot. `effective`
contains independently returned native settings, each with a `state`, `value`,
and `evidence_paths` pointing to native JSON. A successful setter call alone
does not establish an effective value. Unavailable and unsupported settings
remain explicitly unavailable; they are never copied from the request and
called verified.

Timing fields distinguish:

| Field in `execution.timing` | Meaning |
| --- | --- |
| `started_at_seconds`, `completed_at_seconds` | Wrapper-observed Unix events. |
| `elapsed_seconds` | Operation duration measured with a monotonic clock. |
| `native_started_at_seconds` | Timestamp reported by native JSON. |
| `requested_duration_seconds` | Configured test duration, not observed elapsed time. |
| `estimated_completed_at_seconds` | Explicitly inferred start-plus-duration estimate, when available. |

An imported native document cannot establish actual completion from requested
duration. Its estimate never becomes a Prometheus completion or freshness
metric. Native version/system information and live Python/platform metadata
describe the execution environment; an importer does not replace them with
its own environment.

For compatibility, `Result.duration_seconds` retains the duration reported in
native test configuration. A live wrapper's `timing.requested_duration_seconds`
records its admitted request. If the native setting differs, both values and
a configuration diagnostic are preserved; neither is observed elapsed time.

## Measurements and aggregation

Flows retain separate sender and receiver observations. Stream summaries
retain local socket identities; these identifiers do not identify the same
connection across different runs or reporting endpoints.

Intervals explicitly declare `scope` as `aggregate`, `stream`, or `unknown`.
Scope comes from the native container, even if a stream has no socket ID.
Bytes, native elapsed seconds, boundaries, bitrate, packet counts, and
`omitted` warm-up state remain separate optional fields. Never add aggregate
and component-stream measurements together, or add both endpoint observations
of the same transfer.

Native UDP per-stream end summaries can mix sender throughput with receiver
loss/jitter evidence. Such records are retained as `StreamStats.unattributed`
with a diagnostic, rather than falsely assigning the whole object to one
endpoint. Attributed aggregate summaries and interval observations remain
available for calculations.

Field units are fixed by the schema: bytes and counts are integers, duration
and timing fields use seconds, bitrate uses bits/s, jitter uses milliseconds,
and `lost_percent` uses percent. The exporter converts to its documented base
units. Missing fields are null; measured zeros stay zero. `availability`
records explicit absent, unsupported, malformed, or unknown states using
canonical JSON Pointer paths. Missing native fields alone do not prove that a
feature is unsupported.

On the qualified libiperf 3.19.1, 3.21 and 3.22 producers, SCTP retransmission
fields are unavailable: native output can contain values without retransmission
measurement support, including nonnegative values. The normalized field is null
with unsupported-state evidence; the original value remains in `raw`. For other
or unidentified SCTP producers, measurement support stays unknown and the
normalized field remains null until that producer is qualified.

Diagnostics carry stable `code`, `severity`, optional canonical `path`, and
native/canonical `evidence_paths`. Captured native data stays in `raw`, including
partial and failed-run evidence. Ordinary JSON capture preserves the parsed
native document. Streaming on native 3.21 and 3.22 requests full final JSON;
3.19.1 instead records an explicit reconstruction from event envelopes. In that case,
`extensions["iperf3_lib.native_json"]` retains
`{"representation": "reconstructed_events", "events": [...]}`, and the
`execution.reconstructed_json` diagnostic points to
`/extensions/iperf3_lib.native_json/events`. This extension is absent when a full
native document was captured. Artifact roundtrips preserve the representation
and envelopes without promoting reconstruction to a full original document.

Bounded live-callback delivery does not limit the size of retained native
intervals or artifact evidence. Failed and partial streaming captures keep their
actual evidence rather than manufacturing unreported native metadata.

## Validation and evolution

The reader and writer validate types and cross-field consistency, including
mutable manually constructed dataclasses. They reject unknown canonical
fields, unsupported schema versions, invalid enum values, duplicate JSON
keys, non-finite numbers, booleans substituted for numbers, invalid counts,
and contradictory outcomes or compatibility projections.

`ArtifactValidationError` is a `ValueError` with a path identifying the bad
data. `UnsupportedArtifactVersion` identifies a schema version the reader
cannot interpret. Upgrade the reader or use an explicit migration; do not
discard unknown fields and call the result a lossless import.

Arbitrary strict JSON remains permitted in `raw` and namespaced `extensions`.
Unknown diagnostic codes are retained as advisory information. Decoding and
encoding preserve producer metadata, raw data, and extensions semantically;
JSON whitespace and number spelling are not preserved byte for byte.

Changing canonical fields, units, or interpretation requires a
new schema version under this strict policy. Released schema readers remain
supported; migrations are explicit and preserve evidence.

## Migrating unversioned snapshots

Continue using `to_dict()` for short-lived application snapshots. To archive
an existing development snapshot, use `artifact_from_legacy_dict()` explicitly;
the normal artifact reader rejects unversioned dictionaries with a migration
hint. The adapter preserves known fields and original JSON, rejects unfamiliar
fields, and adds compatibility diagnostics for missing metadata.

A legacy missing socket ID does not prove aggregate scope. A legacy inferred
completion timestamp does not prove an observed completion event. Those
uncertainties remain explicit after migration. The Pydantic models published
in 0.2.0 and arbitrary third-party dictionaries are not silently treated as
this development snapshot format.


## TCP/CPU fields in schema v1

The published v1 contract includes optional TCP evidence on interval and stream
sender statistics, and an endpoint CPU collection on `Result`. The
[analysis guide](analysis.md#tcp-and-endpoint-cpu-evidence) specifies units,
qualification and attribution. Strict readers validate protocol/scope/endpoint
consistency and require an existing raw or namespaced receipt for every present
measurement. CPU percentages may exceed 100%; windows/counts remain nonnegative
integers and RTT remains finite nonnegative seconds.

Retained development fixtures gained only explicit `tcp: null` and `cpu: []`
fields. Hash regressions ensure all previous measurements, raw JSON, diagnostics
and producer metadata remain unchanged. A separate fixture records newly
normalized evidence. Loading earlier archives never reparses or backfills them.
