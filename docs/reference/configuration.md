# Configuration reference

For aggregate rate targets, explicit units, plan estimates, and capability
inspection, see [rate intent and capabilities](../guides/configuration-intent.md).
`ClientConfig.rate` remains the low-level **per-stream** bits/s setting.
The [native option inventory](native-options.md) maps every supported-version
CLI flag to a Python facility or an explicit application concern; the
[control recipes](../guides/native-controls.md) show practical combinations.

Since 0.3.0, `ClientConfig` is a mutable standard-library dataclass.
Its constructor validates types, bounds, and option combinations.
Pydantic APIs such as `model_validate()` and `model_dump()` are not provided.

```python
from dataclasses import asdict, replace

from iperf3_lib import ClientConfig, Protocol

config = ClientConfig(server="127.0.0.1", protocol=Protocol.TCP, duration=5)
reverse_config = replace(config, reverse=True)
print(asdict(config))
```

Use a fresh instance or `dataclasses.replace()` when changing a validated
configuration. Direct attribute assignment does not rerun validation, and a
`Client` retains the configuration object passed to it. Each `run()` validates
and executes a detached snapshot at admission, so later mutations cannot change
an active run. The result records that request separately from settings verified
through native output.

## Client fields

| Field | Default | Accepted values and behavior |
| --- | --- | --- |
| `server` | Required | Hostname/address string or standard-library `IPv4Address`/`IPv6Address` object. Name resolution occurs in libiperf. |
| `port` | `5201` | Integer, 1–65,535. |
| `protocol` | `Protocol.TCP` | `Protocol.TCP`, `Protocol.UDP`, `Protocol.SCTP`, or the exact strings `"tcp"`, `"udp"`, `"sctp"`. Strings normalize to the enum. |
| `duration` | `10` | Measured-test seconds, 0–86,400; zero requests unlimited duration. Use `None` with a byte/block target. This is not an execution deadline. |
| `parallel` | `1` | Integer stream count, 1–128. |
| `omit` | `0` | Integer initial omitted period in seconds, 0–600. |
| `reverse` | `False` | Boolean; sends from server to client. |
| `bidirectional` | `False` | Boolean; sends in both directions simultaneously. Requires native support. |
| `mptcp` | `False` | Request MPTCP through the isolated worker; TCP/native/kernel support required. |
| `blksize` | `None` | Integer bytes, 1–1,048,576; UDP narrows this to 16–65,507. `None` chooses protocol-specific defaults. |
| `rate` | `None` | Integer target bits/s per stream, 0–18,446,744,073,709,551,615. Zero disables the native rate limit. `None` chooses protocol defaults. |
| `tos` | `None` | Integer traffic-class/TOS value, 0–255. Nonzero values require TCP or UDP; SCTP accepts only `None` or `0`. |
| `json_stream` | `False` | Enable isolated native event capture. `run(on_event=...)` also enables it. |

Integer fields reject booleans, floats, and numeric strings. Boolean options
require actual `bool` values. Interval fields explicitly accept finite numbers.
An invalid type raises `TypeError`; an out-of-range value or invalid
combination raises `ValueError`. `reverse=True` with `bidirectional=True` is
invalid.

The `server` annotation remains `str`, although runtime construction also
accepts standard-library IP address objects. Passing `str(address)` is
compatible with both the runtime and that static annotation. The constructor
does not validate whether a hostname is reachable.

### Local endpoints and TCP

All optional fields below default to `None`, except `address_family="auto"`
and `no_delay=False`.

| Field | Meaning and validation |
| --- | --- |
| `bind_address` | Nonempty, NUL-free local address string. |
| `bind_device` | Nonempty, NUL-free local device-name string; separate from address binding. |
| `client_port` | Data-stream source port, 1–65,535. |
| `address_family` | `"auto"`, `"ipv4"`, or `"ipv6"`. |
| `socket_buffer_bytes` | Requested socket-buffer bytes, 1–536,870,912; observed kernel sizes may differ. |
| `congestion_control` | Nonempty, NUL-free TCP algorithm name. |
| `no_delay` | Boolean TCP/SCTP no-delay request. |
| `mss` | TCP maximum segment size, 1–32,767 bytes; SCTP narrows this to 512–32,767. |
| `connect_timeout_ms` | Initial control-connection timeout in milliseconds. |

