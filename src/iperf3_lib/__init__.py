"""Top-level package for iperf3-lib: modern Python wrapper for iperf3 using cffi."""

from .config import ClientConfig, Protocol
from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .iperf_client import Client
from .iperf_server import Server
from .result import Result

__all__ = [
    "Client",
    "ClientConfig",
    "IperfError",
    "IperfLibraryError",
    "Protocol",
    "Result",
    "Server",
    "UnsupportedFeatureError",
]
