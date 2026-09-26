# Compatibility and native setup

## Tested targets

| Component | Project coverage |
| --- | --- |
| Python | 3.12, 3.13, 3.14. |
| libiperf | 3.19.1 minimum; 3.21 default. CI tests both endpoints. |
| Operating system | Linux. |
| Protocols | TCP, UDP, SCTP; SCTP also requires operating-system and native-library support. |

The package declares Python `>=3.12`; the versions listed above are the
current tested matrix. macOS and FreeBSD are unverified by this project.
Windows DLL loading is best-effort and has no native CI coverage.

Installing the Python package does not install or upgrade libiperf. Follow the
[upstream iperf releases](https://github.com/esnet/iperf/releases) for native
release information. For local development, Docker provides the repository's
reproducible Linux validation environment.

## Library discovery

Set `IPERF3_LIB` to an explicit shared-library path when needed:

```bash
export IPERF3_LIB=/usr/local/lib/libiperf.so
```

Without that variable the loader tries these names in order:

1. `libiperf.so`
2. `libiperf.so.0`
3. `libiperf.dylib`
4. `iperf3.dll`
5. `libiperf.dll`

The operating system resolves those names and any transitive shared-library
dependencies. Set the environment before the first native operation: the
loaded library is cached in the process. An explicit path that cannot be
loaded raises `IperfLibraryError` rather than falling back to another name.

## Feature boundaries

| Feature | Current wrapper behavior |
| --- | --- |
| TCP, UDP, SCTP selection | Uses the public protocol setter and verifies the selected protocol with a getter. |
| Reverse and parallel streams | Exposed through `ClientConfig`. |
| Simultaneous bidirectional mode | Exposed when the native setter is available. |
| Native JSON | One complete result after the run. |
| MPTCP | `mptcp=True` is explicitly rejected. |
| Streaming JSON | `json_stream=True` is explicitly rejected. |
| Async convenience | Executor-backed calls; cancellation does not stop native execution. |
| Concurrent native operations | Requires separate processes; same-process concurrency is unsupported. |
| Server shutdown | Cooperative between iterations; no interruption of a blocking native call. |

An iperf3 command-line option is not automatically a Python API option. Only
the fields in the [configuration reference](configuration.md) are exposed.
[Capability reports](../guides/configuration-intent.md) distinguish wrapper,
native, and qualification evidence. Additional native options remain evaluated
follow-ups in the [roadmap](../roadmap.md).

## Troubleshooting

**The package imports, but running a client fails to load libiperf.** Check
the shared-library installation, architecture, native dependencies, and
`IPERF3_LIB`. A Python import only checks the Python package because native
loading is deferred.

**A requested feature raises `UnsupportedFeatureError`.** Check the wrapper
feature table and loaded library version. Symbol availability, wrapper
support, and a successful test are separate checks.

**An asyncio timeout expires, but traffic continues.** The executor thread
still owns a blocking native operation. Wait for its completion before
reusing the process, or design execution around separate worker processes.

**The reported number differs from the iperf3 terminal summary.** Compare
the same direction, observation point, interval, and units in `result.raw`.
The [results guide](../guides/results.md) explains the convenience summary
and the current normalization limits.