`mptcp` and `congestion_control` require TCP; `no_delay` and `mss` accept TCP or
SCTP. A configured algorithm or device must also exist on the executing host. Strings are validated
before crossing the native boundary; this does not establish reachability,
permissions, or kernel support.

### Termination, intervals and pacing

These optional fields default to `None`.

| Field | Meaning and validation |
| --- | --- |
| `bytes_to_send` | Positive byte target, at most `2**64 - 1`; requires `duration=None`. |
| `blocks_to_send` | Positive block target, at most `2**64 - 1`; requires `duration=None`. |
| `interval_seconds` | `0` disables periodic statistics; otherwise 0.1–60 seconds. |
| `pacing_timer_us` | Positive configured native pacing interval in microseconds; an observed getter does not prove scheduler behavior. |
| `fq_rate_bps` | TCP/UDP socket-pacing target, 0–`2**53 - 1` bits/s. Zero disables it; SCTP rejects this field. |
| `burst_packets` | Native send-batch count, 1–1,000; rate limiting remains a separate setting. |

Exactly one of duration, bytes, and blocks selects termination. Native count
termination is not an exact final-byte guarantee, nor a duration estimate.
Count-based and unlimited runs have unknown active-time/payload estimates;
finite estimate caps reject unknown totals. Finite trial plans reject
`duration=0`. Count-based trials require uncapped estimate budgets, while sweeps
retain their finite active-time requirement.

### Protocol, payload and host settings

Boolean fields default to `False`; other fields default to `None`, except the
empty tuple default for `sctp_bind_addresses`.

| Field | Meaning and validation |
| --- | --- |
| `zerocopy` | Request the native TCP zero-copy send path; rejected for UDP/SCTP. |
| `skip_rx_copy` | Request native TCP/UDP receive-copy avoidance; rejected for SCTP. |
| `udp_counters_64bit` | Use UDP 64-bit packet counters. |
| `dont_fragment` | Request IPv4 UDP don't-fragment behavior. |
| `gsro` | UDP GSO/GRO request; requires native 3.21+. |
| `flow_label` | IPv6 TCP flow label, 1–1,048,575; requires TCP and `address_family="ipv6"`. |
| `sctp_streams` | SCTP stream count, 1–65,535. |
| `sctp_bind_addresses` | Tuple of nonempty NUL-free SCTP address strings. |
| `payload_file` | Nonempty NUL-free path to a local native source/sink file. |
| `repeating_payload` | Use the native repeating payload. |
| `affinity` | Local worker CPU selection, 0–1,024. |
| `server_affinity` | Requested remote CPU, 0–1,024; requires an explicit local `affinity`. |
| `get_server_output` | Request remote server output in the retained native result. |
| `title` | Nonempty NUL-free native output title. |
| `extra_data` | Nonempty NUL-free native JSON metadata. |

UDP-specific fields require UDP; SCTP association fields require SCTP.
`dont_fragment` rejects IPv6 and changes automatic family selection to IPv4.
`payload_file` and `repeating_payload` cannot be combined. Host-specific options
still require native build, operating-system and peer support. Choosing a CPU
or file happens inside the worker but refers to resources on the executing host.

### Connection lifetime and authentication

These fields default to `None`, except `use_pkcs1_padding=False`.

| Field | Meaning |
| --- | --- |
| `receive_timeout_ms` | Native idle receive timeout, 100–86,400,000 milliseconds. |
| `send_timeout_ms` | Native unacknowledged TCP-data timeout, 0–86,400,000 milliseconds, where available. |
| `control_keepalive` | `(idle, interval, count)` tuple for TCP control-connection keepalive. Idle and interval use seconds; zeros keep kernel defaults. |
| `username` | Native authentication username; supplied with `rsa_public_key_path`. |
| `rsa_public_key_path` | Native authentication public-key file; supplied with `username`. |
| `use_pkcs1_padding` | Explicit legacy authentication-padding compatibility; requires authentication. Client use is rejected with libiperf 3.21+, where this flag is server-only. |

