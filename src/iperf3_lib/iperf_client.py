"""Client wrapper and helpers for running iperf3 client tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from functools import partial

from .config import ClientConfig, Protocol
from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .ffi.api import ffi, lib
from .result import EndStats, Result, SumStats

TCP_PROTOCOL_ID = 1
UDP_PROTOCOL_ID = 2
SCTP_PROTOCOL_ID = 12

DEFAULT_UDP_BLOCK_SIZE = 0  # select the control connection's MSS, with libiperf fallback
DEFAULT_SCTP_BLOCK_SIZE = 64 * 1024
DEFAULT_UDP_RATE = 1024 * 1024

PROTOCOL_IDS = {
    Protocol.TCP: TCP_PROTOCOL_ID,
    Protocol.UDP: UDP_PROTOCOL_ID,
    Protocol.SCTP: SCTP_PROTOCOL_ID,
}


def _check(ret: int) -> None:
    """Raise IperfError if the return code is negative."""
    if ret < 0:
        err = lib.iperf_strerror(lib.i_errno)
        msg = ffi.string(err).decode() if err != ffi.NULL else "unknown libiperf error"
        raise IperfError(msg)


def _set_str(setter: Callable, t, s: str) -> None:
    """Call a CFFI string setter with a Python string argument."""
    setter(t, ffi.new("char[]", s.encode()))


def _try_set(sym: str) -> Callable | None:
    """Return a lib function if it exists, else None."""
    try:
        return getattr(lib, sym)
    except AttributeError:
        return None


def _maybe_set(setter_name: str, t, value: int) -> bool:
    """Try to set a value using a setter if available; return True if set."""
    fn = _try_set(setter_name)
    if fn:
        fn(t, int(value))
        return True
    return False


def _install_json_callback(t) -> tuple[list[str], object | None]:
    """Install libiperf's JSON callback when both the library and FFI support it.

    Minimal Python test doubles commonly omit ``ffi.callback``. In that case,
    retain the legacy getter-based behavior so unit tests do not need to emulate
    native callback machinery.
    """
    setter = _try_set("iperf_set_test_json_callback")
    callback_factory = getattr(ffi, "callback", None)
    if setter is None or callback_factory is None:
        return [], None

    payloads: list[str] = []

    def receive_json(_test, payload) -> None:
        if payload != ffi.NULL:
            payloads.append(ffi.string(payload).decode())

    callback = callback_factory("void(iperf_test *, char *)", receive_json)
    setter(t, callback)
    return payloads, callback


class Client:
    """Client for running iperf3 tests using the provided configuration."""

    def __init__(self, cfg: ClientConfig):
        """Initialize the client with a configuration object."""
        self.cfg = cfg

    def run(self) -> Result:
        """Run the iperf3 test synchronously and return the result."""
        if self.cfg.mptcp:
            raise UnsupportedFeatureError(
                "MPTCP is unavailable through the direct libiperf ABI backend; "
                "libiperf does not publish an MPTCP setter"
            )
        if self.cfg.json_stream:
            raise UnsupportedFeatureError(
                "Streaming JSON is unavailable through the direct libiperf ABI backend; "
                "Client.run() requires one complete JSON result"
            )

        t = lib.iperf_new_test()
        if t == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        try:
            _check(lib.iperf_defaults(t))
            lib.iperf_set_test_role(t, b"c")
            _set_str(lib.iperf_set_test_server_hostname, t, str(self.cfg.server))
            lib.iperf_set_test_server_port(t, int(self.cfg.port))
            lib.iperf_set_test_duration(t, int(self.cfg.duration))

            protocol_id = PROTOCOL_IDS[self.cfg.protocol]
            protocol_setter = _try_set("set_protocol")
            protocol_getter = _try_set("iperf_get_test_protocol_id")
            if protocol_setter is None or protocol_getter is None:
                raise UnsupportedFeatureError(
                    "This libiperf lacks the public protocol selection API"
                )
            _check(protocol_setter(t, protocol_id))
            if int(protocol_getter(t)) != protocol_id:
                raise IperfLibraryError(
                    f"libiperf did not apply requested protocol {self.cfg.protocol.value}"
                )

            if self.cfg.omit:
                lib.iperf_set_test_omit(t, int(self.cfg.omit))
            if self.cfg.parallel and self.cfg.parallel > 1:
                lib.iperf_set_test_num_streams(t, int(self.cfg.parallel))

            block_size = self.cfg.blksize
            if block_size is None and self.cfg.protocol is Protocol.UDP:
                block_size = DEFAULT_UDP_BLOCK_SIZE
            elif block_size is None and self.cfg.protocol is Protocol.SCTP:
                block_size = DEFAULT_SCTP_BLOCK_SIZE
            if block_size is not None:
                lib.iperf_set_test_blksize(t, int(block_size))

            if self.cfg.tos is not None:
                lib.iperf_set_test_tos(t, int(self.cfg.tos))

            # reverse / bidirectional
            if self.cfg.reverse:
                lib.iperf_set_test_reverse(t, 1)
            if self.cfg.bidirectional:
                if not _maybe_set("iperf_set_test_bidirectional", t, 1):
                    raise UnsupportedFeatureError("Bidirectional not supported by this libiperf")

            rate = self.cfg.rate
            if rate is None and self.cfg.protocol is Protocol.UDP:
                rate = DEFAULT_UDP_RATE
            if rate is not None:
                rate_setter = _try_set("iperf_set_test_rate")
                if rate_setter is None:
                    raise UnsupportedFeatureError("This libiperf lacks the public rate setter")
                rate_setter(t, int(rate))

            # ensure JSON output
            if not _maybe_set("iperf_set_test_json_output", t, 1):
                # some extremely old libs may lack JSON setter; we rely on JSON for parsing
                raise UnsupportedFeatureError("This libiperf lacks JSON output support")

            callback_payloads, callback = _install_json_callback(t)
            _check(lib.iperf_run_client(t))
            # Keep the cdata callback alive through iperf_run_client().
            _ = callback

            json_text: str | None = callback_payloads[-1] if callback_payloads else None
            if json_text is None:
                cjson = lib.iperf_get_test_json_output_string(t)
                if cjson != ffi.NULL:
                    json_text = ffi.string(cjson).decode()

            if json_text is not None:
                raw = json.loads(json_text)
                end = raw.get("end", {})

                # minimal typed mapping into Result
                def _sum(d) -> SumStats | None:
                    if not d:
                        return None
                    return SumStats.model_validate(
                        {
                            "bits_per_second": d.get("bits_per_second", 0.0),
                            "retransmits": d.get("retransmits"),
                            "lost_percent": d.get("lost_percent"),
                            "jitter_ms": d.get("jitter_ms"),
                        }
                    )

                end_model = EndStats(
                    sum_sent=_sum(end.get("sum_sent")),
                    sum_received=_sum(end.get("sum_received")),
                )
                return Result(ok=True, raw=raw, end=end_model)
            return Result(ok=False, error="No JSON returned by libiperf")
        except IperfError as e:
            return Result(ok=False, error=str(e))
        finally:
            lib.iperf_free_test(t)

    async def arun(self) -> Result:
        """Run the iperf3 test asynchronously and return the result."""
        loop = asyncio.get_running_loop()
        # use functools.partial to provide a zero-arg callable so static analyzers
        # don't complain about unfilled *args parameter on run_in_executor
        return await loop.run_in_executor(None, partial(self.run))
