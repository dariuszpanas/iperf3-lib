"""Configuration models and enums for iperf3 client and server."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import StrEnum

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
    duration: int = 10
    parallel: int = 1
    omit: int = 0
    reverse: bool = False
    bidirectional: bool = False  # --bidir (>= 3.7)
    mptcp: bool = False  # compatibility field; the direct ABI backend rejects True
    blksize: int | None = None  # --blksize
    rate: int | None = None  # bits/sec
    tos: int | None = None
    json_stream: bool = False  # compatibility field; the direct ABI backend rejects True

    def __post_init__(self) -> None:
        """Validate configuration values and option combinations."""
        # Accept stdlib IP address objects, matching the former IPvAnyAddress support.
        if not isinstance(self.server, (str, ipaddress.IPv4Address, ipaddress.IPv6Address)):
            raise TypeError("server must be a string or IP address")
        if isinstance(self.protocol, str):
            try:
                self.protocol = Protocol(self.protocol)
            except ValueError as exc:
                raise ValueError(f"unsupported protocol: {self.protocol!r}") from exc
        if not isinstance(self.protocol, Protocol):
            raise TypeError("protocol must be a Protocol value")

        for name in ("reverse", "bidirectional", "mptcp", "json_stream"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")

        bounds = {
            "port": (1, 65535),
            "duration": (1, 86400),
            "parallel": (1, 128),
            "omit": (0, 600),
            "blksize": (1, MAX_BLOCK_SIZE),
            "rate": (0, 2**64 - 1),
            "tos": (0, 255),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if value is None and name in {"blksize", "rate", "tos"}:
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