Pass the password separately to `Client(config, password=...)` or use
`IPERF3_PASSWORD`. Passwords are absent from configuration snapshots and artifacts.
Username and key path remain request metadata; key contents are not retained.
Native authentication requires an appropriate libiperf/OpenSSL build.
The tested 3.19.1/OpenSSL 3 build rejects valid credentials due to a
[native authentication limitation](compatibility.md#authentication-compatibility);
authenticated operation is qualified with libiperf 3.22.

These native timers do not replace `Client.run(timeout=...)`. The execution
timeout selects the isolated worker, includes startup, and terminates/reaps that
process before raising `TimeoutError`; it does not claim that C finalizers ran.
The watchdog stops the worker independently of a blocked Python callback, but
returning control to the caller still waits for that callback to return.

## Protocol defaults

| Protocol | `rate=None` | `blksize=None` |
| --- | --- | --- |
| TCP | Native default. | Native default. |
| UDP | Wrapper sets 1,048,576 bits/s per stream. | Wrapper selects libiperf's dynamic block-size path. |
| SCTP | Native default. | Wrapper sets 65,536 bytes. |

`ClientConfig.rate` retains the native
[per-stream bitrate meaning](https://software.es.net/iperf/invoking.html).
Aggregate targets and explicit unit strings use the separate `RateIntent`
and `parse_rate()` APIs; `estimate_plan()` checks sequential admission
budgets. See [rate intent and capabilities](../guides/configuration-intent.md).
These estimates do not impose a hard deadline on a blocking native call.

## Server configuration

Import `ServerConfig` from `iperf3_lib` or `iperf3_lib.server_config` and pass it
as `Server(config=ServerConfig(...))`. The server detaches that configuration,
then validates and snapshots it again at admission. All server calls use an
isolated Python/libiperf worker.

`Server(port=5201, bind_host=None)` and its positional form remain available.
Do not combine `config` with explicitly supplied `port` or `bind_host`, even
when the values match. `.port` and `.bind_host` remain validated mutable aliases
for `config.port` and `config.bind_address`.

| Field | Default | Meaning and validation |
| --- | --- | --- |
| `port` | `5201` | Listening port, 1–65,535. |
| `bind_address` | `None` | Local address; nonempty NUL-free string when supplied. |
| `bind_device` | `None` | Local network device name; independent of its address. |
| `address_family` | `"auto"` | `"auto"`, `"ipv4"`, or `"ipv6"`. |
| `interval_seconds` | `1.0` | Native report/statistics interval; zero or 0.1–60 seconds. |
| `idle_timeout_seconds` | `None` | Native wait-for-client timeout, 1–86,400 seconds. |
| `receive_timeout_ms` | `None` | Idle receive timeout, 100–86,400,000 milliseconds. |
| `send_timeout_ms` | `None` | Unacknowledged TCP-data timeout, 0–86,400,000 milliseconds. |
| `bitrate_limit_bps` | `None` | Server aggregate-rate limit, 0–`2**53 - 1`; zero disables the limit. |
| `bitrate_limit_interval_seconds` | `None` | Averaging interval; zero or 0.1–60 seconds; requires a bitrate limit. |
| `max_duration_seconds` | `None` | Server duration policy, 0–86,400 seconds; requires native 3.21+. |
| `affinity` | `None` | Native worker CPU selection. |
| `rsa_private_key_path` | `None` | Native private-key file; requires authorized-users configuration. |
| `authorized_users_path` | `None` | Native credentials file; requires the private-key path. |
| `time_skew_threshold_seconds` | `None` | Positive authentication clock tolerance; requires authentication files. |
| `use_pkcs1_padding` | `False` | Explicit legacy padding; requires authentication files. |
| `control_keepalive` | `None` | Tuple `(idle, interval, count)`; `(0, 0, 0)` enables keepalive with kernel defaults. |
| `payload_file` | `None` | Local native file source/sink according to test direction. |
| `extra_data` | `None` | Native JSON metadata. |
| `json_stream` | `False` | Enable native event capture. |

Strings must be nonempty and NUL-free. Integer options reject booleans and
numeric coercion. When keepalive idle is nonzero, it must exceed interval times
count. An active nonzero bitrate limit requires periodic statistics.

The [server guide](../guides/running-tests.md#use-the-python-server-wrapper)
describes returned results, callbacks, serving sessions and shutdown. A native
idle policy, a maximum client duration, and a whole-session worker timeout are
different controls.

