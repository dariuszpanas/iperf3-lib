# Native controls and execution

Use typed configuration for network and transport controls, and explicit
execution arguments for worker lifetime and event delivery. The
[complete native option inventory](../reference/native-options.md) accounts for
both supported libiperf versions, including aliases and CLI-only facilities.

The repository includes a runnable
[native-controls example](https://github.com/dariuszpanas/iperf3-lib/blob/main/examples/native_controls.py)
with explicit destination, local binding, aggregate rate, optional event notices,
a worker timeout and optional artifact output. Importing it generates no traffic;
running it starts one benchmark against the requested server.

## Select local addresses and devices

`server` identifies the remote destination. `bind_address` identifies an address
assigned to the local client host. `bind_device` names a local network device.
These controls answer different questions; selecting a device does not create
an address or route on it.

```python
from iperf3_lib import Client, ClientConfig

config = ClientConfig(
    server="127.0.0.1",
    bind_address="127.0.0.1",
    address_family="ipv4",
    duration=2,
    connect_timeout_ms=2_000,
)
result = Client(config).run(timeout=10)
print(result.ok, result.error)
```

For a particular non-loopback interface, replace the local address with an IP
actually assigned to that host. On a compatible Linux host, `bind_device="eth0"`
selects that named device; substitute its real name. Native support and
permissions still apply. Use explicit address/device fields instead of relying
on CLI `%device` shorthand.

`client_port` sets a data-stream source port, not the server's listening port or
the source port of the control connection. Reserve enough ports for the chosen
parallelism and avoid simultaneous reuse by other tests. Setting `parallel`
controls streams inside one native test; it does not make separate calls in the
same process reentrant.

For IPv6, choose `address_family="ipv6"` and IPv6 endpoints. A configured
`flow_label` requires TCP and that explicit family. Scoped/link-local addressing also
depends on the local device and native resolver; an IPv6 literal alone is not
a claim of available IPv6 connectivity.

The Python server's `bind_host` similarly selects a **local address**, not a
device name. See [server configuration](../reference/configuration.md#server-configuration)
for `ServerConfig` and named-device controls, and the
[server example](running-tests.md#use-the-python-server-wrapper).

## Tune TCP deliberately

```python
config = ClientConfig(
    server="127.0.0.1",
    duration=2,
    socket_buffer_bytes=131_072,
    no_delay=True,
    mss=1_200,
    interval_seconds=0.5,
)
result = Client(config).run(timeout=10)
print(result.raw)
```

Socket-buffer sizes are requests; operating systems may cap or transform them.
Compare native requested and observed buffer fields instead of treating the
configuration value as a measured TCP window. `congestion_control="cubic"` is
an example only for a kernel that offers that algorithm; unavailable selections
must be reported as failures.

`no_delay` and `mss` accept TCP or SCTP; SCTP requires `mss >= 512`.
`congestion_control` and `mptcp` require TCP.
`mptcp=True` uses the worker and requires a compatible native build and kernel.
Neither a recognized native flag nor a successful TCP fallback establishes that
multiple MPTCP paths carried traffic.

## Choose duration, bytes, or blocks

```python
byte_test = ClientConfig(
    server="127.0.0.1",
    duration=None,
    bytes_to_send=1_000_000,
)
block_test = ClientConfig(
    server="127.0.0.1",
    duration=None,
    blocks_to_send=100,
    blksize=4_096,
)
result = Client(byte_test).run(timeout=10)
```

Choose exactly one termination mode. Count-based runs require `duration=None`;
setting a count alongside the default duration is an error. A transfer target
is not an exact observed-byte assertion: protocol framing, complete native
blocks, omitted traffic and direction affect the measured scope. Inspect the
returned endpoint summaries.

`duration=0` requests an unlimited native test. Use an explicit execution timeout
when that mode is intended. A measured duration, a native connection/receive
timeout and the worker's total execution timeout are separate controls.

Finite [trial plans](trials.md) and [sweeps](sweeps.md) have their own admission
contract. Do not infer a finite active-time estimate from a byte or block count,
or assume that a plan budget bounds a running native call.

## Control offered load and UDP

```python
from iperf3_lib import Protocol
from iperf3_lib.intent import RateIntent, parse_rate

config = ClientConfig(
    server="127.0.0.1",
    protocol=Protocol.UDP,
    address_family="ipv4",
    duration=2,
    parallel=2,
    blksize=1_200,
    udp_counters_64bit=True,
    dont_fragment=True,
    pacing_timer_us=1_000,
)
intent = RateIntent(aggregate_bps_per_direction=parse_rate("10 Mbit/s"))
result = Client(config, rate_intent=intent).run(timeout=10)
```

`rate` remains a per-stream target. Aggregate intent divides one direction's
target across streams, preserving any unused remainder. `burst_packets` changes
the send pattern; `pacing_timer_us` changes the internal timer. `fq_rate_bps`
requests additional TCP/UDP socket-level pacing, where supported; SCTP rejects
that field, including zero. These requests do not
guarantee achieved rates or hard wire-traffic ceilings.

`dont_fragment` applies to IPv4 UDP; automatic family selection becomes IPv4 and
explicit IPv6 is rejected. `gsro=True` requests UDP GSO/GRO and requires
libiperf 3.21; do not assume that both peers or their kernels can use offload.
Keep offload and payload settings consistent when comparing trials.

For numeric DSCP with zero ECN bits, convert explicitly:

```python
dscp = 46
config = ClientConfig(server="127.0.0.1", tos=dscp << 2, duration=2)
```

DSCP is six bits; `tos` is the entire eight-bit field. Validate application input
before shifting, and do not pass symbolic CLI names as `tos`.

## Preserve native output and payload intent

```python
config = ClientConfig(
    server="127.0.0.1",
    duration=2,
    get_server_output=True,
    extra_data="branch-office-baseline-v1",
    title="upload",
    repeating_payload=True,
)
result = Client(config).run(timeout=10)
print(result.raw)
```

The remote server determines the returned server-output format. Preserve its
native JSON/text without assuming a second normalized `Result` is available.
Use namespaced `result.extensions` for application artifact metadata; native
`extra_data` and `title` have separate meanings.

`payload_file` is a local file source or sink according to the native role and
direction. It is mutually exclusive with `repeating_payload`; it is not a
general-purpose or integrity-checked file transfer. `zerocopy` selects a TCP
send strategy; `skip_rx_copy` supports TCP/UDP receive-copy avoidance. SCTP
rejects both. These native facilities depend on the build/platform and may affect
comparability.

## Observe events and bound a run

```python
from iperf3_lib.events import NativeEvent


def on_event(event: NativeEvent) -> None:
    print(event.sequence, event.kind, event.received_at_seconds)


config = ClientConfig(server="127.0.0.1", duration=2, json_stream=True)
result = Client(config).run(timeout=10, on_event=on_event)
```

`NativeEvent` contains `kind`, copied `data`, a `sequence`, and
`received_at_seconds`. The receipt time is not the interval's native measurement
boundary. The callback runs in the calling Python thread (`arun()` uses its
executor thread), never inside a native C callback. Both sides of the transport
use queues with capacity 256. Keep callbacks short; sequence gaps and
`result.extensions["iperf3_lib.event_delivery"]` report delivery loss through
`emitted`, `dropped`, and `queue_capacity`. A complete result remains the basis
for artifacts and final analysis; callbacks do not replace it. Native 3.21
streaming also enables its full-output facility. Native 3.19.1 has no such
facility: the wrapper explicitly labels `raw` as `reconstructed_events` and
retains the original event envelopes. Fields absent from those envelopes cannot
be represented as a complete original native document.

The marker and envelopes are in
`result.extensions["iperf3_lib.native_json"]` as `representation` and `events`.
The `execution.reconstructed_json` diagnostic identifies reconstructed capture.
Full native-document capture needs no reconstruction extension. Bounded live
delivery queues do not bound memory used for the retained intervals/result.

After a callback raises, further delivery stops. The active native run is
allowed to finish, and the callback error is then raised. An explicit timeout
terminates and reaps the worker and raises `TimeoutError`. Event loss does not
change the independently captured result evidence.

An independent watchdog terminates/reaps the worker at its deadline even if a
Python callback is blocked. The caller still cannot regain control until that
callback returns. Keep user work short or hand it off to an application-owned queue.

Async methods always use an isolated worker and propagate task cancellation
after confirmed cleanup; unconfirmed cleanup raises `IperfCleanupError`. A
callback already running must return before the await finishes. See
[async cancellation](running-tests.md#integrate-with-asyncio) for cleanup bounds
and failure behavior; this support is new after 0.3.0.

An explicit `timeout` or `on_event` callback selects the isolated Python/CFFI
worker even for a basic client. `json_stream=True` also selects that path.
The worker uses the installed Python package and shared library, not an
`iperf3` executable. Native parser and process-global state remain inside its
process. Basic direct calls still require serialization within the caller's
process; starting multiple ordinary clients does not select isolation by itself.

The execution timeout bounds the worker operation. Native
`connect_timeout_ms`, `receive_timeout_ms`, `send_timeout_ms`, and
`control_keepalive` affect particular connection states and do not provide the
same lifetime guarantee. Consult [the API contract](../reference/api.md) for
timeout, callback-failure and async behavior. An `asyncio.wait_for` around an
application-owned executor does not cancel a direct native call.

## SCTP and host-specific settings

`sctp_streams` and `sctp_bind_addresses` configure SCTP associations, not TCP
streams. They require `protocol=Protocol.SCTP` and a compatible build/kernel.
`sctp_bind_addresses` is a tuple of address strings; address-family constraints
remain explicit.

`affinity` selects a local CPU inside the worker. `server_affinity` additionally
requests a CPU at the remote endpoint and requires an explicit local affinity.
CPU numbering and the CPUs allowed to a container or service vary by host.
Keep those settings in benchmark methodology instead of copying a fixed CPU
number between machines.

Authentication uses explicit client/server configuration and native OpenSSL
support. Set client `username` and `rsa_public_key_path` together, then supply
the password separately with `Client(config, password=...)` or the
`IPERF3_PASSWORD` environment variable. Passwords are not `ClientConfig` fields
and are not included in artifacts; username and key path remain request
metadata, while key contents are absent. Do not put reusable credentials in
`extra_data`, artifact extensions, labels or event handlers. See the complete
[configuration contract](../reference/configuration.md) before enabling it.
Client `use_pkcs1_padding=True` is rejected with libiperf 3.21, whose legacy
padding flag is server-only. The flag is accepted for clients with libiperf
3.19.1; matching authentication support is still required at both endpoints.
The tested 3.19.1/OpenSSL 3 build fails authentication even with valid
credentials because of an upstream encryption bug. Use the qualified 3.21
build for authenticated benchmarks; see the
[native authentication limitation](../reference/compatibility.md#authentication-compatibility)
for the reproduced behavior and build-specific scope.

## Interpret applied-setting evidence

The result extension `iperf3_lib.native_configuration` records available
`{field: {getter, value}}` receipts. A matching getter confirms the native stored
request; it does not prove a kernel-applied buffer, an achieved pacing rate or
network-device behavior. Effective-setting provenance must retain that
distinction. Existing artifacts containing only the original configuration
fields remain readable; new requests preserve the expanded fields.

A configured advanced option also affects
[comparison eligibility](analysis.md#advanced-configuration-in-comparisons).
Matching requests do not establish matching native settings: every compared
trial needs the relevant returned-value or getter receipts, including a peer
that requested defaults.
