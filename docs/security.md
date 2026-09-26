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
plan-wide traffic budgets. Native output can contain host addresses and test
metadata. Review what you retain or publish in metrics and reports.

Serialize native operations within one process. The async helpers do not add
cancellation or concurrency isolation; these are explicit items in the
[roadmap](roadmap.md).
