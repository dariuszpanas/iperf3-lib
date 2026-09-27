# iperf3-lib

[![CI](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml/badge.svg)](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)
[![Python](https://img.shields.io/pypi/pyversions/iperf3-lib.svg)](https://pypi.org/project/iperf3-lib/)

[Documentation](https://dariuszpanas.github.io/iperf3-lib/) ·
[Changelog](https://dariuszpanas.github.io/iperf3-lib/changelog.html) ·
[Issues](https://github.com/dariuszpanas/iperf3-lib/issues)

`iperf3-lib` is a programmable network benchmarking and analysis library powered
by native **libiperf** through CFFI. Configure clients and servers from Python,
preserve and analyze measurements, run repeatable experiments, and export
results to Prometheus.

## Getting started

Follow the [installation and first-run guide](https://dariuszpanas.github.io/iperf3-lib/getting-started.html)
to install the Python package and native library, start a server, and run a
benchmark. The Python package does not bundle libiperf.

Documentation follows `main`. The installation guide explains how to choose
between a published package and a source revision; consult the
[changelog](https://dariuszpanas.github.io/iperf3-lib/changelog.html) for release
status and the [migration guide](https://dariuszpanas.github.io/iperf3-lib/guides/migration-0.3.html)
when upgrading an existing application.

## Documentation

The documentation contains the examples, API contracts, supported versions,
and execution limits.

| Topic | Guides and references |
| --- | --- |
| Run clients and servers | [Running tests](https://dariuszpanas.github.io/iperf3-lib/guides/running-tests.html) · [Python API](https://dariuszpanas.github.io/iperf3-lib/reference/api.html) |
| Bind interfaces and tune traffic | [Native-control recipes](https://dariuszpanas.github.io/iperf3-lib/guides/native-controls.html) · [Configuration](https://dariuszpanas.github.io/iperf3-lib/reference/configuration.html) · [Complete native-option inventory](https://dariuszpanas.github.io/iperf3-lib/reference/native-options.html) |
| Check support and execution limits | [Compatibility and native setup](https://dariuszpanas.github.io/iperf3-lib/reference/compatibility.html) · [Rate intent and capabilities](https://dariuszpanas.github.io/iperf3-lib/guides/configuration-intent.html) |
| Interpret and save measurements | [Results](https://dariuszpanas.github.io/iperf3-lib/guides/results.html) · [Portable artifacts](https://dariuszpanas.github.io/iperf3-lib/guides/artifacts.html) |
| Analyze and compare experiments | [Analysis](https://dariuszpanas.github.io/iperf3-lib/guides/analysis.html) · [Trials and baselines](https://dariuszpanas.github.io/iperf3-lib/guides/trials.html) · [Parameter sweeps](https://dariuszpanas.github.io/iperf3-lib/guides/sweeps.html) |
| Integrate monitoring | [Prometheus](https://dariuszpanas.github.io/iperf3-lib/guides/prometheus.html) · [Grafana](https://dariuszpanas.github.io/iperf3-lib/guides/grafana.html) |

## Contributing

See the [contributor guide](https://dariuszpanas.github.io/iperf3-lib/contributing.html)
for development setup, checks, and repository policies. The
[roadmap](https://dariuszpanas.github.io/iperf3-lib/roadmap.html) and
[issues](https://github.com/dariuszpanas/iperf3-lib/issues) track planned work.
