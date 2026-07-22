"""Configuration models and enums for iperf3 client and server."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, NonNegativeInt, model_validator

MAX_BLOCK_SIZE = 1024 * 1024
MIN_UDP_BLOCK_SIZE = 16
MAX_UDP_BLOCK_SIZE = 65507


class Protocol(StrEnum):
    """Supported protocols for iperf3 tests."""

    TCP = "tcp"
    UDP = "udp"
    SCTP = "sctp"  # requires kernel+lib support


class ClientConfig(BaseModel):
    """Configuration for an iperf3 client run."""

    model_config = ConfigDict(extra="forbid")

    server: str | IPvAnyAddress
    port: int = Field(default=5201, ge=1, le=65535)
    protocol: Protocol = Protocol.TCP
    duration: int = Field(default=10, ge=1, le=86400)
    parallel: int = Field(default=1, ge=1, le=128)
    omit: NonNegativeInt = Field(default=0, le=600)
    reverse: bool = False
    bidirectional: bool = False  # --bidir (>= 3.7)
    mptcp: bool = False  # compatibility field; the direct ABI backend rejects True
    blksize: int | None = Field(default=None, ge=1, le=MAX_BLOCK_SIZE)  # --blksize
    rate: int | None = Field(default=None, ge=0, le=2**64 - 1)  # bits/sec
    tos: int | None = Field(default=None, ge=0, le=255)
    json_stream: bool = False  # compatibility field; the direct ABI backend rejects True

    @model_validator(mode="after")
    def validate_combined_options(self) -> Self:
        """Validate constraints that depend on more than one option."""
        if self.reverse and self.bidirectional:
            raise ValueError("reverse and bidirectional modes cannot be enabled together")
        if self.protocol is Protocol.UDP and self.blksize is not None:
            if not MIN_UDP_BLOCK_SIZE <= self.blksize <= MAX_UDP_BLOCK_SIZE:
                raise ValueError(
                    "UDP block size must be between "
                    f"{MIN_UDP_BLOCK_SIZE} and {MAX_UDP_BLOCK_SIZE} bytes"
                )
        return self
