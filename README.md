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

## Why use iperf3-lib?

Use `iperf3-lib` when benchmarks feed an application, a repeatable test campaign,
or a monitoring pipeline. Reuse a tested Python API for the work that otherwise
accumulates around `subprocess` calls or Ansible tasks:

- **Configure tests with validated dataclasses**, including traffic direction,
  transport controls, and explicit rate intent.
- **Interpret measurements consistently**, keeping sender and receiver
  observations separate, preserving native JSON, and distinguishing missing
  measurements from measured zero.
- **Build on reusable experiments and reports**: repeated trials, baseline
  assessments, bounded parameter sweeps, portable artifacts, and Prometheus
  output.

The library calls `libiperf` through CFFI; it does not invoke the `iperf3`
executable. Some execution paths use isolated Python worker processes. A short
CLI script remains useful for one-off tests, and Ansible can deploy and run
Python benchmark applications across your hosts. See
[choosing a Python API, CLI, or Ansible workflow](https://dariuszpanas.github.io/iperf3-lib/guides/choosing-an-integration.html)
for the comparison, execution boundaries, and version-specific features.

## Getting started

Follow the [installation and first-run guide](https://dariuszpanas.github.io/iperf3-lib/getting-started.html)
to install the Python package and native library, start a server, and run a
benchmark. The Python package does not bundle libiperf. Current native
qualification covers libiperf **3.19.1** and **3.22** on Linux; see the
[compatibility reference](https://dariuszpanas.github.io/iperf3-lib/reference/compatibility.html).

Documentation follows `main`. The installation guide explains how to choose
between a published package and a source revision; consult the
[changelog](https://dariuszpanas.github.io/iperf3-lib/changelog.html) for release
status. Upgrade guidance covers
[0.2.0 to 0.3.0](https://dariuszpanas.github.io/iperf3-lib/guides/migration-0.3.html)
and [unreleased changes after 0.3.0](https://dariuszpanas.github.io/iperf3-lib/guides/migration-0.4.html).

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
for development setup, checks, and repository policies. Use
[GitHub issues](https://github.com/dariuszpanas/iperf3-lib/issues) to report bugs
or discuss planned work.
