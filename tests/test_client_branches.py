"""Unit tests for iperf3 client code branches and setters."""

import importlib

from iperf3_lib.result import Result


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
        return val


class RecorderLib:
    """Recorder lib for capturing setter calls and simulating libiperf."""

    def __init__(self, *, make_json=True, run_client_ret=0):
        """Initialize RecorderLib with options for JSON and run_client_ret."""
        self.i_errno = 0
        self._record = {}
        self._make_json = make_json
        self._run_client_ret = run_client_ret

    def iperf_new_test(self):
        """Simulate iperf_new_test call."""
        return 1

    def iperf_defaults(self, t):
        """Simulate iperf_defaults call."""
        return 0

    def iperf_set_test_role(self, t, c):
        """Record test role setter."""
        self._record["role"] = c

    def iperf_set_test_server_hostname(self, t, s):
        """Record test server hostname setter."""
        self._record["hostname"] = s

    def iperf_set_test_server_port(self, t, p):
        """Record test server port setter."""
        self._record["port"] = p

    def iperf_set_test_duration(self, t, d):
        """Record test duration setter."""
        self._record["duration"] = d

    def iperf_set_test_omit(self, t, o):
        """Record test omit setter."""
        self._record["omit"] = o

    def iperf_set_test_num_streams(self, t, n):
        """Record test num_streams setter."""
        self._record["parallel"] = int(n)

    def iperf_set_test_blksize(self, t, b):
        """Record test blksize setter."""
        self._record["blksize"] = int(b)

    def iperf_set_test_tos(self, t, tos):
        """Record test tos setter."""
        self._record["tos"] = int(tos)

    def iperf_set_test_reverse(self, t, v):
        """Record test reverse setter."""
        self._record["reverse"] = int(v)

    def iperf_set_test_bidirectional(self, t, v):
        """Record test bidirectional setter."""
        self._record["bidir"] = int(v)

    def iperf_set_test_mptcp(self, t, v):
        """Record test mptcp setter."""
        self._record["mptcp"] = int(v)

    def iperf_set_test_json_output(self, t, v):
        """Record test json_output setter."""
        self._record["json"] = int(v)

    def iperf_run_client(self, t):
        """Simulate iperf_run_client call."""
        return int(self._run_client_ret)

    def iperf_get_test_json_output_string(self, t):
        """Simulate iperf_get_test_json_output_string call."""
        if not self._make_json:
            return self._make_json
        raw = b'{"end": {"sum_sent": {"bits_per_second": 1234.0}}}'
        return raw

    def iperf_strerror(self, errno):
        """Simulate iperf_strerror call."""
        return b"err"

    def iperf_free_test(self, t):
        """Simulate iperf_free_test call."""
        return None


def _setup_and_run(monkeypatch, lib, cfg_kwargs):
    """Helper to patch client and run with given lib and config."""
    import iperf3_lib.ffi.api as api_mod

    dummy_ffi = DummyFFI()
    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", lib)

    # reload client module so it picks updated api objects
    import iperf3_lib.iperf_client as client_mod

    importlib.reload(client_mod)

    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", **cfg_kwargs)
    res = Client(cfg).run()
    return res, lib._record


def test_client_bidirectional(monkeypatch):
    """Test bidirectional setter logic in client."""
    r = RecorderLib()
    res, record = _setup_and_run(monkeypatch, r, {"bidirectional": True})
    assert isinstance(res, Result)
    # json output present => ok True
    assert res.ok is True
    assert record.get("bidir") == 1


def test_client_mptcp(monkeypatch):
    """Test MPTCP setter logic in client."""
    r = RecorderLib()
    res, record = _setup_and_run(monkeypatch, r, {"mptcp": True})
    assert isinstance(res, Result)
    assert res.ok is True
    assert record.get("mptcp") == 1


def test_client_setters(monkeypatch):
    """Test all setter logic in client."""
    r = RecorderLib()
    res, record = _setup_and_run(
        monkeypatch,
        r,
        {"parallel": 4, "blksize": 1500, "tos": 2, "omit": 1},
    )
    assert isinstance(res, Result)
    assert res.ok is True
    assert record.get("parallel") == 4
    assert record.get("blksize") == 1500
    assert record.get("tos") == 2
    assert record.get("omit") == 1


def test_client_run_error(monkeypatch):
    """Test error handling in client run logic."""
    # Simulate iperf_run_client returning negative -> client returns Result(ok=False)
    r = RecorderLib(run_client_ret=-1)
    res, record = _setup_and_run(monkeypatch, r, {})
    assert isinstance(res, Result)
    assert res.ok is False
