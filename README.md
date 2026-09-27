# iperf3-lib

[![CI](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml/badge.svg)](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)
[![Python](https://img.shields.io/pypi/pyversions/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)

[Documentation](https://dariuszpanas.github.io/iperf3-lib/) ·
[Roadmap](https://dariuszpanas.github.io/iperf3-lib/roadmap.html) ·
[Issues](https://github.com/dariuszpanas/iperf3-lib/issues)

> The dataclass, artifact, analysis, trial, sweep, and exporter APIs described here
> are introduced in **0.3**. Use 0.3 or a reviewed source revision; before 0.3
> is published, install from source. Published **0.2.0** uses Pydantic models.
> The [documentation](https://dariuszpanas.github.io/iperf3-lib/) follows `main`;
> check the [installation guide](https://dariuszpanas.github.io/iperf3-lib/getting-started.html)
> for version-specific instructions.

For an existing 0.2.0 application, follow the
[dataclass migration guide](https://dariuszpanas.github.io/iperf3-lib/guides/migration-0.3.html)
before changing model methods or loading saved results.

`iperf3-lib` is a programmable network benchmarking and analysis library powered
by native **libiperf**. Native code generates traffic and takes measurements;
Python provides validated configuration, reusable results, analysis, and finite
sequential experiments. CFFI is the only direct runtime dependency.

- Preserve native JSON alongside normalized directional measurements and
  [portable artifacts](https://dariuszpanas.github.io/iperf3-lib/guides/artifacts.html).
- Analyze measured throughput, interval stability, stream balance and scaling,
  directional asymmetry, and available TCP/CPU evidence with
  [explicit data-quality rules](https://dariuszpanas.github.io/iperf3-lib/guides/analysis.html).
- Run [repeated trials and baseline assessments](https://dariuszpanas.github.io/iperf3-lib/guides/trials.html)
  with admission budgets, retained failures, and JSON, text, JUnit, and CI outcomes.
- Explore [bounded parameter sweeps](https://dariuszpanas.github.io/iperf3-lib/guides/sweeps.html)
  with recorded order, qualified cell measurements, and portable reports.
- Render completed results as Prometheus gauges or atomic node_exporter textfiles;
  scraping never starts a benchmark.

Synchronous clients, async convenience methods, and Python servers share the
result model. The [roadmap](https://dariuszpanas.github.io/iperf3-lib/roadmap.html)
records selected scope and follow-up designs.

## Choose the controls you need

| Task | Python controls |
| --- | --- |
| Select endpoints and interfaces | Destination/port, client local address and source port, server local address, named device, IPv4/IPv6. |
| Shape traffic | TCP/UDP/SCTP, forward/reverse/bidirectional, streams, duration/byte/block termination, rate intent, pacing, payload and transport tuning. |
| Inspect native results | Client/server `Result`, original JSON, directional summaries, intervals, optional remote server output. |
| Bound or observe a run | `Client.run(timeout=..., on_event=...)` uses an isolated Python/CFFI worker; typed events retain delivery-loss information. |
| Build experiments | Capabilities, portable artifacts, analysis, repeated assessments, finite sweeps and Prometheus snapshots. |

The [complete option inventory](https://dariuszpanas.github.io/iperf3-lib/reference/native-options.html)
maps all flags from both supported native versions, including platform limits
and command-line-only concerns. Start with the
[native-control recipes](https://dariuszpanas.github.io/iperf3-lib/guides/native-controls.html)
or [rate intent and capabilities](https://dariuszpanas.github.io/iperf3-lib/guides/configuration-intent.html).
Expanded controls are development work under
[#53](https://github.com/dariuszpanas/iperf3-lib/issues/53); use a reviewed source
revision containing them. Earlier candidate qualification does not cover later changes.

## Support

| Component | Supported and tested | Notes |
| --- | --- | --- |
| Python | 3.12, 3.13, 3.14 | CI and release smoke tests cover every supported version. |
| libiperf | 3.19.1, 3.21 | 3.19.1 is the minimum supported security baseline; 3.21 is the default. |
| Platform | Linux | CI runs on Linux. macOS and FreeBSD are unverified; Windows requires a compatible DLL and is best-effort. |
| Protocol | TCP, UDP, SCTP | SCTP also requires operating-system and libiperf SCTP support. |

The Python package does not bundle `libiperf`. Install a supported iperf3
release from your operating-system packages or from the
[official iperf releases](https://github.com/esnet/iperf/releases). If the
library is outside the dynamic loader's normal search path, set `IPERF3_LIB` to
the full shared-library path before using a client or server.

```bash
export IPERF3_LIB=/usr/local/lib/libiperf.so
```

### Current limitations

- Native option support depends on the installed build, kernel, peer and
  permissions. MPTCP requires TCP/native/kernel support; UDP GSRO requires 3.21.
- Basic direct client calls share libiperf's process-global state. Serialize
  them; multiple client objects or executor threads do not provide isolation.
- Expanded controls, streaming, an event callback or an explicit timeout select
  an isolated Python/CFFI worker. A timeout terminates and reaps that worker;
  callbacks have bounded delivery and may drop events, with retained counts.
- Async convenience uses executor threads. Cancelling an await alone does not
  terminate its operation; pass an explicit execution timeout to bound it.
- `Server.stop()` is cooperative between runs. Use the server's explicit
  session timeout to bound waiting for or handling clients.

## Install

These commands install the published package. Before 0.3 is available, use the
[source installation instructions](https://dariuszpanas.github.io/iperf3-lib/getting-started.html)
for the APIs introduced in 0.3.

For an application using uv:

```bash
uv add iperf3-lib
```

With pip:

```bash
python -m pip install iperf3-lib
```

Importing the package does not load the native library immediately. The first
client/server operation will raise `IperfLibraryError` if a compatible
`libiperf` cannot be found.

## Client example

Start an iperf3 server separately, then run:

```python
from iperf3_lib import Client, ClientConfig, Protocol

config = ClientConfig(
    server="127.0.0.1",
    duration=2,
    parallel=2,
    protocol=Protocol.TCP,
)
result = Client(config).run()

if result.ok:
    print(f"{result.summary_mbps:.2f} Mbps")
else:
    print(f"iperf failed: {result.error}")
```

For UDP, the wrapper applies libiperf's 1,048,576 bits/s default rate and leaves
block-size selection to libiperf's dynamic path unless a value is supplied.
SCTP defaults to a 65,536-byte block. Set `rate` or `blksize` explicitly to
override these values.

The asynchronous API has the same configuration and result behavior:

```python
result = await Client(config).arun()
```

## Results and Prometheus metrics

`Result` keeps parsed native JSON in `raw` and provides normalized
`flows`, `intervals`, protocol, duration, and timestamp fields. Each flow
identifies its direction independently from whether the native measurement was
reported by the sender or receiver. Missing measurements are `None`; measured
zeros remain zero. Ambiguous stream direction is `"unknown"` with diagnostics,
and native error documents are failed results. The legacy `summary_mbps`
convenience still returns `0.0` when no rate is available. Review the
[result semantics](https://dariuszpanas.github.io/iperf3-lib/guides/results.html)
before making automated acceptance decisions.
Streaming on native 3.19.1 reconstructs `raw` from retained event envelopes and
labels that provenance explicitly; 3.21 streaming enables full native output.
Bounded callback queues do not impose a memory bound on retained result data.
For durable storage, the
[versioned artifact API](https://dariuszpanas.github.io/iperf3-lib/guides/artifacts.html)
preserves normalized measurements, native JSON, requested and verified settings,
and timing provenance. It loads without libiperf. `Result.to_dict()` remains
an unversioned dataclass snapshot.
Configuration strings for protocols are normalized to `Protocol`; count, byte
and rate fields require integers, while interval fields accept finite numbers. All
of these reject boolean substitution and implicit numeric-string conversion, and
boolean options require actual booleans. Hostnames remain strings, and standard
library IPv4/IPv6 address objects are accepted.

A
[local Grafana integration](https://dariuszpanas.github.io/iperf3-lib/guides/grafana.html)
provides a reproducible native benchmark, textfile collector, Prometheus,
and dashboard validation path.
Render a completed run for an existing Prometheus metrics endpoint:

```python
from iperf3_lib.exporters.prometheus import render_text

metrics = render_text(result, labels={"target": "lab-server-b", "profile": "tcp-4-streams"})
```

All measurements are latest-run gauges using base units such as bytes per
second, seconds, and loss ratios. Supplied labels should remain bounded and
stable; avoid run IDs, timestamps, and error messages. Failed runs expose their
status and completion time, omit measurements from earlier runs, and can retain
the last-success timestamp supplied by the consuming application:

```python
metrics = render_text(
    result,
    labels={"target": "lab-server-b"},
    last_success_timestamp_seconds=previous_success_timestamp,
)
```

For host-associated tests, write a textfile collector metric file with an
atomic same-directory replacement:

```python
from iperf3_lib.exporters.prometheus import write_textfile

write_textfile("/var/lib/node_exporter/textfile_collector/iperf.prom", result)
```

Schedule benchmark runs separately from Prometheus scrapes so scrape frequency
does not control generated traffic. The library does not start a metrics
server.

## Server example

```python
from iperf3_lib import Server

server = Server(port=5201, bind_host="127.0.0.1")
result = server.run_once()
print(result.ok, result.reporting_role)
```

`bind_host` is an address assigned to the local host, not a network-device name.
Replace loopback with the desired interface's assigned IP address for a remote
test. Use `ServerConfig` for named-device binding and server policies; see
[server configuration](https://dariuszpanas.github.io/iperf3-lib/reference/configuration.html#server-configuration).
For sequential clients, use `server.serve_forever()` with result callbacks and
an explicit whole-session timeout when a bounded wait is required.

## Contributing

Install a current stable [uv](https://docs.astral.sh/uv/) release, then
synchronize the committed lockfile:

```bash
make install
```

With a supported native libiperf available:

```bash
make check
make test
```

The reproducible Linux route builds libiperf and runs the complete suite in
Docker:

```bash
make docker-test
```

You can override either compatibility dimension:

```bash
make docker-test PYTHON_BASE=python:3.14-slim IPERF3_VERSION=3.19.1
```

`make check` never changes source files. Run `make format` explicitly to apply
formatting and safe lint fixes. YAGA is included in the development environment
and enforces this repository's commit, workflow, and file policies in local
commands and CI. See [CONTRIBUTING.md](https://github.com/dariuszpanas/iperf3-lib/blob/main/CONTRIBUTING.md#yaga-checks-and-policies)
for policy commands and the full development and review checklist.

Build the documentation with `make docs`, or preview it with `make docs-serve`.

## Changelog

### APIs introduced in 0.3

- Replace Pydantic with dataclasses and strict configuration validation; preserve
  missing measurements separately from measured zero.
- Add canonical directional results, portable versioned artifacts, rate intent,
  capability reports, evidence-aware analysis, retained trial/baseline reports,
  and reproducible finite parameter sweeps.
- Add Prometheus snapshot gauges, freshness and atomic textfiles, with a
  Docker Desktop Kubernetes/Grafana qualification example.
- Adopt YAGA policies, current stable uv, documentation and issue workflows,
  shared local Docker staging, and retained-artifact release qualification.

See the [full changelog](https://dariuszpanas.github.io/iperf3-lib/changelog.html)
for publication status and release history, and the
[migration guide](https://dariuszpanas.github.io/iperf3-lib/guides/migration-0.3.html)
for configuration, result, and serialization changes.

### 0.2.0 — 2026-07-22

- Correctly apply and verify TCP, UDP, and SCTP protocol selection.
- Fix server bind-address handling, lazy-load libiperf, and keep JSON output
  out of the host process's stdout.
- Validate native option limits and reject unsupported MPTCP/streaming-JSON
  requests explicitly.
- Add Python 3.14 and libiperf 3.21 support, refreshed dependencies, reproducible
  CI/release tooling, and stronger native integration coverage.

### 0.1.0

- Initial CFFI ABI wrapper for libiperf clients and servers.
- Typed Pydantic configuration/results and asynchronous convenience methods.
- Docker compatibility testing and PyPI release automation.
