# Compatibility and native setup

## Tested targets

| Component | Project coverage |
| --- | --- |
| Python | CPython 3.12, 3.13, 3.14, 3.15 with the standard GIL. |
| CFFI | `cffi>=2.0,<3` on Python 3.12–3.14; `cffi>=2.1.1,<3` on Python `>=3.15`. |
| libiperf | 3.19.1 minimum; 3.22 recommended default. CI tests both endpoints. |
| Operating system | Linux `x86_64` (`glibc`). |
| Protocols | TCP, UDP, SCTP; SCTP also requires operating-system and native-library support. |

The package declares Python `>=3.12`; the versions and architecture listed above
define the tested runtime envelope. Although the distribution is a pure-Python
`py3-none-any` wheel, that wheel tag does not imply native qualification on
`aarch64`, `musl` (Alpine), macOS, FreeBSD, Windows, or free-threaded
(`Py_GIL_DISABLED`) Python builds. macOS and FreeBSD are unverified by this
project, and Windows DLL loading is best-effort with no native CI coverage.

Python 3.15 final is included in stable CI and release qualification against
both native endpoints, including installed wheel and sdist checks. The package
uses one `py3-none-any` wheel across Python versions; `Requires-Python: >=3.12`
is unchanged. Python 3.15 requires CFFI 2.1.1 or newer: the
[CFFI 2.1.1 release](https://github.com/python-cffi/cffi/releases/tag/v2.1.1)
fixes an ABI incompatibility introduced in Python 3.15.0b4. Python 3.12–3.14
retains the CFFI 2.0 minimum. Runtime compatibility also depends on the native
library. Free-threaded Python builds are outside the tested matrix.

The earlier Python 3.15.0rc3 preview workflow remains available for manual
historical diagnostics. Its receipts do not qualify the final interpreter or
replace the current release matrix.

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
| Async convenience | Always isolated; cancellation waits for worker cleanup and any active callback. Unconfirmed cleanup raises `IperfCleanupError` (introduced in 0.4.0). |
| Concurrent native operations | Basic direct calls remain non-reentrant. Expanded worker calls isolate native state; ordinary client construction alone does not select isolation. |
| Bounded concurrent plans | Opt-in isolated workers with explicit target-rate/worker ceilings, endpoint and caller-resource exclusions, zero pauses and no fixed client ports. See [concurrent plans](../guides/concurrent-plans.md). |
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

Use libiperf 3.22 for authenticated benchmarks. The tested 3.19.1/OpenSSL 3.5.7
combination rejects valid credentials with an authentication failure and
`output buffer too small`. The failure also occurs with the upstream CLI.

The [3.19.1 authentication implementation](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_auth.c)
passes a zero output length to OpenSSL's encryption call; the
[3.21 implementation](https://github.com/esnet/iperf/blob/3.21/src/iperf_auth.c)
initializes that length to the allocated buffer size. Treat authenticated
operation with that 3.19.1/OpenSSL build as a known native limitation.
Other native builds may differ.

The [3.22 release](https://github.com/esnet/iperf/releases/tag/3.22) also fixes
server use-after-free, authentication memory errors, invalid test parameters,
and a UDP loop condition. It is the current recommended native release;
retaining 3.19.1 in CI preserves minimum-version compatibility. Installing or
upgrading the Python package does not apply these native fixes.

The wrapper preserves native authentication access and a failed `Result`;
it does not reject every 3.19.1 build or replace the native cryptographic
implementation.
Client `use_pkcs1_padding=True` is rejected with 3.21+ because that native flag
is server-only; leave it at its default for a 3.22 client.

## Troubleshooting

**The package imports, but running a client fails to load libiperf.** Check
the shared-library installation, architecture, native dependencies, and
`IPERF3_LIB`. A Python import only checks the Python package because native
loading is deferred.

**A requested feature raises `UnsupportedFeatureError`.** Check the wrapper
feature table and loaded library version. Symbol availability, wrapper
support, and a successful test are separate checks.

**An asyncio timeout expires, but traffic continues.** Version 0.3.0 leaves the
executor operation running when its await is cancelled. Version 0.4.0 and later make
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

## Compatibility, deprecation, and archive policy

### Public API boundary

The supported public surface consists of the symbols documented in the
[Python API reference](api.md) and exported by `iperf3_lib` and its documented
public submodules (`config`, `server_config`, `result`, `artifacts`, `analysis`,
`trials`, `assessments`, `async_trials`, `plan_execution`, `plan_reports`,
`concurrent_trials`, `concurrent_execution`, `concurrent_reports`, `sweeps`,
`sweep_reports`, `adaptive`, `adaptive_execution`, `adaptive_reports`, `events`,
`live_events`, `intent`, `capabilities`, `native_options`, `reports`,
`exceptions`, and `exporters.prometheus`). Documented signatures, default
values, measurement units, missing-value semantics (`None` vs. measured `0`),
exception inheritance, and worker cleanup rules belong to that contract.

Private `_`-prefixed modules and helpers, worker IPC wire framing, internal
CFFI handles, process IDs, and exact human-readable error strings are internal
implementation details and may change between releases.

### Versioning and deprecation

- **Patch releases** preserve documented valid usage.
- **Minor releases** add backward-compatible APIs, options, or diagnostic codes.
  While the project is in `0.x` Beta, any intentional incompatible change in a
  minor release is accompanied by explicit migration instructions.
- **From 1.0.0 onward**, incompatible changes to the documented public API
  require a major release under [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
  Deprecations document the replacement, emit a `DeprecationWarning` at the
  affected public entry point, and remain supported for at least one minor
  release before removal in a major release. Urgent security mitigations
  provide an explicit notice and never silently alter archived measurements.
- Consumers inspecting `Diagnostic.code` or `AnalysisDiagnostic.code` should
  tolerate newly added advisory codes in minor releases.

### Data ownership and mutation boundaries

- `Client.run()`, `Client.arun()`, and `EventStream` snapshot and detach
  `ClientConfig` and `ServerConfig` on admission so subsequent caller mutation
  does not alter an admitted operation.
- `result_from_iperf_json()` retains the caller's `raw` mapping on the returned
  `Result`. Treat that input mapping as read-only after normalization, or pass
  a deep copy if your application mutates it later.
- `artifact_from_result()`, `loads_artifact()`, and `LiveEvent` delivery detach
  their JSON payloads at the conversion boundary so consumer edits do not alias
  producer state.

### Versioned archive and report schemas

Archive schema versions are independent of the Python package version. Readers
validate both `kind` and `schema_version`:

| Kind | Schema version | Module |
| --- | --- | --- |
| `iperf3-lib.result` | `1` | `iperf3_lib.artifacts` |
| `iperf3-lib.assessment` | `1` (`median-summary-v1`) | `iperf3_lib.reports` |
| `iperf3-lib.sweep` | `1` (`sweep-summary-v1`) | `iperf3_lib.sweep_reports` |
| `iperf3-lib.plan-execution` | `2` (`sequential-owned`) | `iperf3_lib.plan_reports` |
| `iperf3-lib.plan-execution` | `3` (`bounded-concurrent`) | `iperf3_lib.concurrent_reports` |
| `iperf3-lib.adaptive-udp` | `1` (`conservative-tested-rates-v1`) | `iperf3_lib.adaptive_reports` |

Published archive schemas remain readable across minor updates without loading
`libiperf`, re-running benchmarks, or substituting a newer analysis algorithm.
Unknown schema versions raise `UnsupportedArtifactVersion` or
`UnsupportedReportVersionError`.

### Release documentation snapshots

This site tracks `main`. To read documentation matching a specific published
release tag, browse the immutable tagged tree in GitHub:

- [v0.4.1 documentation](https://github.com/dariuszpanas/iperf3-lib/tree/v0.4.1/docs) ([release notes](https://github.com/dariuszpanas/iperf3-lib/releases/tag/v0.4.1))
- [v0.4.0 documentation](https://github.com/dariuszpanas/iperf3-lib/tree/v0.4.0/docs) ([release notes](https://github.com/dariuszpanas/iperf3-lib/releases/tag/v0.4.0))
- [v0.3.0 documentation](https://github.com/dariuszpanas/iperf3-lib/tree/v0.3.0/docs) ([release notes](https://github.com/dariuszpanas/iperf3-lib/releases/tag/v0.3.0))
- [v0.2.0 README](https://github.com/dariuszpanas/iperf3-lib/blob/v0.2.0/README.md) ([release notes](https://github.com/dariuszpanas/iperf3-lib/releases/tag/v0.2.0))
