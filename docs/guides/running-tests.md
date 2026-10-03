# Running clients and servers

These examples use the APIs available in 0.3.0. Follow
[Getting started](../getting-started.md).

## Choose a protocol and direction

```python
from iperf3_lib import Client, ClientConfig, Protocol

result = Client(
    ClientConfig(
        server="127.0.0.1",
        protocol=Protocol.UDP,
        duration=5,
        rate=10_000_000,
        parallel=1,
    )
).run()
```

`rate` is a target in bits per second for each stream. With multiple streams,
the native rate limit applies independently to each one. `rate=0` disables
that limit. These are the upstream
[iperf3 bitrate semantics](https://software.es.net/iperf/invoking.html).

When `rate=None`, the wrapper sets UDP to 1,048,576 bits/s and leaves TCP/SCTP
at their native defaults. When `blksize=None`, UDP uses libiperf's dynamic
selection, SCTP uses 65,536 bytes, and TCP keeps its native default.

| Configuration | Traffic direction |
| --- | --- |
| Defaults | Client sends to server. |
| `reverse=True` | Server sends to client. |
| `bidirectional=True` | Both directions run simultaneously. |

`reverse` and `bidirectional` cannot both be enabled. Two separate forward and
reverse runs measure different conditions from one simultaneous bidirectional
run. Keep that distinction when comparing measurements.

All accepted fields, defaults, and bounds are in the
[configuration reference](../reference/configuration.md). SCTP requires both
the operating system and libiperf build to support SCTP.

## Integrate with asyncio

```python
import asyncio

from iperf3_lib import Client, ClientConfig


async def main() -> None:
    config = ClientConfig(server="127.0.0.1", duration=2)
    result = await Client(config).arun()
    print(result.ok, result.summary_mbps)


asyncio.run(main())
```

On current `main`, `arun()` always uses an isolated Python/CFFI worker. Cancelling
the awaiting task, including through an asyncio timeout, requests worker
termination and propagates `CancelledError` after confirmed cleanup. Unconfirmed
cleanup raises `IperfCleanupError`. This behavior is new after 0.3.0.
Pass `await Client(config).arun(timeout=10)` to set an independent worker deadline.
Repeated cancellation does not abandon cleanup. A callback already running must
return before the await finishes; the worker is still stopped independently.

Shutdown sends TERM, waits up to two seconds, then KILL if needed within a
four-second process cleanup budget. OS process creation and application
callbacks can extend the time until the Python call returns. If cleanup cannot
be confirmed, `IperfCleanupError` reports that failure instead of successful
cancellation. The library retains ownership for later reaping. No partial
measurement result is manufactured after forced termination.

Serialize basic direct native operations within a process. libiperf's global
error state and blocking operations are not treated as reentrant. Multiple
`Client` objects or separate executor threads do not provide isolation.
Async methods, expanded controls, MPTCP, streaming, explicit execution timeouts and event
callbacks select the Python/CFFI worker. See [native execution](native-controls.md#observe-events-and-bound-a-run)
for the path-selection and delivery contract; no `iperf3` subprocess is used.

## Handle failures

`Client.run()` and `Client.arun()` return `Result(ok=False, error=...)` when
libiperf reports an `IperfError` during the run or returns no JSON. Some errors
are raised instead:

| Error | Meaning |
| --- | --- |
| `TypeError`, `ValueError` from `ClientConfig` | A configuration value or combination is invalid. |
| `UnsupportedFeatureError` | The wrapper or loaded native library cannot apply a requested feature. |
| `IperfLibraryError` | Loading, allocation, or native setup verification failed. |
| `IperfError` raised by a worker | Native setup failed before a completed measurement was available. |
| `IperfCleanupError` | Worker cleanup could not be confirmed; the original cancellation/failure is retained as its cause. |
| JSON decoding errors or parser `ValueError` | The returned native JSON could not be decoded or normalized. |
| `TimeoutError` | The worker execution deadline expired; its process was terminated and reaped. |
| Callback exception | Delivery stopped; the exception propagates after the active run and worker shutdown. |

Async methods use their built-in isolated implementation rather than invoking
an override of `run()` or `run_once()`. Setup errors therefore follow worker
semantics: they raise, whereas some direct-client setup errors produce failed
results. Wrapping a synchronous method in your own executor does not stop its
native operation when its await is cancelled. Plan cancellation remains
unsupported.

Do not use `result.ok` as a performance acceptance decision: a completed test
can have low throughput or substantial loss. Choose application thresholds with
`AssessmentPolicy`, then use `assess_plan` for
[repeated trials and baseline assessment](trials.md).

## Worker lifetime on Linux

Library-launched Linux workers install a kernel parent-death signal before
importing the native worker. If the owning parent dies, the kernel sends
`SIGKILL` to the worker. Startup also verifies the expected parent identity;
if the parent has already disappeared or protection cannot be installed, the
worker exits before running libiperf. This behavior is new after 0.3.0.

Linux ties this signal to the thread that created the child. The library keeps
that thread alive until the operation and its cleanup finish. After parent
death, the worker's OS resources are released and the system's init process or
subreaper owns final reaping. Forced termination does not run native finalizers
or return a partial measurement to the vanished caller.

This protection covers the worker after its bootstrap executes. It does not
supervise arbitrary descendants or recover a stalled interpreter before
bootstrap. Other platforms retain their existing worker behavior without this
Linux lifetime guarantee. Normal cancellation and explicit deadlines continue
to use the [bounded shutdown sequence](#integrate-with-asyncio).

## Use the Python server wrapper

Run the server in a separate process from your client:

```python
from iperf3_lib import Server, ServerConfig

server = Server(config=ServerConfig(
    port=5201,
    bind_address="127.0.0.1",
    address_family="ipv4",
    idle_timeout_seconds=10,
))
result = server.run_once(timeout=15)
print(result.ok, result.reporting_role, result.error)
```

`bind_address` selects an address assigned to the local server. For a remote
network, substitute that interface's assigned IP. `bind_device` selects a device
name separately. The concise `Server(port=5201, bind_host="127.0.0.1")` form
remains available, with `bind_host` serving as an address alias. Do not combine
legacy constructor arguments with `config`. The
[configuration reference](../reference/configuration.md#server-configuration)
lists native bitrate/duration policies, authentication and all other controls.

`run_once()` returns a normalized server `Result`; `aserve_once()` returns the
same result through an executor thread. Server operations always use an isolated
Python worker. Native failures are retained in results; setup errors raise.
An idle exit with no JSON produces an incomplete result.
Cancelling `aserve_once()` requests worker termination and waits for cleanup.
The instance can be reused after cleanup and any active callback complete;
unconfirmed cleanup raises `IperfCleanupError` and keeps it unavailable until
the worker is reaped. `stop()` retains its cooperative between-test behavior.

For a bounded sequential session:

```python
def record_result(result):
    print(result.ok, result.reporting_role, result.error)


server.serve_forever(on_result=record_result, max_runs=3, timeout=60)
```

Each iteration creates and configures a fresh native test, then frees it before
`on_result` receives the attempt. The same worker process serves the sequential
session; there is no native-test reset/reuse. `on_result` receives failures too.
Without that callback, a native failed result raises `IperfError` from
`serve_forever()`. `max_runs` limits the attempt count. `timeout` covers
startup and the **whole serving session**, not 60 seconds per client. A failed
or incomplete attempt ends the session. Event callbacks can also be supplied
with `on_event`, which enables streaming automatically; callback errors propagate
after worker shutdown.

`stop()` prevents the next iteration but does not interrupt a blocked listener
or active test. Use an explicit timeout for a bounded session. The stop flag
is not reset: a later `serve_forever()` returns without starting a worker,
although one-shot calls remain available. Concurrent use of one `Server`
instance raises `RuntimeError`.

