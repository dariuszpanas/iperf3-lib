"""Unit tests for iperf3 client parsing logic with mocked FFI/lib."""

import json

from iperf3_lib.result import Result, SumStats


class DummyFFI:
    """Dummy FFI class for simulating cffi in tests."""

    def __init__(self):
        """Initialize DummyFFI with NULL attribute."""
        self.NULL = 0

    def string(self, s):
        """Return the input string (simulate cffi.string)."""
        return s

    def new(self, spec, val):
        """Return the value (simulate cffi.new)."""
        # mimic cffi.new("char[]", b"...") by returning the bytes buffer
        return val


class DummyLib:
    """Dummy lib class for simulating libiperf in tests."""

    def __init__(self):
        """Initialize DummyLib with i_errno attribute."""
        self.i_errno = 0

    def iperf_new_test(self):
        """Simulate iperf_new_test call."""
        return 1

    def iperf_defaults(self, t):
        """Simulate iperf_defaults call."""
        return 0

    def iperf_set_test_role(self, t, c):
        """Simulate iperf_set_test_role call."""
        return None

    def iperf_set_test_server_hostname(self, t, s):
        """Simulate iperf_set_test_server_hostname call."""
        return None

    def iperf_set_test_server_port(self, t, p):
        """Simulate iperf_set_test_server_port call."""
        return None

    def iperf_set_test_duration(self, t, d):
        """Simulate iperf_set_test_duration call."""
        return None

    def iperf_set_test_json_output(self, t, v):
        """Simulate iperf_set_test_json_output call."""
        return None

    def iperf_run_client(self, t):
        """Simulate iperf_run_client call."""
        return 0

    def iperf_get_test_json_output_string(self, t):
        """Simulate iperf_get_test_json_output_string call."""
        # return a C-like pointer: we'll just return bytes
        raw = json.dumps({"end": {"sum_sent": {"bits_per_second": 1000.0}}})
        return raw.encode()

    def iperf_free_test(self, t):
        """Simulate iperf_free_test call."""
        return None


def test_client_parsing(monkeypatch):
    """Test client parsing logic with monkeypatched FFI and lib."""
    from iperf3_lib.ffi import api as api_mod

    # monkeypatch ffi and lib
    dummy_ffi = DummyFFI()
    dummy_lib = DummyLib()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    # reload the client module so it picks up the patched api module-level objects
    import importlib

    import iperf3_lib.iperf_client as client_mod

    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig, Protocol
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", duration=1, protocol=Protocol.TCP)
    res = Client(cfg).run()

    assert isinstance(res, Result)
    assert res.ok is True
    assert isinstance(res.end.sum_sent, SumStats) or res.end.sum_sent is None
