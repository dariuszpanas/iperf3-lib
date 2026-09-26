# Migrating from 0.2.0 to dataclasses

!!! note "Preparing for 0.3.0"
    These changes are on the development branch and remain unreleased.
    Published 0.2.0 uses Pydantic. See [installation](../getting-started.md)
    to try a reviewed source revision before upgrading an application.

The configuration and result models now use standard-library dataclasses.
`Client`, `ClientConfig`, `Protocol`, `Result`, and `Server` retain their
existing entry points. Pydantic's model methods, coercion, and validation
exceptions are no longer part of those objects.

CFFI is the sole direct Python runtime dependency. Its own dependencies and
the native libiperf requirement remain. This change does not expand the
[tested platform matrix](../reference/compatibility.md) or claim faster
benchmark execution.

## Replace model operations deliberately

| 0.2.0 operation | Development replacement |
| --- | --- |
| `ClientConfig.model_validate(mapping)` | `ClientConfig(**mapping)` after application input parsing. |
| `ClientConfig.model_validate_json(text)` | `ClientConfig(**json.loads(text))` for a configuration JSON object. |
| `config.model_dump()` | `dataclasses.asdict(config)` for a detached Python mapping. |
| `config.model_copy(update={...})` | `dataclasses.replace(config, ...)`; construction validates the new values. |
| `result.model_dump()` | `result.to_dict()` for an unversioned application snapshot. |
| `result.model_dump_json()` for archives | `dumps_artifact(artifact_from_result(result))`; this intentionally uses a new versioned format. |
| `Result.model_validate(...)` | Choose the parser for the input format below; there is no generic recursive dataclass replacement. |
| Catch `pydantic.ValidationError` for configuration | Catch `TypeError` or `ValueError`. |
| Pydantic schema/introspection helpers and dump options | No equivalent model framework is supplied; keep application-owned adapters where needed. |

`dataclasses.asdict()` does not validate an object or promise JSON scalar
conversion. For configuration JSON, convert an IP address object explicitly:

```python
import json
from dataclasses import asdict, replace

from iperf3_lib import ClientConfig, Protocol

config = ClientConfig(server="127.0.0.1", protocol=Protocol.UDP, duration=3)
reverse = replace(config, reverse=True)

config_data = asdict(reverse)
config_data["server"] = str(reverse.server)
config_data["protocol"] = reverse.protocol.value
config_json = json.dumps(config_data, allow_nan=False)
restored_config = ClientConfig(**json.loads(config_json))
```

The result, statistics, and configuration dataclasses remain mutable. A
dataclass constructor does not recursively turn nested dictionaries into
models. In particular, `Result(**saved_snapshot)` can leave dictionaries
where statistics objects are expected; it is not a snapshot decoder.

## Configuration is stricter at the boundary

`ClientConfig` checks types, ranges, and option combinations explicitly:

- Integer fields require integers. Numeric strings, floats, and booleans are
  rejected, even when a conversion might appear lossless.
- Boolean options require `True` or `False`, not `"true"`, `"false"`, `0`, or `1`.
- Exact protocol strings `"tcp"`, `"udp"`, and `"sctp"` normalize to `Protocol`.
- Hostname/address strings and standard-library IP address objects remain
  accepted. An unknown constructor option raises `TypeError`.
- Bounds, conflicting reverse/bidirectional modes, and UDP block-size rules
  remain enforced. Invalid types raise `TypeError`; invalid values or
  combinations raise `ValueError`.

Parse environment variables, command-line strings, or application forms into
the intended types before constructing a configuration. Avoid generic
`bool(text)` conversion: a nonempty `"false"` string would become true.

Direct attribute assignment does not rerun validation. Prefer a fresh instance
or `replace(config, ...)`. Each `Client.run()` revalidates and executes a
detached configuration snapshot, so later caller mutations cannot alter that
admitted run. The result retains the admitted request separately from native
settings verified through returned evidence.

