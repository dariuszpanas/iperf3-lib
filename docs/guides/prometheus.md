# Prometheus snapshots

!!! warning "Unreleased exporter"
    The exporter is available on `main` after `0.2.0`. The current renderer
    repeats metric metadata when multiple endpoint observations are present.
    Prometheus requires one `HELP` and `TYPE` declaration per metric family;
    consumer-parser validation and a correction are needed before release.
    See the [roadmap](../roadmap.md) and the
    [Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/).

The exporter turns a completed `Result` into a latest-run snapshot. Your
application owns scheduling and storage; this package provides no HTTP server
and does not start a benchmark when Prometheus scrapes.

## Render a completed run

```python
from iperf3_lib.exporters.prometheus import render_text

text = render_text(
    result,
    labels={"target": "lab-server-b", "profile": "tcp-4-streams"},
)
```

Keep label values stable and bounded. A target and named test profile are
useful; run IDs, timestamps, and free-text error messages create unbounded
series. The renderer adds `direction` and `observer` to measurement samples;
leave those labels to the renderer.

Label values must be strings. Invalid label names and non-finite sample
values raise an error. Backslashes, quotes, and newlines in values are escaped.

## Understand the metrics

Every emitted series is a **gauge** describing a completed run. Counts are
run snapshots, not counters accumulating across runs.

| Metric | Value |
| --- | --- |
| `iperf3_last_run_success` | `1` for success, `0` for failure. |
| `iperf3_last_run_completed_timestamp_seconds` | Unix completion time, when available. |
| `iperf3_last_success_timestamp_seconds` | Unix completion time of the latest known successful run. |
| `iperf3_last_run_throughput_bytes_per_second` | Endpoint bitrate divided by eight. |
| `iperf3_last_run_retransmissions` | Reported TCP retransmissions. |
| `iperf3_last_run_packet_loss_ratio` | Reported loss percentage divided by 100. |
| `iperf3_last_run_jitter_seconds` | Reported jitter in milliseconds divided by 1,000. |

Measurements carry `direction` and `observer` labels. Status and freshness
metrics use only your supplied labels. Missing optional measurements are
omitted; see the [result model's current missing-data limitations](results.md).

A failed result emits its failure status and known timestamps, without
throughput, retransmissions, loss, or jitter from earlier runs. Persist the
last successful completion time in your application and pass it when rendering
the next snapshot:

```python
text = render_text(
    result,
    labels={"target": "lab-server-b"},
    last_success_timestamp_seconds=previous_success_timestamp,
)
```

For a successful result with a known completion time, that time takes
precedence over the supplied previous success time. There is no stored state
inside the exporter.

## Write a node_exporter textfile

```python
from iperf3_lib.exporters.prometheus import write_textfile

write_textfile(
    "/var/lib/node_exporter/textfile_collector/iperf.prom",
    result,
    labels={"target": "lab-server-b", "profile": "tcp-4-streams"},
    last_success_timestamp_seconds=previous_success_timestamp,
)
```

The writer creates the parent directory if necessary, writes a temporary file
in the destination directory, flushes it, and replaces the destination
atomically. Give your application permission to write that directory and
configure node_exporter to collect it.

The intended sequence is:

1. Your scheduler runs the benchmark.
2. Your application replaces the snapshot after completion, including failures.
3. node_exporter reads the saved file independently of the benchmark schedule.

Completion times appear as gauge values, not explicit sample timestamps.
The file remains until replaced or removed, so use freshness metrics to detect
an application that has stopped producing new results. The library does not
delete stale files automatically.

