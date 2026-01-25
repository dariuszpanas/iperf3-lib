import importlib
import json

import pytest

from py_iperf3.config import Protocol
from py_iperf3.exceptions import IperfLibraryError, UnsupportedFeatureError
from py_iperf3.result import Result


class DummyFFI:
    def __init__(self):
        self.NULL = 0

    def string(self, s):
        return s

    def new(self, spec, val):
        return val


class BaseDummyLib:
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

    def iperf_run_client(self, t):
        return 0

    def iperf_get_test_json_output_string(self, t):
        raw = json.dumps({"end": {"sum_sent": {"bits_per_second": 1000.0}}})
        return raw.encode()

    def iperf_free_test(self, t):
        return None


def _reload_client_with(api_mod, dummy_ffi, dummy_lib):
    # patch api module and reload client module so it picks up new objects
    import py_iperf3.libiperf_client as client_mod

    importlib.reload(api_mod)
    # ensure module-level objects are updated
    api_mod.ffi = dummy_ffi
    api_mod.lib = dummy_lib
    importlib.reload(client_mod)
    return client_mod


def test_client_new_test_null(monkeypatch):
    from py_iperf3.ffi import api as api_mod

    dummy_ffi = DummyFFI()
    dummy_lib = BaseDummyLib()

    # make new_test return NULL
    def iperf_new_test_null():
        return dummy_ffi.NULL

    dummy_lib.iperf_new_test = iperf_new_test_null

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    import py_iperf3.libiperf_client as client_mod

    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1")
    with pytest.raises(IperfLibraryError):
        Client(cfg).run()


def test_client_no_json_support(monkeypatch):
    from py_iperf3.ffi import api as api_mod

    dummy_ffi = DummyFFI()

    # Create a lib that lacks iperf_set_test_json_output
    class LibNoJson(BaseDummyLib):
        def __init__(self):
            super().__init__()
            # deliberately don't add iperf_set_test_json_output

    dummy_lib = LibNoJson()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    import py_iperf3.libiperf_client as client_mod

    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1")
    with pytest.raises(UnsupportedFeatureError):
        Client(cfg).run()


def test_client_udp_bitrate(monkeypatch):
    from py_iperf3.ffi import api as api_mod

    dummy_ffi = DummyFFI()

    class LibUDP(BaseDummyLib):
        def iperf_set_test_bitrate(self, t, rate):
            # accept bitrate
            self._last_rate = rate

        def iperf_set_test_json_output(self, t, v):
            return None

    dummy_lib = LibUDP()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    import py_iperf3.libiperf_client as client_mod

    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", protocol=Protocol.UDP, rate=1000)
    res = Client(cfg).run()

    assert isinstance(res, Result)
    assert res.ok is True
    # ensure bitrate setter was called with integer
    assert getattr(dummy_lib, "_last_rate", None) == 1000
