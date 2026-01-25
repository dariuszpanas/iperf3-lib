"""Server wrapper and helpers for running iperf3 server tests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from functools import partial
from typing import Any

from .exceptions import IperfError, IperfLibraryError
from .ffi.api import ffi, lib


def _check(ret: int) -> None:
    """Raise IperfError if the return code is negative."""
    if ret < 0:
        err = lib.iperf_strerror(lib.i_errno)
        msg = ffi.string(err).decode() if err != ffi.NULL else "unknown libiperf error"
        raise IperfError(msg)


def _set_str(setter: Callable[..., Any], t: Any, s: str) -> None:
    """Call a CFFI string setter with a Python string argument."""
    setter(t, ffi.new("char[]", s.encode()))


class Server:
    """Minimal server wrapper. By default, it runs a single test, then returns.

    For a persistent loop, call serve_forever().
    """

    def __init__(self, port: int = 5201, bind_host: str | None = None):
        """Initialize the server with a port and optional bind host."""
        self.port = port
        self.bind_host = bind_host
        # event used to cooperatively stop the serve_forever loop
        self._stop_event = threading.Event()

    def _run_iteration(self, t: Any) -> None:
        """Run a single server iteration against an existing iperf_test `t`.

        Extracted from the loop in `serve_forever` to make testing easier.
        """
        _check(lib.iperf_run_server(t))
        lib.iperf_reset_test(t)

    def run_once(self) -> None:
        """Run the server for a single test and return."""
        t = lib.iperf_new_test()
        if t == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        try:
            _check(lib.iperf_defaults(t))
            lib.iperf_set_test_role(t, b"s")
            lib.iperf_set_test_server_port(t, int(self.port))
            if self.bind_host:
                _set_str(lib.iperf_set_test_server_hostname, t, self.bind_host)
            # single iteration
            self._run_iteration(t)
        finally:
            lib.iperf_free_test(t)

    def serve_forever(self) -> None:
        """Run the server loop until `stop()` is called.

        This method creates a single iperf_test and reuses it across iterations.
        The loop checks a threading.Event to allow cooperative shutdown.
        """
        t = lib.iperf_new_test()
        if t == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        try:
            _check(lib.iperf_defaults(t))
            lib.iperf_set_test_role(t, b"s")
            lib.iperf_set_test_server_port(t, int(self.port))
            if self.bind_host:
                _set_str(lib.iperf_set_test_server_hostname, t, self.bind_host)
            while not self._stop_event.is_set():
                try:
                    self._run_iteration(t)
                except IperfError:
                    # If stop() has been requested, suppress errors caused by
                    # the socket being closed during shutdown. Otherwise
                    # re-raise to surface unexpected server errors.
                    if self._stop_event.is_set():
                        break
                    raise
        finally:
            lib.iperf_free_test(t)

    async def aserve_once(self) -> None:
        """Run the server for a single test asynchronously."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, partial(self.run_once))

    def stop(self) -> None:
        """Signal the serve_forever loop to stop."""
        self._stop_event.set()
