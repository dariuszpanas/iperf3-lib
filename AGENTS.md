# Repository agent guide

## Start here

- Read `.vault/iperf3-lib-vault/Home.md` when the local vault is present.
- Before branch-sensitive edits or repository-status claims, fetch `origin` and compare with `origin/main`.
- Keep the vault local and ignored. Update `Agent Handoff.md` and `Validation Log.md` after material work.

## Engineering rules

- Every accepted `ClientConfig` field must be applied to libiperf exactly once or rejected explicitly.
- Verify native behavior from returned JSON or a matching getter; mock call counts are not sufficient for integration coverage.
- Preserve the lifecycle invariant: each non-null `iperf_test` is freed exactly once, including error paths.
- Treat libiperf's process-global error state and blocking calls as non-reentrant. Do not promise concurrent execution or cancellation without an isolation design.
- Supported targets are documented in the vault and README. Keep the minimum and latest libiperf versions in CI.

## Validation

Run the non-native quality and unit gates first, then the Docker/native integration matrix. Record exact commands and outcomes in the vault rather than relying on historical green checks.
