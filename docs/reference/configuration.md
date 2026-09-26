# Configuration reference

`ClientConfig` is a mutable standard-library dataclass on the development
branch. Its constructor validates types, bounds, and option combinations.
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
`Client` retains the configuration object passed to it.

## Client fields

| Field | Default | Accepted values and behavior |
| --- | --- | --- |
| `server` | Required | Hostname/address string or standard-library `IPv4Address`/`IPv6Address` object. Name resolution occurs in libiperf. |
| `port` | `5201` | Integer, 1–65,535. |
| `protocol` | `Protocol.TCP` | `Protocol.TCP`, `Protocol.UDP`, `Protocol.SCTP`, or the exact strings `"tcp"`, `"udp"`, `"sctp"`. Strings normalize to the enum. |
| `duration` | `10` | Integer measured-test duration in seconds, 1–86,400. It is not a total wall-clock deadline. |
| `parallel` | `1` | Integer stream count, 1–128. |
| `omit` | `0` | Integer initial omitted period in seconds, 0–600. |
| `reverse` | `False` | Boolean; sends from server to client. |
| `bidirectional` | `False` | Boolean; sends in both directions simultaneously. Requires native support. |
| `mptcp` | `False` | Compatibility field; `True` raises `UnsupportedFeatureError` when running. |
| `blksize` | `None` | Integer bytes, 1–1,048,576; UDP narrows this to 16–65,507. `None` chooses protocol-specific defaults. |
| `rate` | `None` | Integer target bits/s per stream, 0–18,446,744,073,709,551,615. Zero disables the native rate limit. `None` chooses protocol defaults. |
| `tos` | `None` | Integer traffic-class/TOS value, 0–255. `None` leaves the native default. |
| `json_stream` | `False` | Compatibility field; `True` raises `UnsupportedFeatureError` when running. |

Numeric fields require integers. Booleans, floats, and numeric strings are
rejected for these fields. Boolean options require actual `bool` values.
An invalid type raises `TypeError`; an out-of-range value or invalid
combination raises `ValueError`. `reverse=True` with `bidirectional=True` is
invalid.

The `server` annotation remains `str`, although runtime construction also
accepts standard-library IP address objects. Passing `str(address)` is
compatible with both the runtime and that static annotation. The constructor
does not validate whether a hostname is reachable.

## Protocol defaults

| Protocol | `rate=None` | `blksize=None` |
| --- | --- | --- |
| TCP | Native default. | Native default. |
| UDP | Wrapper sets 1,048,576 bits/s per stream. | Wrapper selects libiperf's dynamic block-size path. |
| SCTP | Native default. | Wrapper sets 65,536 bytes. |

There is currently no aggregate-rate field, unit-string parser, or plan-wide
traffic budget. `rate` retains the native
[per-stream bitrate meaning](https://software.es.net/iperf/invoking.html).

## Server arguments

`Server(port=5201, bind_host=None)` accepts an integer port from 1 through
65,535. A boolean or non-integer port raises `TypeError`. An invalid port
range raises `ValueError`.

`bind_host` is a string passed to libiperf's bind-address setter when nonempty.
`None` leaves the bind address at its native default. Server options are
constructor arguments; there is no `ServerConfig` dataclass.

