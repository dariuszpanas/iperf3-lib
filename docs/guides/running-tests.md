# Running clients and servers

These examples target the development version described in
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

`arun()` moves the blocking native call to an executor thread. Cancelling the
awaiting task, including through an asyncio timeout, does not terminate the
native run. A task's cancellation is therefore not evidence that the native
library is idle.

Serialize native operations within a process. libiperf's global error state
and blocking operations are not treated as reentrant. Multiple `Client`
objects or separate executor threads do not provide isolation. Applications
needing concurrent runs must use separate processes; the library does not yet
provide a managed process execution API.

## Handle failures

`Client.run()` and `Client.arun()` return `Result(ok=False, error=...)` when
libiperf reports an `IperfError` during the run or returns no JSON. Some errors
are raised instead:

| Error | Meaning |
| --- | --- |
| `TypeError`, `ValueError` from `ClientConfig` | A configuration value or combination is invalid. |
| `UnsupportedFeatureError` | The wrapper or loaded native library cannot apply a requested feature. |
| `IperfLibraryError` | Loading, allocation, or native setup verification failed. |
| JSON decoding errors or parser `ValueError` | The returned native JSON could not be decoded or normalized. |

Do not use `result.ok` as a performance acceptance decision: a completed test
can have low throughput or substantial loss. Thresholds and repeated-run
acceptance remain application decisions.

## Use the Python server wrapper

Run the server in a separate process from your client:

```python
from iperf3_lib import Server

server = Server(port=5201, bind_host="127.0.0.1")
server.run_once()
```

`run_once()` blocks for one test and returns `None`. `aserve_once()` runs the
same operation in an executor thread and also returns `None`; it has the same
cancellation limitation as `Client.arun()`.

For sequential clients, `serve_forever()` reuses one native test object across
iterations. `stop()` sets a cooperative flag checked between iterations. It
does not interrupt a server blocked waiting for a client or handling a test.
The flag is not reset by subsequent calls: create a new `Server` if you need a
fresh serving loop after stopping one.

