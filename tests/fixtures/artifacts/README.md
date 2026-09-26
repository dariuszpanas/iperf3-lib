# Result artifact fixtures

These are retained schema-v1 writer outputs, not native input fixtures. Tests read
them directly and require semantic dict/JSON round trips. They are deliberately
not regenerated during testing: parser changes must not rewrite archived values.

Files beginning `v1-native-` contain the entire original JSON from the native
fixture identified by `extensions["org.iperf3-lib.fixture"].native_fixture`, plus
its normalized result. See `../native/README.md` for capture commands, versions,
endpoint roles, and bounded execution details. Producer version `0.2.0` is the
installed development package version at capture, separate from schema version 1.

- TCP bidirectional client: both flow directions and endpoint observations.
- TCP reverse server: server reporting role and reverse flow semantics.
- UDP bidirectional client: mixed native per-stream end summaries remain
  unattributed, with explicit diagnostics and availability evidence.
- TCP warm-up server: omitted and measured intervals, aggregate and stream scope.
- SCTP: supported native protocol with unavailable retransmits represented by
  explicit producer-specific evidence, rather than fabricated zero measurements.
- `v1-partial-zero-extensions.json`: synthetic, independently specified zero and
  missing measurements, malformed-field availability, unknown advisory code,
  requested/effective configuration, observed versus estimated timing, and
  namespaced extensions. Its raw malformed value is intentionally not reparsed.
- `v1-failed.json` and `v1-incomplete.json`: synthetic distinct outcomes with
  partial zero-throughput intervals and missing final measurements.
- `legacy-end-only.json`: the known unversioned development snapshot shape.
  Migration keeps its zero rate, makes interval scope unknown, and preserves its
  unverified completion only as an estimate. The regular v1 reader rejects it.

Field units are fixed by schema v1: bytes, seconds, bits per second, milliseconds
for jitter, percent for loss, and nonnegative integer counts. Sender/receiver
observations and aggregate/component stream values are distinct measurements.
The corpus is supplemented by round-trip tests over every native minimum/latest
fixture, including forward, reverse and bidirectional TCP/UDP/SCTP endpoints.
