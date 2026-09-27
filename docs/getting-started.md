# Getting started

`iperf3-lib` runs network throughput tests through the native `libiperf` shared
library and returns Python objects for your application to inspect.

!!! note "Match the documentation to your version"
    This site follows `main`. Dataclasses, portable artifacts, analysis, trial
    plans, sweeps, and Prometheus output are available in 0.3.0; 0.2.0 uses
    Pydantic models. Use documentation matching your installed version.
    Check the [changelog](changelog.md) and
    [published releases](https://github.com/dariuszpanas/iperf3-lib/releases)
    for publication status.

Upgrading an existing 0.2.0 application? Read the
[dataclass migration guide](guides/migration-0.3.md) for API substitutions,
stricter inputs, missing-data handling, and saved-result formats.

## Install the Python package

For the published package, choose the installer used by your application:

```bash
uv add iperf3-lib
```

```bash
python -m pip install iperf3-lib
```

For an optional installation from the current source:

```bash
python -m pip install "iperf3-lib @ git+https://github.com/dariuszpanas/iperf3-lib.git@main"
```

The source command requires Git. For repeatable application builds, replace
`main` with a reviewed commit SHA and retain your application's lockfile.
Contributors should use the repository's development environment instead; see
[CONTRIBUTING.md](https://github.com/dariuszpanas/iperf3-lib/blob/main/CONTRIBUTING.md).

## Install libiperf

The package requires Python 3.12 or newer and does not bundle `libiperf`.
Linux with Python 3.12–3.14 is covered by this project's CI. The native version
matrix covers libiperf 3.19.1 and 3.21; see the
[compatibility reference](reference/compatibility.md).

Install a supported iperf3 version using your operating system's packages or
the [upstream releases](https://github.com/esnet/iperf/releases). Check the
version supplied by your distribution before using it.

If the shared library is outside the dynamic loader's search path, set
`IPERF3_LIB` before starting Python:

```bash
export IPERF3_LIB=/usr/local/lib/libiperf.so
```

This is the path to the shared library, not the `iperf3` executable. Importing
`iperf3_lib` alone does not load it; the first native operation does.

## Start a server

In a separate terminal, start an iperf3 server on the machine you want to test:

```bash
iperf3 -s
```

You can also run a Python server in a separate terminal:

```python
from iperf3_lib import Server

result = Server(port=5201, bind_host="127.0.0.1").run_once(timeout=30)
print(result.ok, result.error)
```

This example listens on loopback. For another interface, use its assigned local
IP address as `bind_host`. A device name belongs in the separate `ServerConfig`
device option. See [running a Python server](guides/running-tests.md#use-the-python-server-wrapper)
and [address/device binding](guides/native-controls.md#select-local-addresses-and-devices).

The following client uses `127.0.0.1`, so it measures a loopback path. Replace
that address with your server's hostname or address to measure a network path.

## Run a client

```python
from iperf3_lib import Client, ClientConfig, Protocol

config = ClientConfig(
    server="127.0.0.1",
    protocol=Protocol.TCP,
    duration=2,
    parallel=1,
)
result = Client(config).run()

if result.ok:
    print(f"{result.summary_mbps:.2f} Mbps")
else:
    print(f"Test failed: {result.error}")
```

`summary_mbps` is a convenience value: it picks the first available summary
rate, including zero and preferring the sender. It returns `0.0` when
unavailable. For directional analysis or missing-data decisions, use
[normalized results](guides/results.md).

## Next steps

- [Migrate a 0.2.0 application to dataclasses](guides/migration-0.3.md).
- [Run TCP, UDP, reverse, or bidirectional tests](guides/running-tests.md).
- [Choose configuration values and understand validation](reference/configuration.md).
- [Find a native CLI option's Python equivalent](reference/native-options.md)
  and [use binding, transport, timeout and event controls](guides/native-controls.md).
- [Declare aggregate rate intent and inspect capabilities](guides/configuration-intent.md).
- [Read flows, observations, intervals, and native JSON](guides/results.md).
- [Save portable artifacts](guides/artifacts.md) and [analyze measurements](guides/analysis.md).
- [Repeat and assess trials](guides/trials.md) or [run finite parameter sweeps](guides/sweeps.md).
- [Explore Prometheus snapshots and textfile output](guides/prometheus.md).

