# Isolated execution qualification

This maps [issue #36](https://github.com/dariuszpanas/iperf3-lib/issues/36) to
required evidence. It defines the audit, not a claim that a revision passed.
Use the exact candidate commit, retained distributions and current Actions results.

| Requirement | Contract | Required evidence |
| --- | --- | --- |
| Concurrent admission and contention | Invocation-local worker and aggregate target-rate caps, endpoint/user-resource exclusion and per-cell dependencies. Targets include streams, directions and warm-ups; wire traffic is measured separately. | Admission/race tests plus positive native traffic from overlapping workers, actual exclusions/rate-cap backfill and schema-v3 reservation histories. |
| Comparison methodology | Descriptive analysis by default; comparisons require a declared matched cohort/contention method. Native-setting compatibility alone is insufficient. | The [comparison method](../../docs/guides/concurrent-plans.md#compare-experiments-with-a-declared-contention-policy), complete histories and strict v1/v2 type boundaries. Unknown contention remains inconclusive; no automatic concurrent performance assessment is claimed. |
| Plan and consumer ownership | Cancellation owns every admitted child through cleanup. Callback failure suppresses delivery and propagates after shutdown. No published async iterator adds an abandonment contract. | Queued/startup/completion races, repeated cancellation, multi-worker cleanup failure retaining original owners, and callback failure after positive native traffic. |
| Shutdown and grace | Admission-only stops let peers finish; an optional overall deadline bounds that drain. Hard stops have zero natural-completion grace, then TERM/KILL cleanup with retained ownership on failure. | Deadline-after-admission-stop and first-cause tests; actual reaping/closed pipes. Cleanup-attempt budgets do not bound blocked callbacks or Python return time. |
| Partial outcomes | Completed results remain artifacts. Interrupted and exceptional trials retain bounded diagnostic events, with unstarted trials explicit. No native summary or C finalizer is invented after a kill. | Existing v1/v2 fixtures, strict v2/v3 reports and saved native report/hash round-trips with positive partial intervals. Sequential SIGKILL and real output-pipe interruption after positive traffic must retain the completed prefix, failed active trial and unstarted suffix over TCP and UDP. |
| IPC bounds | Versioned frames, identities/order, bounded queues/control capacity and terminal/EOF/exit validation. Native capture is outside wire/queue bounds. | Malformed, oversized, replayed/truncated and saturated channel tests, including interruption during incomplete output. |
| Worker lifetime/crashes | Linux bootstrap protects the native worker from parent death; init or a dedicated subreaper owns later reaping. | Idle/active TCP/UDP parent-death tests, startup races and native crashes with measured endpoint reuse. Protection begins at bootstrap. |
| Resource accounting/stress | Supported workers create no application subprocesses. Repeated normal/error/forced-exit paths release owned processes, descriptors and listeners. | PID/start-time identities, all-thread child observations, warmed supervisor FD/child inventories, pre-reaping return evidence and positive reuse. Parent-death reaping is measured separately. |
| Installed matrix | Both retained wheel and sdist execute the same contracts on supported Linux. | Python 3.12–3.14 × libiperf 3.19.1/3.21: twelve complete installed receipts tied to exact source/archive hashes, with no selected skip. |

## Test entry points

- `tests/test_cancellation_integration.py`: native operation cancellation.
- `tests/test_worker_lifetime_integration.py`: Linux parent-death behavior.
- `tests/test_worker_ipc_integration.py`: real pipe/worker transport faults.
- `tests/test_async_trials_integration.py`: owned sequential plan history,
  including retained evidence after active worker crashes and transport failures.
- `tests/test_concurrent_trials_integration.py`: concurrent admission, measured
  overlap, exclusions and multi-worker cleanup.
- `tests/test_resource_stress_integration.py`: repeated lifecycle paths with
  process, descriptor, child and socket-use evidence.
- `tests/_native_resource_inventory.py`: read-only `/proc` snapshots; fixture
  tests check identity parsing and disappearance/races without libiperf.

Child inventories are samples: Linux documents that concurrent exits can make
[`/proc` child observations incomplete](https://www.kernel.org/doc/html/latest/filesystems/proc.html#proc-pid-task-tid-children-information-about-task-children).
Each stress supervisor therefore acts as a subreaper and requires its final child
inventory to return to the empty baseline, catching surviving adopted descendants.
These checks qualify the supported worker paths; they do not establish general
supervision of application-created process trees. Reused PIDs must be
distinguished by kernel start time. FD counts alone are insufficient: retain
targets and identities so one leaked pipe/socket cannot be hidden by a different
closed descriptor. TCP `TIME_WAIT` is not an owned live-socket leak; verify closed
owned FDs, released listeners and successful measured reuse instead.

For ordinary API cleanup, assert consumed return codes and closed pipes **before**
a test-side `poll()` or `wait()` could hide a library failure. After parent death,
the dedicated subreaper is the legitimate reaping owner and reports that work
separately. Forced exits establish OS cleanup; normal/error native paths retain
exactly-once `iperf_free_test` assertions.

## Exact-candidate procedure

Run non-native quality/unit gates before Docker/native testing. The repository's
`make check`, `make commit-check`, `make change-check`, distribution build and
Docker-backed `make workflow-lint` remain required. Tree policies inspect committed
revisions; a dirty working-tree result cannot qualify a new commit's policies.

`scripts/qualify_lifecycle.py` seals package bytes, copied test/helper inputs and
the lockfile. The installed runner must import the retained distribution from its
isolated environment, execute every selected setup/call/teardown, and validate
measured receipts. Collection success, a passing subset or bare cleanup booleans
are insufficient.

Retain distributions, manifest, installed import/dependency identity, per-case
JSON, raw logs and workflow metadata. Independently compare archive package bytes
and harness hashes with the exact committed tree. For retained GitHub source
evidence, verify commit/tree identity and recompute Git object hashes first.
Never reuse an earlier candidate's receipts after package or harness changes.

Close issue #36 only when every row has current evidence. Package release and
publication remain separate decisions. Additional async assessment APIs, shared
pools, nonzero pauses and arbitrary descendant supervision are outside this
implemented contract.
