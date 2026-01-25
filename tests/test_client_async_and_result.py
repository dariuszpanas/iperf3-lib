import pytest

from py_iperf3.result import Result


class DummyFFI:
    def __init__(self):
        self.NULL = 0

    def string(self, s):
        return s

    def new(self, spec, val):
        return val


class AsyncLib:
    def __init__(self):
        self.i_errno = 0

    def iperf_new_test(self):
        return 1

    def iperf_defaults(self, t):
        return 0

    def iperf_set_test_role(self, t, c):
        return None

    def iperf_set_test_server_hostname(self, t, s):
        return None

    def iperf_set_test_server_port(self, t, p):
        return None

    def iperf_set_test_duration(self, t, d):
        return None

    def iperf_set_test_json_output(self, t, v):
        return None

    def iperf_run_client(self, t):
        return 0

    def iperf_get_test_json_output_string(self, t):
        raw = b'{"end": {"sum_sent": {"bits_per_second": 2000000.0}}}'
        return raw

    def iperf_free_test(self, t):
        return None


@pytest.mark.asyncio
async def test_client_arun_and_summary(monkeypatch):
    # patch ffi and lib
    import py_iperf3.ffi.api as api_mod
    import py_iperf3.libiperf_client as client_mod

    dummy_ffi = DummyFFI()
    dummy_lib = AsyncLib()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    importlib = __import__("importlib")
    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig, Protocol
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", duration=1, protocol=Protocol.TCP)
    res = await Client(cfg).arun()

    assert isinstance(res, Result)
    assert res.ok is True
    # summary_mbps should come from sum_sent bits_per_second (2,000,000 -> 2.0 Mbps)
    assert abs(res.summary_mbps - 2.0) < 0.001
