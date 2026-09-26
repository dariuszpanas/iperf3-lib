# Getting started

`iperf3-lib` runs network throughput tests through the native `libiperf` shared
library and returns Python objects for your application to inspect.

!!! note "Documentation for the next release"
    This site tracks `main`. Dataclass configuration, normalized flows and
    intervals, and Prometheus output are unreleased additions after `0.2.0`.
    Install from source to use those APIs. The
    [published releases](https://github.com/dariuszpanas/iperf3-lib/releases)
    describe the versions available on PyPI.

## Install the Python package

For the published package, choose the installer used by your application:

```bash
uv add iperf3-lib
```

```bash
python -m pip install iperf3-lib
```

To try the APIs documented on this site from the current source:

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

`summary_mbps` is a convenience value: it picks the first nonzero summary
rate, preferring the sender. It returns `0.0` when unavailable. For directional
analysis or missing-data decisions, use [normalized results](guides/results.md).

## Next steps

- [Run TCP, UDP, reverse, or bidirectional tests](guides/running-tests.md).
- [Choose configuration values and understand validation](reference/configuration.md).
- [Read flows, observations, intervals, and native JSON](guides/results.md).
- [Explore Prometheus snapshots and textfile output](guides/prometheus.md).

