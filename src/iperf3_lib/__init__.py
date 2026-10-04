"""Top-level package for iperf3-lib: modern Python wrapper for iperf3 using cffi."""

from ._event_stream import EventStream
from .config import ClientConfig, Protocol
from .events import LiveEvent, NativeEvent
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
    "EventStream",
    "IperfError",
    "IperfCleanupError",
    "IperfLibraryError",
    "IntervalStats",
    "LiveEvent",
    "NativeEvent",
    "Protocol",
    "Result",
    "Server",
    "ServerConfig",
    "UnsupportedFeatureError",
    "SumStats",
]
