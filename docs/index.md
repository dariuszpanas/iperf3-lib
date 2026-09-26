---
description: Run libiperf from Python, inspect measurements, and build network benchmark workflows.
---

# Network benchmarks, from Python

`iperf3-lib` connects Python applications to native **libiperf** through CFFI.
Configure a client, run a benchmark, and keep its measurements in your application.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Read the API](reference/api.md){ .md-button }

<p class="project-facts">Python 3.12–3.14 · Linux tested · libiperf 3.19.1 / 3.21 · MIT license</p>

## Choose your next step

<div class="grid cards" markdown>

- **Run a benchmark**

    Install the shared library, configure a TCP or UDP client, and learn the
    synchronous and asynchronous APIs.

    [Installation and first run](getting-started.md)

- **Understand the measurements**

    Read flow directions, sender and receiver observations, intervals, and the
    original native JSON.

    [Results guide](guides/results.md)

- **Export completed runs**

    Explore the unreleased Prometheus renderer and atomic textfile writer,
    including their current validation gaps.

    [Prometheus guide](guides/prometheus.md)

- **Shape the next version**

    Follow the issue backlog for result contracts, analysis, benchmark plans,
    live events, and process isolation.

    [Roadmap and issues](roadmap.md)

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

Linux is the tested platform. Native calls block and share process-global error
state: serialize runs within a process. The async helpers use executor threads;
cancelling an await does not stop its native operation. See
[compatibility and limitations](reference/compatibility.md).

These pages track `main`. Published **0.2.0** still uses Pydantic models. The
dataclass migration, normalized results, and exporters are **unreleased**; install
from source to explore those APIs. The [changelog](changelog.md) separates released
behavior from current development.
