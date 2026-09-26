# Working with results

!!! note "Unreleased result model"
    The normalized dataclass result model is available on `main` after `0.2.0`.
    Its remaining correctness and schema work is tracked in the
    [roadmap](../roadmap.md).

`Result` contains the original native JSON in `raw`, end-of-test summaries,
normalized flows and intervals, and timing metadata. Normalization currently
covers a subset of native output; preserve `raw` when you need measurements
outside that subset.

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

Use `flow.direction` for direction labels. The development parser currently
leaves the nested `SumStats.direction` value inconsistent for reverse runs.
Bidirectional end summaries have separate flow entries for each direction.

`result.end.sum_sent` and `result.end.sum_received` retain the compatibility
view of the primary native summaries. Use `flows` to inspect bidirectional
results rather than interpreting those two fields as opposite directions.

## Treat missing data explicitly

A missing endpoint summary is `None`. Missing retransmissions, loss, jitter,
or interval throughput are also `None`. Their absence does not establish a
zero measurement.

There are still exceptions in the development model:

- `SumStats.bits_per_second` defaults to zero, and an existing native summary
  object without a bitrate is normalized to zero.
- Missing interval start/end values default to zero.
- `summary_mbps` returns the first nonzero summary rate, preferring the sender,
  or zero when none is available. It is not a sum across flows or streams.

Inspect `raw` if your application must distinguish an absent bitrate or
interval boundary from a measured zero. The roadmap includes strengthening
these missing-data guarantees before release.

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
Aggregate entries have `stream_id=None`; per-stream entries use the native
socket identifier when present. A missing socket identifier also produces
`None`, so it is not an unconditional guarantee that a record is an aggregate.
Do not add aggregate and per-stream records together.

Bidirectional aggregate interval keys are recognized, but per-stream interval
directions currently use the first flow's direction. Inspect native interval
records for bidirectional per-stream analysis. Omission flags, byte counts,
and other interval fields remain in `raw` rather than the normalized model.

The package does not yet calculate stability statistics or interval
percentiles. Any percentile calculated from interval rates describes interval
rates, not packet latency. Duration weighting and warm-up handling need to be
explicit in application calculations.

## Preserve results

```python
import json
from pathlib import Path

Path("result.json").write_text(
    json.dumps(result.to_dict(), indent=2),
    encoding="utf-8",
)
```

`to_dict()` uses `dataclasses.asdict()` and includes `raw`. This is a Python
dataclass snapshot; a versioned interchange schema and corresponding importer
have not been introduced. Store your package version alongside it for
long-lived archives.

`started_at_seconds` comes from the native timestamp and `duration_seconds`
from native test configuration. A live client records `completed_at_seconds`
after the call completes. Direct use of `result_from_iperf_json()` estimates
completion as start plus configured duration when both are present, rather
than measuring elapsed wall time.

`diagnostics` currently defaults to an empty list. Automatic data-quality
diagnostics, normalized requested/effective configuration, and richer TCP/CPU
analysis are future work.

