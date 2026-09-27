# Prometheus snapshots

The exporter is available in 0.3.0. See
[installation](../getting-started.md) for package setup.

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
series. The renderer adds `direction` and `observer` to measurement samples.
Caller labels with either name, or any name beginning with `__`, are rejected
with `ValueError`.

Label values must be strings. Invalid label names, non-finite sample values,
and duplicate metric-name/label combinations raise an error. Backslashes,
quotes, and newlines in values are escaped.

The renderer groups each metric family's samples together after one `HELP`
and one `TYPE` declaration, as required by the
[Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/).

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
metrics use only your supplied labels. Missing measurements, including an
absent bitrate inside a present summary, are omitted. Measured zero values
are emitted as zero. An endpoint can therefore export its known retransmission
count even when its throughput is unavailable. See [result semantics](results.md).

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

Saved native documents without an observed completion timestamp omit the
completion metric, including successful documents. Requested duration is never
used to invent freshness. The separately named estimate in execution metadata
is not an observed completion. A live `Client.run()` records its return time
for both successful and failed runs. Portable artifacts preserve that evidence.

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

The writer validates the rendered output before touching the filesystem,
creates the parent directory if necessary, writes a temporary file in that
directory, flushes it, and replaces the destination atomically. Give your
application permission to write that directory and configure node_exporter to
collect it.

On POSIX, the temporary file has owner-only permissions. Those permissions
carry through the replacement; the previous destination's permissions are not
preserved. The collector must be able to read each replacement file, in
addition to the writer being able to write the directory. The
[local Grafana example](grafana.md) runs the writer and node_exporter with the
same user ID. Other deployments must arrange compatible accounts and access;
this API does not expose a file-mode option.

The intended sequence is:

1. Your scheduler runs the benchmark.
2. Your application replaces the snapshot after completion, including failures.
3. node_exporter reads the saved file independently of the benchmark schedule.

Completion times appear as gauge values, not explicit sample timestamps.
The file remains until replaced or removed, so use freshness metrics to detect
an application that has stopped producing new results. The library does not
delete stale files automatically.

## Explore the complete pipeline

The [Grafana guide](grafana.md) provides a Kubernetes
example with a native iperf server, explicitly requested client runs, node_exporter,
Prometheus, and a provisioned dashboard. Use it to inspect completed-run
measurements and freshness across the complete collection path.

