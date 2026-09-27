"""Configuration models and enums for iperf3 client and server."""

from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

MAX_BLOCK_SIZE = 1024 * 1024
MIN_UDP_BLOCK_SIZE = 16
MAX_UDP_BLOCK_SIZE = 65507


class Protocol(StrEnum):
    """Supported protocols for iperf3 tests."""

    TCP = "tcp"
    UDP = "udp"
    SCTP = "sctp"  # requires kernel+lib support


@dataclass
class ClientConfig:
    """Configuration for an iperf3 client run."""

    server: str
    port: int = 5201
    protocol: Protocol = Protocol.TCP
    duration: int | None = 10
    parallel: int = 1
    omit: int = 0
    reverse: bool = False
    bidirectional: bool = False  # --bidir (>= 3.7)
    mptcp: bool = False
    blksize: int | None = None  # --blksize
    rate: int | None = None  # bits/sec
    tos: int | None = None
    json_stream: bool = False
    bind_address: str | None = None
    bind_device: str | None = None
    client_port: int | None = None
    address_family: Literal["auto", "ipv4", "ipv6"] = "auto"
    socket_buffer_bytes: int | None = None
    congestion_control: str | None = None
    no_delay: bool = False
    mss: int | None = None
    connect_timeout_ms: int | None = None
    bytes_to_send: int | None = None
    blocks_to_send: int | None = None
    interval_seconds: float | None = None
    pacing_timer_us: int | None = None
    fq_rate_bps: int | None = None
    burst_packets: int | None = None
    zerocopy: bool = False
    skip_rx_copy: bool = False
    udp_counters_64bit: bool = False
    dont_fragment: bool = False
    flow_label: int | None = None
    sctp_streams: int | None = None
    sctp_bind_addresses: tuple[str, ...] = ()
    payload_file: str | None = None
    repeating_payload: bool = False
    affinity: int | None = None
    server_affinity: int | None = None
    get_server_output: bool = False
    title: str | None = None
    extra_data: str | None = None
    receive_timeout_ms: int | None = None
    send_timeout_ms: int | None = None
    control_keepalive: tuple[int, int, int] | None = None
    gsro: bool = False
    username: str | None = None
    rsa_public_key_path: str | None = None
    use_pkcs1_padding: bool = False

    def __post_init__(self) -> None:
        """Validate configuration values and option combinations."""
        # Accept stdlib IP address objects, matching the former IPvAnyAddress support.
        if not isinstance(self.server, (str, ipaddress.IPv4Address, ipaddress.IPv6Address)):
            raise TypeError("server must be a string or IP address")
        _text("server", str(self.server))
        if isinstance(self.protocol, str):
            try:
                self.protocol = Protocol(self.protocol)
            except ValueError as exc:
                raise ValueError(f"unsupported protocol: {self.protocol!r}") from exc
        if not isinstance(self.protocol, Protocol):
            raise TypeError("protocol must be a Protocol value")

        for name in (
            "reverse",
            "bidirectional",
            "mptcp",
            "json_stream",
            "no_delay",
            "zerocopy",
            "skip_rx_copy",
            "udp_counters_64bit",
            "dont_fragment",
            "repeating_payload",
            "get_server_output",
            "gsro",
            "use_pkcs1_padding",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")

        bounds = {
            "port": (1, 65535),
            "duration": (0, 86400),
            "parallel": (1, 128),
            "omit": (0, 600),
            "blksize": (1, MAX_BLOCK_SIZE),
            "rate": (0, 2**64 - 1),
            "tos": (0, 255),
            "client_port": (1, 65535),
            "socket_buffer_bytes": (1, 512 * 1024 * 1024),
            "mss": (1, 32767),
            "connect_timeout_ms": (1, 2**31 - 1),
            "bytes_to_send": (1, 2**64 - 1),
            "blocks_to_send": (1, 2**64 - 1),
            "pacing_timer_us": (1, 2**31 - 1),
            "fq_rate_bps": (0, 2**53 - 1),
            "burst_packets": (1, 1000),
            "flow_label": (1, 0xFFFFF),
            "sctp_streams": (1, 65535),
            "affinity": (0, 1024),
            "server_affinity": (0, 1024),
            "receive_timeout_ms": (100, 86400000),
            "send_timeout_ms": (0, 86400000),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if value is None and name not in {"port", "parallel", "omit"}:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.reverse and self.bidirectional:
            raise ValueError("reverse and bidirectional modes cannot be enabled together")
        if self.protocol is Protocol.UDP and self.blksize is not None:
            if not MIN_UDP_BLOCK_SIZE <= self.blksize <= MAX_UDP_BLOCK_SIZE:
                raise ValueError(
                    "UDP block size must be between "
                    f"{MIN_UDP_BLOCK_SIZE} and {MAX_UDP_BLOCK_SIZE} bytes"
                )

        for name in (
            "bind_address",
            "bind_device",
            "congestion_control",
            "payload_file",
            "title",
            "extra_data",
            "username",
            "rsa_public_key_path",
        ):
            value = getattr(self, name)
            if value is not None:
                _text(name, value)
        if not isinstance(self.address_family, str):
            raise TypeError("address_family must be a string")
        if self.address_family not in ("auto", "ipv4", "ipv6"):
            raise ValueError("address_family must be auto, ipv4 or ipv6")
        if self.interval_seconds is not None:
            _interval("interval_seconds", self.interval_seconds)
        if not isinstance(self.sctp_bind_addresses, tuple):
            raise TypeError("sctp_bind_addresses must be a tuple of address strings")
        for address in self.sctp_bind_addresses:
            _text("sctp_bind_addresses", address)
        if self.control_keepalive is not None:
            if not isinstance(self.control_keepalive, tuple) or len(self.control_keepalive) != 3:
                raise TypeError("control_keepalive must be an (idle, interval, count) tuple")
            for value in self.control_keepalive:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError("control_keepalive values must be integers")
                if not 0 <= value <= 2**31 - 1:
                    raise ValueError("control_keepalive values must be between 0 and 2147483647")
            idle, interval, count = self.control_keepalive
            if idle and idle <= interval * count:
                raise ValueError("control_keepalive idle must exceed interval times count")
        counts = sum(value is not None for value in (self.bytes_to_send, self.blocks_to_send))
        if counts > 1 or (counts and self.duration is not None):
            raise ValueError(
                "choose duration, bytes_to_send or blocks_to_send; use duration=None for counts"
            )
        if not counts and self.duration is None:
            raise ValueError("duration=None requires bytes_to_send or blocks_to_send")
        if self.protocol is not Protocol.TCP and (
            self.mptcp or self.congestion_control is not None
        ):
            raise ValueError("MPTCP and congestion_control require TCP")
        if self.protocol is Protocol.UDP and (self.no_delay or self.mss is not None):
            raise ValueError("no_delay and mss require TCP or SCTP")
        if self.zerocopy and self.protocol is not Protocol.TCP:
            raise ValueError("zerocopy requires TCP")
        if self.protocol is Protocol.SCTP:
            if self.skip_rx_copy:
                raise ValueError("skip_rx_copy requires TCP or UDP")
            if self.fq_rate_bps is not None:
                raise ValueError("fq_rate_bps requires TCP or UDP")
            if self.tos not in (None, 0):
                raise ValueError("nonzero tos requires TCP or UDP")
            if self.mss is not None and self.mss < 512:
                raise ValueError("SCTP mss must be at least 512 bytes")
        if self.protocol is not Protocol.UDP and any(
            (self.udp_counters_64bit, self.dont_fragment, self.gsro)
        ):
            raise ValueError("udp_counters_64bit, dont_fragment and gsro require UDP")
        if self.protocol is not Protocol.SCTP and (
            self.sctp_streams is not None or self.sctp_bind_addresses
        ):
            raise ValueError("SCTP association options require SCTP")
        if self.flow_label is not None and (
            self.protocol is not Protocol.TCP or self.address_family != "ipv6"
        ):
            raise ValueError("flow_label requires TCP and address_family='ipv6'")
        if self.dont_fragment:
            try:
                ipv6 = ipaddress.ip_address(str(self.server)).version == 6
            except ValueError:
                ipv6 = False
            if self.address_family == "ipv6" or ipv6:
                raise ValueError("dont_fragment applies only to IPv4")
            if self.address_family == "auto":
                # Prevent hostname resolution from silently selecting IPv6.
                self.address_family = "ipv4"
        if self.server_affinity is not None and self.affinity is None:
            raise ValueError("server_affinity requires an explicit local affinity")
        if (self.username is None) != (self.rsa_public_key_path is None):
            raise ValueError("username and rsa_public_key_path must be supplied together")
        if self.use_pkcs1_padding and self.username is None:
            raise ValueError("use_pkcs1_padding requires authentication")
        if self.payload_file is not None and self.repeating_payload:
            raise ValueError("payload_file and repeating_payload cannot be combined")


def _text(name: str, value: str) -> None:
    """Validate text before it crosses a NUL-terminated native boundary."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty and contain no NUL")


def _interval(name: str, value: float) -> None:
    """Validate native reporting interval, including zero to disable intervals."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not (value == 0 or 0.1 <= value <= 60) or not math.isfinite(value):
        raise ValueError(f"{name} must be zero or between 0.1 and 60 seconds")


LEGACY_CONFIG_FIELDS = frozenset(
    {
        "server",
        "port",
        "protocol",
        "duration",
        "parallel",
        "omit",
        "reverse",
        "bidirectional",
        "mptcp",
        "blksize",
        "rate",
        "tos",
        "json_stream",
    }
)


def requires_worker(config: ClientConfig) -> bool:
    """Select process isolation for expanded native configuration and live output."""
    from dataclasses import fields

    return bool(
        config.mptcp
        or config.json_stream
        or config.duration is None
        or config.duration == 0
        or any(
            getattr(config, item.name) != item.default
            for item in fields(config)
            if item.name not in LEGACY_CONFIG_FIELDS
        )
    )


def config_to_dict(config: ClientConfig, *, compact: bool = False) -> dict:
    """Copy a configuration to JSON values without storing any password."""
    from dataclasses import asdict, fields

    data = asdict(config)
    if compact:
        for item in fields(config):
            if item.name not in LEGACY_CONFIG_FIELDS and getattr(config, item.name) == item.default:
                data.pop(item.name)
    data["server"] = str(config.server)
    return json.loads(json.dumps(data, allow_nan=False))


def config_from_dict(data: dict) -> ClientConfig:
    """Restore tuple fields from a JSON configuration and validate admission."""
    values = dict(data)
    for name in ("sctp_bind_addresses", "control_keepalive"):
        if isinstance(values.get(name), list):
            values[name] = tuple(values[name])
    return ClientConfig(**values)
