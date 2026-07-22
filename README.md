# iperf3-lib

[![CI](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml/badge.svg)](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)
[![Python](https://img.shields.io/pypi/pyversions/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)

`iperf3-lib` is a typed Python wrapper around the native iperf3 `libiperf`
library. It uses CFFI's ABI mode and provides synchronous and asynchronous
client APIs, a minimal server wrapper, Pydantic configuration, and typed result
models.

## Support

| Component | Supported and tested | Notes |
| --- | --- | --- |
| Python | 3.12, 3.13, 3.14 | Python 3.15 prereleases run in a non-blocking preview job. |
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

## Server example

```python
from iperf3_lib import Server

server = Server(port=5201, bind_host="127.0.0.1")
server.run_once()
```

`serve_forever()` reuses one libiperf test object across sequential runs. See
the cooperative shutdown limitation above before embedding it in a service.

## Contributing

Install [uv](https://docs.astral.sh/uv/) 0.11.31 and synchronize the committed
lockfile:

```bash
uv sync --frozen --dev
```

With a supported native libiperf available:

```bash
make check
uv run --frozen pytest
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
formatting and safe lint fixes. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
full development and review checklist.

## Changelog

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
