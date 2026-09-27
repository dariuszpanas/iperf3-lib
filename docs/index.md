---
description: Run libiperf from Python, inspect measurements, and build network benchmark workflows.
---

# Network benchmarks, from Python

`iperf3-lib` is a programmable network benchmarking and analysis library powered
by native **libiperf** through CFFI. Configure traffic, preserve the measurements,
compare results, and run finite sequential experiments from Python.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Read the API](reference/api.md){ .md-button }

<p class="project-facts">Python 3.12–3.14 · Linux tested · libiperf 3.19.1 / 3.21 · MIT license</p>

## Choose your next step

<div class="grid cards" markdown>

- **Run a benchmark**

    Install the shared library, configure a TCP or UDP client, and learn the
    synchronous and asynchronous APIs.

    [Installation and first run](getting-started.md) · [Native controls](guides/native-controls.md)

- **Understand the measurements**

    Read flow directions, sender and receiver observations, intervals, and the
    original native JSON.

    [Results guide](guides/results.md)

- **Keep portable evidence**

    Save native and normalized measurements with requested settings, verified
    observations, producer identity, and timing provenance.

    [Result artifacts](guides/artifacts.md)

- **Analyze measurements**

    Calculate throughput stability, stream balance and scaling, and directional
    comparisons with explicit missing-data and diagnostic limits.

    [Analysis guide](guides/analysis.md)

- **Run repeatable experiments**

    Retain warm-ups, repetitions and failures; assess compatible baselines or
    explore finite parameter matrices with explicit order and admission budgets.

    [Trials and assessments](guides/trials.md) · [Parameter sweeps](guides/sweeps.md)

- **Export completed runs**

    Use Prometheus gauges, atomic textfiles, and a reproducible local Grafana
    integration. Benchmark scheduling remains under your application's control.

    [Prometheus guide](guides/prometheus.md)

</div>

## A small starting point

Start a compatible iperf3 server on a host you control, then run:

```python
from iperf3_lib import Client, ClientConfig

result = Client(ClientConfig(server="127.0.0.1", duration=2)).run()
if result.ok:
    print(f"{result.summary_mbps:.2f} Mbps")
else:
    print(result.error)
```

The native library generates traffic and measures it. Python provides configuration,
typed access to the output, and integration with your application. The package does
not bundle libiperf or operate a benchmark scheduler or metrics service.

## Know the execution boundary

Linux is the tested platform. Basic direct native calls share process-global
state and must be serialized. Expanded controls, event delivery and explicit
execution timeouts use isolated Python/CFFI workers. Async helpers use executor
threads; cancelling an await alone does not stop the operation. See
[compatibility and limitations](reference/compatibility.md).

Use the [native option inventory](reference/native-options.md) to find binding,
protocol, transport, server-policy and output controls. Option availability also
depends on the native build, kernel and peer; a configuration request is not
proof of effective network behavior.

These pages follow `main`. The APIs introduced in **0.3** require that version
or a reviewed source revision; before publication, install from source.
Published **0.2.0** uses Pydantic models. The [changelog](changelog.md) records
publication status and the [migration guide](guides/migration-0.3.md) explains
compatibility changes.

The [roadmap](roadmap.md) records the selected scope, expanded native-control
work, and the remaining advanced execution and adaptive UDP qualification.
