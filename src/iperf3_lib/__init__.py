"""Top-level package for iperf3-lib: modern Python wrapper for iperf3 using cffi."""

from .config import ClientConfig, Protocol
from .iperf_client import Client
from .iperf_server import Server
from .result import Result

__all__ = ["Client", "ClientConfig", "Protocol", "Result", "Server"]
