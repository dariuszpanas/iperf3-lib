"""Async client tests and result parsing for iperf3-lib."""

import pytest

from iperf3_lib.result import Result


class DummyFFI:
    """Dummy FFI class for simulating cffi in async tests."""

    def __init__(self):
        """Initialize DummyFFI with NULL attribute."""
        self.NULL = 0

    def string(self, s):
        """Return the input string (simulate cffi.string)."""
        return s

    def new(self, spec, val):
        """Return the value (simulate cffi.new)."""
        return val


class AsyncLib:
    """Dummy lib for simulating async iperf3 client behavior."""

    def __init__(self):
        """Initialize AsyncLib with i_errno attribute."""
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
        raw = b'{"end": {"sum_sent": {"bits_per_second": 2000000.0}}}'
        return raw

    def iperf_free_test(self, t):
        """Simulate iperf_free_test call."""
        return None


@pytest.mark.asyncio
async def test_client_arun_and_summary(monkeypatch):
    """Test async client arun and summary_mbps property."""
    # patch ffi and lib
    import iperf3_lib.ffi.api as api_mod
    import iperf3_lib.iperf_client as client_mod

    dummy_ffi = DummyFFI()
    dummy_lib = AsyncLib()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    importlib = __import__("importlib")
    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig, Protocol
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", duration=1, protocol=Protocol.TCP)
    res = await Client(cfg).arun()

    assert isinstance(res, Result)
    assert res.ok is True
    # summary_mbps should come from sum_sent bits_per_second (2,000,000 -> 2.0 Mbps)
    assert abs(res.summary_mbps - 2.0) < 0.001
