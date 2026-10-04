# Native option coverage

This reference maps the option names accepted by the tagged **iperf3 3.19.1**
and **3.22** command-line parsers to the Python API. Use it alongside the
[configuration reference](configuration.md) and [native-control recipes](../guides/native-controls.md).
It includes deprecated aliases and options enabled only in particular native
builds. Parser presence is not a claim that an operating system can apply an option.

## How Python reaches libiperf

Basic client configurations use the existing direct CFFI setters. Expanded
controls, MPTCP, streaming, an explicit execution timeout, or an event callback
select a separate **Python worker that loads libiperf through CFFI**. The worker
uses libiperf's public argument parser for controls without dedicated public
setters. It does not invoke the `iperf3` executable or expose arbitrary CLI
argument passthrough.

Typed configuration still validates values and combinations before execution.
Native parser acceptance, requested settings, returned settings, and successful
network behavior are different evidence. See
[capability reporting](../guides/configuration-intent.md#inspect-capability-evidence)
and [execution limits](compatibility.md#feature-boundaries).

The tables account for **70 long-option names in 3.19.1 and 73 in 3.21 and 3.22**.
The three additional names are marked **3.21+**. Short aliases are shown where
defined. This inventory follows the tagged parser, help, and headers rather
than assuming that the manual lists every option.

## Endpoints and protocols

| Native option | Python facility | Limits and interpretation |
| --- | --- | --- |
| `-c`, `--client` | `Client(ClientConfig(server=...))` | Destination hostname or address. |
| `-s`, `--server` | `Server(...)` | A Python server can return a normalized `Result`; see the [server guide](../guides/running-tests.md#use-the-python-server-wrapper). |
| `-p`, `--port` | Client or server `port` | Listening/destination port, not the client's source port. |
| `-B`, `--bind` | Client `bind_address`; server local-address configuration | An address assigned to the local host. `Server(bind_host=...)` remains the concise address-binding form. |
| `--bind-dev` | `bind_device` | Named device, such as `eth0`; native build, operating-system support and permissions apply. |
| `--cport` | `client_port` | Data-stream source port; distinct from the control connection and server port. |
| `-4`, `--version4` | `address_family="ipv4"` | Explicit family selection; an address literal alone is not a family policy. |
| `-6`, `--version6` | `address_family="ipv6"` | IPv6 must also be available at both endpoints. |
| `-u`, `--udp` | `protocol=Protocol.UDP` | The control connection still uses TCP. |
| `--sctp` | `protocol=Protocol.SCTP` | Requires a native SCTP build and kernel support. |
| `-m`, `--mptcp` | `mptcp=True` | TCP only; selects the isolated worker and requires native/kernel MPTCP support. |
| `-R`, `--reverse` | `reverse=True` | Server sends the measured traffic. |
| `--bidir` | `bidirectional=True` | Simultaneous traffic in both directions; incompatible with `reverse=True`. |

Address and device binding are separate controls. An interface's assigned IP
belongs in `bind_address`/`bind_host`; its device name belongs in `bind_device`.
Use these explicit fields instead of depending on the CLI's `%device` shorthand.
See [binding recipes](../guides/native-controls.md#select-local-addresses-and-devices).

## Load, termination and pacing

| Native option | Python facility | Limits and interpretation |
| --- | --- | --- |
| `-t`, `--time` | `duration` | Requested measured duration, not an operation deadline. |
| `-n`, `--bytes` | `duration=None, bytes_to_send=...` | Byte-count termination instead of duration. |
| `-k`, `--blockcount` | `duration=None, blocks_to_send=...` | Block-count termination instead of duration or bytes. |
| `-l`, `--length` | `blksize` | Read/write buffer or UDP datagram size, in bytes. |
| `-P`, `--parallel` | `parallel` | Parallel native traffic streams within one test. |
| `-O`, `--omit` | `omit` | Initial traffic period excluded from measured statistics. |
| `-b`, `--bitrate`, `--bandwidth` | `rate` or `RateIntent` | Per-stream pacing target; `--bandwidth` is a deprecated native alias. |
| `/count` suffix of `--bitrate` | `burst_packets` | Explicit burst count; separate from the rate's units. |
| `--pacing-timer` | `pacing_timer_us` | Configured interval in microseconds; native builds using nanosleep do not use the pacing timer. Not a packet-spacing guarantee. |
| `--fq-rate` | `fq_rate_bps` | Additional TCP/UDP socket-level pacing; rejected for SCTP. Depends on native/platform support. |
| `--no-fq-socket-pacing` | `fq_rate_bps=0` | Deprecated native spelling for disabling socket pacing. |
| `-i`, `--interval` | `interval_seconds` | Native statistics/reporting interval; zero disables periodic intervals. It does not enable live delivery by itself. |

Use strict integer counts/bytes/bits per second. For human-readable rate input,
call [parse_rate()](../guides/configuration-intent.md#exact-unit-inputs) explicitly.
Native CLI suffix parsing is not the Python configuration contract.

## Transport, payload and host controls

| Native option | Python facility | Limits and interpretation |
| --- | --- | --- |
| `-w`, `--window` | `socket_buffer_bytes` | Requested send/receive socket buffers; effective kernel sizes can differ. |
| `-M`, `--set-mss` | `mss` | TCP maximum segment size, 1–32,767 bytes; SCTP requires at least 512. |
| `-N`, `--no-delay` | `no_delay=True` | TCP/SCTP no-delay control. |
| `-C`, `--congestion`, `--linux-congestion` | `congestion_control` | Requested TCP algorithm; kernel availability matters. The long legacy alias adds no Python field. |
| `-S`, `--tos` | `tos` | Raw 8-bit traffic-class value; nonzero SCTP values are rejected because the supported native implementations do not apply them. |
| `--dscp` | Explicit conversion to `tos` | For numeric DSCP, use `tos=dscp << 2` when ECN bits should be zero. Symbolic CLI names are not accepted as configuration values. |
| `-L`, `--flowlabel` | `flow_label` with `address_family="ipv6"` | TCP only; native/platform-dependent IPv6 flow label. |
| `--udp-counters-64bit` | `udp_counters_64bit=True` | UDP only; both peers must support the packet format. |
| `--dont-fragment` | `dont_fragment=True` | IPv4 UDP; not a general path-MTU discovery API. |
| `--gsro` **3.21+** | `gsro=True` | UDP GSO/GRO request; peer and operating-system support still apply. |
| `-Z`, `--zerocopy` | `zerocopy=True` | TCP only; native zero-copy send path where available. |
| `--skip-rx-copy` | `skip_rx_copy=True` | TCP/UDP receive-copy avoidance where the native build supports it; rejected for SCTP. |
| `-F`, `--file` | `payload_file` | Local source/sink file according to role and traffic direction; not a file-transfer integrity facility. |
| `--repeating-payload` | `repeating_payload=True` | Repeating data rather than random payload; cannot be combined with `payload_file`. |
| `--nstreams` | `sctp_streams` | SCTP streams within an association, distinct from `parallel`. |
| `-X`, `--xbind` | `sctp_bind_addresses` | Tuple of SCTP association addresses; protocol/family restrictions apply. |
| `-A`, `--affinity` | `affinity`; client `server_affinity` | Local CPU and optional requested server CPU. Affinity changes happen inside the worker. |

## Timeouts, server policies and authentication

| Native option | Python facility | Limits and interpretation |
| --- | --- | --- |
| `--connect-timeout` | `connect_timeout_ms` | Initial control-connection establishment; not a whole-run deadline. |
| `--rcv-timeout` | `receive_timeout_ms` | Idle receive timeout during a test. |
| `--snd-timeout` | `send_timeout_ms` | Unacknowledged TCP-data timeout where supported. |
| `--cntl-ka[=idle/interval/count]` | `control_keepalive=(idle, interval, count)` | Control-connection TCP keepalive; separate from test duration. This option exists in both tagged parsers/help even though their manuals omit it. |
| `-1`, `--one-off` | `Server.run_once()` | One server result; a persistent Python loop is a separate API. |
| `--idle-timeout` | `ServerConfig.idle_timeout_seconds` | Distinguish waiting for a client from the whole-session worker timeout. |
| `--server-bitrate-limit` | `ServerConfig.bitrate_limit_bps`, `bitrate_limit_interval_seconds` | Native aggregate-rate rejection/measurement policy, distinct from a client pacing target. |
| `--server-max-duration` **3.21+** | `ServerConfig.max_duration_seconds` | Server admission policy; not a substitute for worker cleanup on timeout. |
| `--username` | Client `username` | Used with explicit authentication configuration. |
| `--rsa-public-key-path` | Client `rsa_public_key_path` | Requires a native authentication build and a matching server key. |
| `--rsa-private-key-path` | `ServerConfig.rsa_private_key_path` | Server authentication; do not put private-key contents in result metadata. |
| `--authorized-users-path` | `ServerConfig.authorized_users_path` | Native-format credentials file. |
| `--time-skew-threshold` | `ServerConfig.time_skew_threshold_seconds` | Permitted client/server clock difference. |
| `--use-pkcs1-padding` | `use_pkcs1_padding` | Explicit legacy authentication compatibility. Client use is rejected with libiperf 3.21+, where the flag is server-only. |

The server field names and complete validation contract are listed in
[Server configuration](configuration.md#server-configuration).

## Results, events and command-line concerns

| Native option | Python facility or disposition | Limits and interpretation |
| --- | --- | --- |
| `-J`, `--json` | `Result.raw` plus normalized dataclasses | Completed client/server output; enabled by the wrapper. |
| `--get-server-output` | `get_server_output=True` | Preserve remote output in native JSON; the server determines its format. |
| `--extra-data` | `extra_data` | Native JSON metadata. Application artifact metadata belongs separately in namespaced `result.extensions`. |
| `--json-stream` | `json_stream=True`; `Client.run(on_event=...)` | Typed parent-side event delivery from an isolated worker; see the [execution guide](../guides/native-controls.md#observe-events-and-bound-a-run). |
| `--json-stream-full-output` **3.21+** | Worker result-capture implementation | Enabled internally for streaming on 3.21+. On 3.19.1, the result is explicitly reconstructed from retained native event envelopes. |
| `-T`, `--title` | `title` | Native output title; not an artifact identifier or metric label policy. |
| `-f`, `--format` | Format numeric Python results in the application | Measurement units stay explicit; no CLI display-unit passthrough. |
| `-V`, `--verbose` | Inspect retained native JSON and diagnostics | No CLI verbosity passthrough. |
| `--timestamps` | Native/execution timestamps and `NativeEvent.received_at_seconds` | Printing timestamps does not change measurement timing. |
| `--forceflush` | Worker transport implementation | No user flag; not a guarantee that a slow callback can keep every event. |
| `-d`, `--debug` | Python diagnostics / native development tooling | No arbitrary native debug-level passthrough. |
| `-D`, `--daemon` | Application/service process management | Library calls do not daemonize the calling application. |
| `-I`, `--pidfile` | Application/service process management | Worker ownership is managed internally; no CLI PID-file option. |
| `--logfile` | Application logging and saved artifacts | No CLI logfile passthrough; payload files and exported results are separate facilities. |
| `-v`, `--version` | Capability report and package version | Does not exit the application. |
| `-h`, `--help` | Python help and this documentation | Does not exit the application. |

## Platform support

Linux is the project's native qualification platform. The presence of an
option in a tagged parser, a public header, or a capability report does not
establish that a particular kernel, native build, peer, or permission set can
execute it. Unsupported requested settings must fail explicitly. Keep original
native errors and distinguish unknown verification from a proven effective value.

See [compatibility and execution limits](compatibility.md) for the supported
platforms and the [event and timeout guide](../guides/native-controls.md#observe-events-and-bound-a-run)
for worker behavior.

Authoritative inventories:

- [3.19.1 parser](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c),
  [help](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_locale.c),
  [public header](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.h),
  [manual](https://github.com/esnet/iperf/blob/3.19.1/src/iperf3.1).
- [3.21 parser](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c),
  [help](https://github.com/esnet/iperf/blob/3.21/src/iperf_locale.c),
  [public header](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.h),
  [manual](https://github.com/esnet/iperf/blob/3.21/src/iperf3.1).
- [3.22 parser](https://github.com/esnet/iperf/blob/3.22/src/iperf_api.c),
  [help](https://github.com/esnet/iperf/blob/3.22/src/iperf_locale.c),
  [public header](https://github.com/esnet/iperf/blob/3.22/src/iperf_api.h),
  [manual](https://github.com/esnet/iperf/blob/3.22/src/iperf3.1).

Protocol restrictions also follow the actual
[TCP](https://github.com/esnet/iperf/blob/3.21/src/iperf_tcp.c),
[UDP](https://github.com/esnet/iperf/blob/3.21/src/iperf_udp.c), and
[SCTP](https://github.com/esnet/iperf/blob/3.21/src/iperf_sctp.c)
implementations, with the corresponding 3.19.1 and 3.22 implementations checked too.
A stored option does not establish that a protocol uses it.
