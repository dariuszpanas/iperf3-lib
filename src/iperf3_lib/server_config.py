"""Validated settings for an isolated libiperf server."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass
class ServerConfig:
    """Server listener, policy, authentication, and reporting configuration.

    Native version and build support are checked in the isolated worker.
    Paths refer to files on the machine running Python. Authentication files
    must be supplied together; their contents are never stored in results.
    """

    port: int = 5201
    bind_address: str | None = None
    bind_device: str | None = None
    address_family: Literal["auto", "ipv4", "ipv6"] = "auto"
    interval_seconds: float = 1.0
    idle_timeout_seconds: int | None = None
    receive_timeout_ms: int | None = None
    send_timeout_ms: int | None = None
    bitrate_limit_bps: int | None = None
    bitrate_limit_interval_seconds: float | None = None
    max_duration_seconds: int | None = None
    affinity: int | None = None
    rsa_private_key_path: str | None = None
    authorized_users_path: str | None = None
    time_skew_threshold_seconds: int | None = None
    use_pkcs1_padding: bool = False
    control_keepalive: tuple[int, int, int] | None = None
    payload_file: str | None = None
    extra_data: str | None = None
    json_stream: bool = False

    def __post_init__(self) -> None:
        """Reject ambiguous types, unsafe strings, and invalid native bounds."""
        bounds = {
            "port": (1, 65535),
            "idle_timeout_seconds": (1, 86400),
            "receive_timeout_ms": (100, 86400000),
            "send_timeout_ms": (0, 86400000),
            "bitrate_limit_bps": (0, 2**53 - 1),
            "max_duration_seconds": (0, 86400),
            "affinity": (0, 1024),
            "time_skew_threshold_seconds": (1, 2**31 - 1),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if value is None and name != "port":
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")

        for name in (
            "bind_address",
            "bind_device",
            "rsa_private_key_path",
            "authorized_users_path",
            "payload_file",
            "extra_data",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value.strip() or "\0" in value:
                raise ValueError(f"{name} must be nonempty and contain no NUL characters")

        if not isinstance(self.address_family, str):
            raise TypeError("address_family must be a string")
        if self.address_family not in {"auto", "ipv4", "ipv6"}:
            raise ValueError("address_family must be auto, ipv4, or ipv6")
        for name in ("use_pkcs1_padding", "json_stream"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        for name in ("interval_seconds", "bitrate_limit_interval_seconds"):
            value = getattr(self, name)
            if value is None and name == "bitrate_limit_interval_seconds":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not (value == 0 or 0.1 <= value <= 60) or not math.isfinite(value):
                raise ValueError(f"{name} must be zero or between 0.1 and 60")

        if self.bitrate_limit_interval_seconds is not None and self.bitrate_limit_bps is None:
            raise ValueError("bitrate_limit_interval_seconds requires bitrate_limit_bps")
        if self.bitrate_limit_bps and self.interval_seconds == 0:
            raise ValueError("a nonzero bitrate limit requires a nonzero stats interval")

        authentication = self.rsa_private_key_path is not None
        if authentication != (self.authorized_users_path is not None):
            raise ValueError(
                "rsa_private_key_path and authorized_users_path must be supplied together"
            )
        if not authentication and (
            self.time_skew_threshold_seconds is not None or self.use_pkcs1_padding
        ):
            raise ValueError("authentication options require the server authentication files")

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
