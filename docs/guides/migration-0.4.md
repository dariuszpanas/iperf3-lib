# Prepare an upgrade from 0.3.0

!!! note "Unreleased changes intended for 0.4.0"
    This guide describes changes on `main` after published 0.3.0. It does not
    establish that 0.4.0 has been released or extend the supported platform
    matrix. Check the [changelog](../changelog.md) and use documentation matching
    the source revision or package you install.

Existing dataclass configurations, synchronous runs, result artifacts,
assessments, sweeps, and Prometheus output remain available. You do not need to
repeat the [0.2-to-0.3 dataclass migration](migration-0.3.md). Review async
customizations and error handling first; the new experiment and progress APIs
are optional.

## Update async lifecycle assumptions

| Application behavior | Published 0.3.0 | Current `main` |
| --- | --- | --- |
| `await client.arun()` | Delegates to `run()` through an executor; basic runs can use direct native calls. | Always uses an isolated Python/CFFI worker. |
| Cancel `arun()` or `aserve_once()` | Cancels the await without stopping its executor operation. | Stops the owned worker and waits for cleanup before propagating cancellation. |
| Override `run()` or `run_once()` | The corresponding async method invokes that override. | Async methods use their built-in isolated path. |

Keep basic synchronous native calls serialized within a process. Moving a direct
call into `asyncio.to_thread()` does not give it owned cancellation. `Server.stop()`
remains cooperative between tests; it does not interrupt an active listener.

Review code that treats cancellation or a timeout as an immediate return.
`timeout=` bounds worker execution, including startup, but cleanup and an active
application callback can extend the time until the await returns. An outer
`asyncio.timeout()` instead cancels the awaiting task and can translate its
cancellation into `TimeoutError`.

Successful cleanup allows `CancelledError` or the worker's `TimeoutError` to
propagate. If cleanup cannot be confirmed, `IperfCleanupError` takes precedence
and retains the original cause. A server remains unavailable until its worker
is reaped. Do not suppress this error and report successful cancellation.
Forced termination produces no partial `Result` and does not prove that native
C finalizers ran. See the [execution contract](running-tests.md#integrate-with-asyncio)
for cleanup bounds and Linux worker lifetime.

Also handle raised worker setup errors, including `IperfError` and
`IperfLibraryError`; some basic synchronous client setup failures instead return
failed results. Completed native failures still return `Result(ok=False)`.
Handle both exceptions and unsuccessful results, and keep performance acceptance
separate from `result.ok`.

## Preserve application customizations explicitly

If a subclass used `run()` for logging, validation, or postprocessing, move that
logic into an explicit async wrapper or implement the async override too:

```python
import logging

from iperf3_lib import Client

logger = logging.getLogger(__name__)


class LoggedClient(Client):
    async def arun(self, *, timeout=None, on_event=None):
        result = await super().arun(timeout=timeout, on_event=on_event)
        logger.info("Native run completed: ok=%s", result.ok)
        return result
```

This preserves the built-in worker ownership and exception behavior. Keep a
separate `run()` override if synchronous callers need the same customization.
For a server, apply the same pattern to `aserve_once()` and await
`super().aserve_once(...)`. Calling the old synchronous override in an executor
would retain its old cancellation limitation.

## Choose new execution APIs deliberately

`run_plan()` remains synchronous and sequential. The new
[`arun_plan()`](trials.md#cancel-an-async-plan-and-retain-its-evidence) accepts
the same prepared plan but returns `PlanExecutionResult`, not the synchronous
`PlanResult`. Its cancellation, timeout, and cleanup exceptions expose
`partial_result`; retain that history before propagating the exception.
`PlanCancelledError` inherits `asyncio.CancelledError`, so `except Exception`
does not catch it. The guide shows explicit exception handling.

Do not switch runners while leaving `assess_plan(execution)` unchanged:
assessment-v1 and sweep-v1 do not accept owned async histories. Completed result
artifacts remain available for analysis under a declared comparison method.

Use [`arun_concurrent_plan()`](concurrent-plans.md) only for explicitly bounded
independent cells. It requires worker and aggregate target-rate limits and
zero pauses; ordinary async plans remain sequential. Its overlap history must
be considered when choosing a baseline; reservations alone do not prove traffic.
[`run_adaptive_udp()`](adaptive-udp.md) is a separate synchronous exploratory
API for confirmed tested UDP rates, not an exact capacity estimate or an async
replacement for finite sweeps.

## Keep archive families distinct

Select readers by both `kind` and `schema_version`. Module names below are
relative to `iperf3_lib`; schema numbers are not package versions.

| Saved evidence | `kind`, `schema_version` | Reader |
| --- | --- | --- |
| [Result artifact](artifacts.md) | `iperf3-lib.result`, `1` | `artifacts.loads_artifact()` |
| [Assessment](trials.md) | `iperf3-lib.assessment`, `1` | `reports.loads_report()` |
| [Sweep](sweeps.md) | `iperf3-lib.sweep`, `1` | `sweep_reports.loads_sweep_report()` |
| [Sequential async execution](trials.md#cancel-an-async-plan-and-retain-its-evidence) | `iperf3-lib.plan-execution`, `2` | `plan_reports.loads_plan_report()` |
| [Concurrent execution](concurrent-plans.md) | `iperf3-lib.plan-execution`, `3` | `concurrent_reports.loads_concurrent_report()` |
| [Adaptive UDP experiment](adaptive-udp.md) | `iperf3-lib.adaptive-udp`, `1` | `adaptive_reports.loads_adaptive_udp_report()` |

The three v1 formats published with 0.3.0 retain their meanings and remain
readable. Do not renumber them or strip evidence to fit a different reader.
Earlier **unreleased** plan-v2 readers reject newer exception records carrying
events; upgrade those readers before consuming such reports. This is separate
from migrating published 0.3.0 archives. Strict loaders validate saved evidence
without running benchmarks.

## Adopt typed progress only when needed

Existing `on_event` callbacks still receive the four-field `NativeEvent` with
`kind`, `data`, `sequence`, and wall-clock `received_at_seconds`. They do not
become `LiveEvent` callbacks.

For typed progress, use `Client.events()` or `Server.events_once()` inside
`async with`, then obtain the same operation's result with `await stream.result()`.
Context exit handles early abandonment and awaits cleanup. Native `end` is not
wrapper completion; check the final result's status. Progress loss can coexist
with a complete result, while lost reconstruction evidence can make a result
incomplete. Preserve final results with the artifact API: live events are runtime
objects, not another archive schema. See the [live-event guide](live-events.md)
for ownership, terminal events, and capture diagnostics.
