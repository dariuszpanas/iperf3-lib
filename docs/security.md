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

Serialize basic direct native calls within one process. Expanded controls and
explicit timeouts use isolated workers; cancelling an async await alone does
not stop its operation. See [execution limits](reference/compatibility.md#feature-boundaries).
