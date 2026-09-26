# iperf3-lib

[![CI](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml/badge.svg)](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)
[![Python](https://img.shields.io/pypi/pyversions/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)

[Documentation](https://dariuszpanas.github.io/iperf3-lib/) ·
[Roadmap](https://dariuszpanas.github.io/iperf3-lib/roadmap.html) ·
[Issues](https://github.com/dariuszpanas/iperf3-lib/issues)

> This README and the documentation track `main`. Published **0.2.0** uses
> Pydantic; the dataclass models, normalized results, and Prometheus exporter
> described below are **unreleased**. See the
> [installation guide](https://dariuszpanas.github.io/iperf3-lib/getting-started.html)
> for installing the development version.

`iperf3-lib` is a typed Python wrapper around the native iperf3 `libiperf`
library. It uses CFFI's ABI mode and provides synchronous and asynchronous
client APIs, a minimal server wrapper, validated dataclass configuration, and
typed result models.

The normalized result model preserves the original native JSON while exposing
flows, endpoint observations, interval measurements, and execution metadata.
Completed results can be rendered as Prometheus metrics or atomically written
for node_exporter's textfile collector; scraping never starts a benchmark.

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

- MPTCP cannot be selected through libiperf's published ABI. Setting
  `ClientConfig(mptcp=True)` raises `UnsupportedFeatureError`.
- Streaming JSON is not exposed. Setting `json_stream=True` raises
  `UnsupportedFeatureError`; normal runs still return one complete JSON result.
- `Client.arun()` and `Server.aserve_once()` run blocking libiperf calls in an
  executor thread. Cancelling the awaiting task does not stop the native call.
- Concurrent operations in the same process are not supported. Serialize runs
  or isolate them in separate processes.
- `Server.stop()` is cooperative: it prevents the next server iteration but
  cannot interrupt an active blocking `iperf_run_server()` call.

## Install

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

`Result` keeps the complete native JSON in `raw` and provides normalized
`flows`, `intervals`, protocol, duration, and timestamp fields. Each flow
identifies its direction independently from whether the native measurement was
reported by the sender or receiver. Missing measurements are `None`; measured
zeros remain zero. Ambiguous stream direction is `"unknown"` with diagnostics,
and native error documents are failed results. The legacy `summary_mbps`
convenience still returns `0.0` when no rate is available. Review the
[result semantics](https://dariuszpanas.github.io/iperf3-lib/guides/results.html)
before using the development API for automated acceptance decisions.
For durable storage, the unreleased
[versioned artifact API](https://dariuszpanas.github.io/iperf3-lib/guides/artifacts.html)
preserves normalized measurements, native JSON, requested and verified settings,
and timing provenance. It loads without libiperf. `Result.to_dict()` remains
an unversioned dataclass snapshot.
Configuration strings for protocols are normalized to `Protocol`; numeric
configuration fields require integers (not booleans or numeric strings), and
boolean options require actual booleans. Hostnames remain strings, and standard
library IPv4/IPv6 address objects are accepted.

The exporter is unreleased. A
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
server.run_once()
```

`serve_forever()` reuses one libiperf test object across sequential runs. See
the cooperative shutdown limitation above before embedding it in a service.

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

### Unreleased

- Replace Pydantic with standard-library dataclasses and explicit configuration
  validation; add normalized flow/interval results and Prometheus textfile output.
- Adopt YAGA repository and commit policies, current stable uv, and refreshed tooling.
- Add a Zensical documentation site and issue-based roadmap.
- Correct Prometheus metric-family output and native bidirectional parsing;
  add a Docker Desktop Kubernetes/Grafana qualification example.

See the [full changelog](https://dariuszpanas.github.io/iperf3-lib/changelog.html)
for migration notes and release history.

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
