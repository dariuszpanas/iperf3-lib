# Security

`iperf3-lib` loads the system `libiperf`; it does not bundle or patch the native
library. Use the supported baseline or newer and follow
[upstream iperf security releases](https://github.com/esnet/iperf/releases).
Linux is the tested platform; see [compatibility](reference/compatibility.md).

## Report privately

Use the repository's **Security → Report a vulnerability** to report a suspected
vulnerability. If private reporting is unavailable, request a private channel
through the maintainer's GitHub profile. Include affected versions, a minimal
reproduction, and impact where possible. Keep exploit details, credentials,
and private network information out of public issues.

The [repository security policy](https://github.com/dariuszpanas/iperf3-lib/blob/main/SECURITY.md)
defines supported versions and the reporting process. Upstream native-library
issues should also be reported to [ESnet](https://github.com/esnet/iperf/security).

## Operational boundary

Benchmarks generate network traffic. Applications choose the target, duration,
protocol, and stream count; the wrapper does not authorize targets or provide
enforced network traffic limits. Native output can contain host addresses and test
metadata. Review what you retain or publish in metrics and reports.

Serialize basic direct native calls within one process. Async methods, expanded
controls and explicit timeouts use isolated workers. On current `main`, cancelling
`arun()` or `aserve_once()` requests worker termination and waits for cleanup and
any active callback. Unconfirmed cleanup raises `IperfCleanupError`; the library
retains ownership of the worker. This support is new after 0.3.0. Cancelling an
application-owned executor wrapper around a synchronous call does not stop its
operation. See [execution limits](reference/compatibility.md#feature-boundaries).

On Linux, library-launched workers install a parent-death signal before native
execution. Parent death kills that worker and releases its OS resources; final
reaping belongs to init or a subreaper. This is a worker lifetime mechanism,
not a sandbox for native code or a supervisor for arbitrary descendants. See
[worker lifetime](guides/running-tests.md#worker-lifetime-on-linux) for startup
and platform boundaries.
