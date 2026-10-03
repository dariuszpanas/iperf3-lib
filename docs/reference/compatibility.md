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
| Expanded controls | Validated typed fields select an isolated Python worker using libiperf's public parser through CFFI. No `iperf3` executable is needed for that path. |
| Native JSON | Completed client/server results remain available independently of live event delivery. |
| MPTCP | `mptcp=True` uses the worker; requires TCP and native/kernel support. |
| Streaming JSON | `json_stream=True` or an event callback selects bounded worker event delivery. |
| Async convenience | Always isolated; cancellation waits for worker cleanup and any active callback. Unconfirmed cleanup raises `IperfCleanupError`. This behavior is new after 0.3.0. |
| Concurrent native operations | Basic direct calls remain non-reentrant. Expanded worker calls isolate native state; ordinary client construction alone does not select isolation. |
| Execution timeout | Explicit `timeout` terminates/reaps the worker before raising; native C finalizers are not promised on forced termination. |
| Server shutdown | `stop()` is cooperative between iterations; a worker timeout bounds the complete server session. |
| Parent death | On Linux, a worker bootstrap installs `SIGKILL` on parent death and checks the expected parent before native execution. Other platforms have no corresponding guarantee. See [worker lifetime](../guides/running-tests.md#worker-lifetime-on-linux). |

An iperf3 command-line option is not automatically a Python API option. The
[complete option inventory](native-options.md) distinguishes typed controls,
native-version/platform constraints, and CLI presentation/process concerns.
The [configuration reference](configuration.md) defines accepted fields.
[Capability reports](../guides/configuration-intent.md) distinguish wrapper,
native, and tested behavior.
Native 3.21 adds GSRO and server maximum-duration controls absent from 3.19.1.

## Authentication compatibility

Use libiperf 3.21 for authenticated benchmarks. The tested 3.19.1/OpenSSL 3.5.7
combination rejects valid credentials with an authentication failure and
`output buffer too small`. The failure also occurs with the upstream CLI.

The [3.19.1 authentication implementation](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_auth.c)
passes a zero output length to OpenSSL's encryption call; the
[3.21 implementation](https://github.com/esnet/iperf/blob/3.21/src/iperf_auth.c)
initializes that length to the allocated buffer size. Treat authenticated
operation with that 3.19.1/OpenSSL build as a known native limitation.
Other native builds may differ.

The wrapper preserves native authentication access and a failed `Result`;
it does not reject every 3.19.1 build or replace the native cryptographic
implementation.
Client `use_pkcs1_padding=True` is rejected with 3.21 because that native flag
is server-only; leave it at its default for a 3.21 client.

## Troubleshooting

**The package imports, but running a client fails to load libiperf.** Check
the shared-library installation, architecture, native dependencies, and
`IPERF3_LIB`. A Python import only checks the Python package because native
loading is deferred.

**A requested feature raises `UnsupportedFeatureError`.** Check the wrapper
feature table and loaded library version. Symbol availability, wrapper
support, and a successful test are separate checks.

**An asyncio timeout expires, but traffic continues.** Version 0.3.0 leaves the
executor operation running when its await is cancelled. Current `main` makes
`arun()` and `aserve_once()` request worker termination and await cleanup and
any active callback during cancellation. Unconfirmed cleanup raises
`IperfCleanupError` and retains worker ownership.
Application-owned executor wrappers around synchronous methods retain their
original behavior. The library's `timeout` argument sets an independent worker
deadline. Wait for a basic direct operation to finish before reusing its process.

**A callback is slow or events are missing.** Callbacks run synchronously in
Python. Keep them short: bounded queues drop events. The independent timeout
watchdog still stops the worker, but caller return waits for a blocked callback.
Inspect delivery counts and capture provenance; final result capture is
independent of delivery loss, while 3.19.1 streaming reconstructs native events.

**The reported number differs from the iperf3 terminal summary.** Compare
the same direction, observation point, interval, and units in `result.raw`.
The [results guide](../guides/results.md) explains the convenience summary
and the current normalization limits.