The low-level `ClientConfig.rate` remains bits/s **per stream**. Higher-level
[`RateIntent`, unit parsing, and admission estimates](configuration-intent.md)
are separate APIs. Native feature rejection still raises
`UnsupportedFeatureError` when applied; MPTCP and streaming remain unsupported.
See the [configuration reference](../reference/configuration.md) for exact
bounds and protocol defaults.

## Identify the data format before loading it

The published [0.2.0 result models](https://github.com/dariuszpanas/iperf3-lib/blob/v0.2.0/src/iperf3_lib/result.py)
contained `ok`, `error`, `raw`, and `end`. Normalized flows, timing metadata,
`to_dict()`, and the artifact API were added during development after that
release. These formats have different contracts:

| Input | Meaning | Loading path |
| --- | --- | --- |
| Native iperf JSON, usually containing `start`/`intervals`/`end` or `error` | Native measurements and metadata | `result_from_iperf_json(json.loads(text))` |
| 0.2.0 Pydantic `Result.model_dump()` / `model_dump_json()` | A wrapper snapshot containing native JSON under `raw` | Explicit migration with the original status retained; see below. |
| The documented historical development `Result.to_dict()` snapshot | An unversioned normalized snapshot | `artifact_from_legacy_dict()` for that specific legacy field set. |
| A versioned envelope with `kind="iperf3-lib.result"` and `schema_version=1` | Durable normalized result plus producer identity | `loads_artifact()` or `artifact_from_dict()` |

Do not detect a format by filename alone. A Pydantic dump or artifact is not
a native iperf document. The native parser does not decode those envelopes.
Similarly, an artifact reader does not silently guess how to upgrade an
unversioned dictionary.

### Native JSON

```python
import json
from pathlib import Path

from iperf3_lib.result import result_from_iperf_json

native = json.loads(Path("iperf-native.json").read_text(encoding="utf-8"))
result = result_from_iperf_json(native, reporting_role="client")
```

Pass `reporting_role` only when the producer is known. Otherwise omit it and
retain any resulting unknown direction/observer diagnostics. Re-normalizing
native JSON intentionally applies the current parser's interpretation. Loading
a versioned artifact instead preserves its writer's normalized record.

### Published 0.2.0 Pydantic dumps

Keep the original archive. The explicit example below accepts a full,
unmodified 0.2.0 dump with nonempty native `raw` data. It preserves the entire
old snapshot in an artifact extension and refuses a change to `ok` or `error`.
It never turns an original failure into a success silently.

```python
from copy import deepcopy

from iperf3_lib.artifacts import artifact_from_result
from iperf3_lib.result import result_from_iperf_json


def migrate_020_dump(snapshot, *, reporting_role=None):
    original = deepcopy(snapshot)
    if not isinstance(original, dict) or set(original) != {"ok", "error", "raw", "end"}:
        raise ValueError("Review customized or unknown snapshot formats explicitly")
    if type(original["ok"]) is not bool:
        raise ValueError("The original execution status must be a boolean")
    if original["error"] is not None and not isinstance(original["error"], str):
        raise ValueError("The original error must be a string or null")
    if not isinstance(original["raw"], dict) or not original["raw"]:
        raise ValueError("No native document: retain the original and review manually")

    parsed = result_from_iperf_json(original["raw"], reporting_role=reporting_role)
    if parsed.ok != original["ok"] or parsed.error != original["error"]:
        raise ValueError("Status differs: retain both records and review the migration")

    artifact = artifact_from_result(parsed)
    artifact.extensions["example.migration_0_2"] = {
        "source_format": "iperf3-lib 0.2.0 Pydantic Result dump",
        "original_snapshot": original,
    }
    return artifact
```

This is an application migration recipe, not a new package API or a universal
Pydantic decoder. Customize the extension namespace for your application.
Serialize the returned artifact with `dumps_artifact()` to validate its complete
JSON content, including the preserved snapshot. Its producer describes the
current artifact writer; the extension records the older source format.

Some 0.2.0 failures have no native JSON. Customized dumps may omit fields, and
old successful snapshots may disagree with stricter current interpretation.
These need a reviewed migration policy. Retain the original outcome and error;
do not infer successful execution from partial throughput or missing metadata.
Preserve any separately reviewed normalized record alongside the original
rather than overwriting the old file. No measurements or completion times can
be recovered when the source never recorded them.

### Historical development snapshots

`artifact_from_legacy_dict()` targets the documented unversioned development
shape that preceded artifacts. It rejects unfamiliar fields and records
uncertainty, including unknown interval scope and unobserved completion.
It is not a blanket adapter for Pydantic models or arbitrary dictionaries.
The richer current `to_dict()` output also includes fields outside that older
adapter's accepted set. For a current in-memory `Result`, use
`artifact_from_result()` directly. See [artifact migration](artifacts.md#migrating-unversioned-snapshots).

### New durable archives

```python
from pathlib import Path

from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact

artifact = artifact_from_result(result)
Path("benchmark-v1.json").write_text(dumps_artifact(artifact, indent=2), encoding="utf-8")
loaded = loads_artifact(Path("benchmark-v1.json").read_text(encoding="utf-8"))
restored_result = loaded.result
```

The artifact functions validate types and cross-field consistency, retain
producer/raw/extension evidence, and reject unsupported schema versions or
unknown canonical fields. `ArtifactValidationError` is a `ValueError` with a
data path; `UnsupportedArtifactVersion` identifies an unsupported schema.
Reading an archive does not load libiperf, run a benchmark, or replace recorded
environment metadata with the importing machine's values. Consult the
[artifact contract](artifacts.md) before committing to a storage format.

## Missing data, zero, and direction

`SumStats.bits_per_second` is now optional: absent native throughput is `None`.
Measured zero stays zero. Check `is not None` before arithmetic instead of
using truthiness or treating every missing value as zero.

```python
flow = next((item for item in result.flows if item.direction == "client_to_server"), None)
received = flow.receiver if flow is not None else None
if not result.ok:
    print("Execution did not succeed:", result.error)
elif received is None or received.bits_per_second is None:
    print("Receiver throughput unavailable")
else:
    print(received.bits_per_second / 1_000_000, "Mbps")
```

`summary_mbps` remains a compatibility convenience with a `0.0` fallback when
no throughput exists. It now selects the first available value, **including
zero**, rather than skipping zero to use another endpoint. The fallback
cannot distinguish an unavailable measurement from measured zero.

`end.sum_sent` and `end.sum_received` describe the primary flow's sender and
receiver observations. They are not two independent traffic directions and
must not be added together. Prefer explicit `flows`, `streams`, and interval
scope for richer analysis; do not combine aggregate intervals with their
component streams. Native errors and absent measured summaries produce
unsuccessful normalized results, with partial evidence and diagnostics retained.
See [reading results](results.md) and [analysis](analysis.md).

## Observed time and inferred time

Timing fields did not exist in published 0.2.0. Current execution metadata
distinguishes:

- Wrapper-observed Unix start/completion events.
- Monotonic elapsed operation time.
- Native-reported start and requested duration.
- A separately labeled inferred completion estimate, where available.

Native saved JSON cannot establish a wrapper-observed completion event from
start plus requested duration. Its `completed_at_seconds` remains `None`;
an estimate stays in `execution.timing.estimated_completed_at_seconds` and
does not create Prometheus completion/freshness samples. Live runs record
observed timing independently. Unix timestamps can move backward after a
clock adjustment; use monotonic elapsed time for operation duration.

The legacy adapter's demotion of an old completion value to an estimate
applies to **unreleased development snapshots**, not to published 0.2.0,
which had no completion field. See [execution evidence](artifacts.md#execution-and-evidence)
for the individual timing and configuration fields.

## Keep application integration explicit

Applications may keep Pydantic at their own boundary, convert values, and
then construct these dataclasses. The library does not require that adapter
or reproduce Pydantic's model framework. Validate any manually assembled
result through the artifact writer before durable storage.

Async convenience still uses executor threads, cancellation still leaves an
active native call running, and same-process native concurrency remains
unsupported. Dataclass migration changes none of those guarantees. Future
execution proposals remain in the [advanced design](../design/advanced-execution.md).
