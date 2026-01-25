"""Comprehensive tests for iperf3 client logic and edge cases."""

import importlib
import json

import pytest

from iperf3_lib.config import Protocol
from iperf3_lib.exceptions import IperfLibraryError, UnsupportedFeatureError
from iperf3_lib.result import Result


class DummyFFI:
    """Dummy FFI class for simulating cffi behavior in tests."""

    def __init__(self):
        """Initialize DummyFFI with NULL attribute."""
        self.NULL = 0

    def string(self, s):
        """Return the input string (simulate cffi.string)."""
        return s

    def new(self, spec, val):
        """Return the value (simulate cffi.new)."""
        return val


class BaseDummyLib:
    """Base dummy lib class for simulating libiperf in tests."""

    def __init__(self):
        """Initialize BaseDummyLib with i_errno attribute."""
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

    def iperf_run_client(self, t):
        """Simulate iperf_run_client call."""
        return 0

    def iperf_get_test_json_output_string(self, t):
        """Simulate iperf_get_test_json_output_string call."""
        raw = json.dumps({"end": {"sum_sent": {"bits_per_second": 1000.0}}})
        return raw.encode()

    def iperf_free_test(self, t):
        """Simulate iperf_free_test call."""
        return None


def _reload_client_with(api_mod, dummy_ffi, dummy_lib):
    """Patch api module and reload client module to pick up new objects."""
    # patch api module and reload client module so it picks up new objects
    import iperf3_lib.iperf_client as client_mod

    importlib.reload(api_mod)
    # ensure module-level objects are updated
    api_mod.ffi = dummy_ffi
    api_mod.lib = dummy_lib
    importlib.reload(client_mod)
    return client_mod


def test_client_new_test_null(monkeypatch):
    """Test client run with iperf_new_test returning NULL."""
    from iperf3_lib.ffi import api as api_mod

    dummy_ffi = DummyFFI()
    dummy_lib = BaseDummyLib()

    # make new_test return NULL
    def iperf_new_test_null():
        return dummy_ffi.NULL

    dummy_lib.iperf_new_test = iperf_new_test_null

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    import iperf3_lib.iperf_client as client_mod

    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1")
    with pytest.raises(IperfLibraryError):
        Client(cfg).run()


def test_client_no_json_support(monkeypatch):
    """Test client run with lib lacking JSON output support."""
    from iperf3_lib.ffi import api as api_mod

    dummy_ffi = DummyFFI()

    # Create a lib that lacks iperf_set_test_json_output
    class LibNoJson(BaseDummyLib):
        def __init__(self):
            super().__init__()
            # deliberately don't add iperf_set_test_json_output

    dummy_lib = LibNoJson()

    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", dummy_lib)

    import iperf3_lib.iperf_client as client_mod

    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1")
    with pytest.raises(UnsupportedFeatureError):
        Client(cfg).run()


def test_client_udp_bitrate(monkeypatch):
    """Test client run with UDP and bitrate set."""
    from iperf3_lib.ffi import api as api_mod

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

    import iperf3_lib.iperf_client as client_mod

    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", protocol=Protocol.UDP, rate=1000)
    res = Client(cfg).run()

    assert isinstance(res, Result)
    assert res.ok is True
    # ensure bitrate setter was called with integer
    assert getattr(dummy_lib, "_last_rate", None) == 1000
