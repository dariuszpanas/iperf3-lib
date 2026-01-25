"""Configuration models and enums for iperf3 client and server."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, IPvAnyAddress, NonNegativeInt, PositiveInt


class Protocol(str, Enum):
    """Supported protocols for iperf3 tests."""

    TCP = "tcp"
    UDP = "udp"
    SCTP = "sctp"  # requires kernel+lib support


class ClientConfig(BaseModel):
    """Configuration for an iperf3 client run."""

    server: str | IPvAnyAddress
    port: PositiveInt = 5201
    protocol: Protocol = Protocol.TCP
    duration: PositiveInt = 10
    parallel: PositiveInt = 1
    omit: NonNegativeInt = 0
    reverse: bool = False
    bidirectional: bool = False  # --bidir (>= 3.7)
    mptcp: bool = False  # (>= 3.19, Linux)
    blksize: PositiveInt | None = None  # --blksize
    rate: int | None = None  # numeric rate alias (bits/sec) - canonical for lib API
    tos: int | None = Field(default=None, ge=0, le=255)
    json_stream: bool = False  # if lib supports streaming JSON chunks
