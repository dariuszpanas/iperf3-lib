"""Top-level package for iperf3-lib: modern Python wrapper for iperf3 using cffi."""

from .config import ClientConfig, Protocol
from .events import NativeEvent
from .exceptions import IperfCleanupError, IperfError, IperfLibraryError, UnsupportedFeatureError
from .iperf_client import Client
from .iperf_server import Server
from .result import Diagnostic, FlowStats, IntervalStats, Result, SumStats
from .server_config import ServerConfig

__all__ = [
    "Client",
    "ClientConfig",
    "Diagnostic",
    "FlowStats",
    "IperfError",
    "IperfCleanupError",
    "IperfLibraryError",
    "IntervalStats",
    "NativeEvent",
    "Protocol",
    "Result",
    "Server",
    "ServerConfig",
    "UnsupportedFeatureError",
    "SumStats",
]
