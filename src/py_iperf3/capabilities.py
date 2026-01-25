from __future__ import annotations

from .ffi.api import lib


def has_symbol(name: str) -> bool:
    try:
        getattr(lib, name)
        return True
    except AttributeError:
        return False


HAS_BIDIR = has_symbol("iperf_set_test_bidirectional")
HAS_JSON_STREAM = has_symbol("iperf_set_test_json_stream")
HAS_MPTCP = has_symbol("iperf_set_test_mptcp")
