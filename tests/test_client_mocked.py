import json

from py_iperf3.result import Result, SumStats


class DummyFFI:
    def __init__(self):
        self.NULL = 0

    def string(self, s):
        return s

    def new(self, spec, val):
        # mimic cffi.new("char[]", b"...") by returning the bytes buffer
        return val


class DummyLib:
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
        # return a C-like pointer: we'll just return bytes
        raw = json.dumps({"end": {"sum_sent": {"bits_per_second": 1000.0}}})
        return raw.encode()

    def iperf_free_test(self, t):
        return None


def test_client_parsing(monkeypatch):
    from py_iperf3.ffi import api as api_mod

    # monkeypatch ffi and lib
    dummy_ffi = DummyFFI()
    dummy_lib = DummyLib()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    # reload the client module so it picks up the patched api module-level objects
    import importlib

    import py_iperf3.libiperf_client as client_mod

    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig, Protocol
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", duration=1, protocol=Protocol.TCP)
    res = Client(cfg).run()

    assert isinstance(res, Result)
    assert res.ok is True
    assert isinstance(res.end.sum_sent, SumStats) or res.end.sum_sent is None
