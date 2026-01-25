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


class Client:
    """Client for running iperf3 tests using the provided configuration."""

    def __init__(self, cfg: ClientConfig):
        """Initialize the client with a configuration object."""
        self.cfg = cfg

    def run(self) -> Result:
        """Run the iperf3 test synchronously and return the result."""
        t = lib.iperf_new_test()
        if t == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        try:
            _check(lib.iperf_defaults(t))
            lib.iperf_set_test_role(t, b"c")
            _set_str(lib.iperf_set_test_server_hostname, t, str(self.cfg.server))
            lib.iperf_set_test_server_port(t, int(self.cfg.port))
            lib.iperf_set_test_duration(t, int(self.cfg.duration))
            if self.cfg.omit:
                lib.iperf_set_test_omit(t, int(self.cfg.omit))
            if self.cfg.parallel and self.cfg.parallel > 1:
                lib.iperf_set_test_num_streams(t, int(self.cfg.parallel))
            if self.cfg.blksize:
                lib.iperf_set_test_blksize(t, int(self.cfg.blksize))
            if self.cfg.tos is not None:
                lib.iperf_set_test_tos(t, int(self.cfg.tos))

            # reverse / bidirectional
            if self.cfg.reverse:
                lib.iperf_set_test_reverse(t, 1)
            if self.cfg.bidirectional:
                if not _maybe_set("iperf_set_test_bidirectional", t, 1):
                    raise UnsupportedFeatureError("Bidirectional not supported by this libiperf")

            # mptcp
            if self.cfg.mptcp:
                if not _maybe_set("iperf_set_test_mptcp", t, 1):
                    raise UnsupportedFeatureError("MPTCP not supported by this libiperf")

            # protocol-specific (UDP bitrate is optional setter name)
            if self.cfg.protocol == Protocol.UDP:
                # Numeric `rate` (bits per second) is the canonical lib API.
                # Only support numeric rate in the lib backend; tests and
                # callers should provide `rate: int`. If rate is absent,
                # do not attempt to set bitrate on the lib path.
                rate = getattr(self.cfg, "rate", None)
                if rate is not None:
                    try:
                        rate_bps = int(rate)
                    except (TypeError, ValueError) as exc:
                        raise IperfError(f"invalid rate value: {rate}") from exc

                    # some libs expose iperf_set_test_bitrate, others expose
                    # a more generic iperf_set_test_rate; accept either.
                    fn = _try_set("iperf_set_test_bitrate") or _try_set("iperf_set_test_rate")
                    if fn is None:
                        raise UnsupportedFeatureError(
                            "This lib lacks 'iperf_set_test_bitrate' or 'iperf_set_test_rate'"
                        )
                    # pass integer value to the C API
                    fn(t, rate_bps)

            # ensure JSON output
            if not _maybe_set("iperf_set_test_json_output", t, 1):
                # some extremely old libs may lack JSON setter; we rely on JSON for parsing
                raise UnsupportedFeatureError("This libiperf lacks JSON output support")

            _check(lib.iperf_run_client(t))
            cjson = lib.iperf_get_test_json_output_string(t)
            if cjson != ffi.NULL:
                raw = json.loads(ffi.string(cjson).decode())
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
