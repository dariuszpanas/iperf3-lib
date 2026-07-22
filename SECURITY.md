# Security policy

## Supported versions

Security fixes are released for the latest published `iperf3-lib` version.
Older `0.x` releases should be upgraded before reporting an issue that is
already fixed in the current release.

This package loads the system `libiperf`; it does not vendor or patch iperf3.
Use libiperf 3.19.1 or newer and follow
[upstream iperf security releases](https://github.com/esnet/iperf/releases).

## Reporting a vulnerability

Do not open a public issue with exploit details, credentials, private network
information, or other sensitive data.

Use **Security > Report a vulnerability** in this GitHub repository to open a
private security advisory. If private reporting is unavailable, contact the
maintainer through their GitHub profile and request a private channel without
including vulnerability details in the initial public message.

Include, when possible:

- the affected `iperf3-lib`, Python, operating-system, and libiperf versions;
- a minimal reproduction or failing test;
- expected impact and any known mitigations; and
- whether the issue has already been disclosed elsewhere.

Reports will be acknowledged as soon as practical. Please allow time to
validate native-library interactions and prepare coordinated fixes before
public disclosure.

## Scope

Issues in this wrapper, its packaging, or its release pipeline are in scope.
Vulnerabilities in iperf itself should also be reported to the
[upstream iperf project](https://github.com/esnet/iperf/security).
