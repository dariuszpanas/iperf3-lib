"""Unit tests for iperf3 client code branches and setters."""

import importlib

import pytest

from iperf3_lib.config import Protocol
from iperf3_lib.exceptions import UnsupportedFeatureError
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


class CallbackFFI(DummyFFI):
    """Dummy FFI that can construct a Python stand-in for a C callback."""

    def callback(self, signature, function):
        """Return the Python callable while recording the expected signature."""
        assert signature == "void(iperf_test *, char *)"
        return function


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

    def set_protocol(self, t, protocol_id):
        """Record the selected protocol."""
        self._record["protocol_id"] = int(protocol_id)
        return 0

    def iperf_get_test_protocol_id(self, t):
        """Return the selected protocol."""
        return self._record["protocol_id"]

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

    def iperf_set_test_json_output(self, t, v):
        """Record test json_output setter."""
        self._record["json"] = int(v)

    def iperf_set_test_rate(self, t, rate):
        """Record test rate setter."""
        self._record["rate"] = int(rate)

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


def _setup_and_run(monkeypatch, lib, cfg_kwargs, dummy_ffi=None):
    """Helper to patch client and run with given lib and config."""
    import iperf3_lib.ffi.api as api_mod

    dummy_ffi = dummy_ffi or DummyFFI()
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
    """Test that MPTCP is rejected by the direct ABI backend."""
    r = RecorderLib()
    with pytest.raises(UnsupportedFeatureError, match="direct libiperf ABI"):
        _setup_and_run(monkeypatch, r, {"mptcp": True})


def test_client_json_stream(monkeypatch):
    """Test that streaming JSON is rejected by the direct ABI backend."""
    r = RecorderLib()
    with pytest.raises(UnsupportedFeatureError, match="direct libiperf ABI"):
        _setup_and_run(monkeypatch, r, {"json_stream": True})


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


@pytest.mark.parametrize(
    ("protocol", "protocol_id", "default_blksize", "default_rate"),
    [
        (Protocol.TCP, 1, None, None),
        (Protocol.UDP, 2, 0, 1024 * 1024),
        (Protocol.SCTP, 12, 64 * 1024, None),
    ],
)
def test_client_protocol_defaults(
    monkeypatch, protocol, protocol_id, default_blksize, default_rate
):
    """Apply every protocol and its protocol-specific direct-ABI defaults."""
    res, record = _setup_and_run(monkeypatch, RecorderLib(), {"protocol": protocol})

    assert res.ok is True
    assert record["protocol_id"] == protocol_id
    assert record.get("blksize") == default_blksize
    assert record.get("rate") == default_rate


def test_client_explicit_rate_applies_to_tcp(monkeypatch):
    """Apply an explicit rate through the generic setter for non-UDP protocols."""
    res, record = _setup_and_run(monkeypatch, RecorderLib(), {"rate": 42})

    assert res.ok is True
    assert record["rate"] == 42


def test_client_udp_zero_rate_overrides_default(monkeypatch):
    """Preserve an explicit unlimited UDP rate instead of applying the default."""
    res, record = _setup_and_run(
        monkeypatch,
        RecorderLib(),
        {"protocol": Protocol.UDP, "rate": 0},
    )

    assert res.ok is True
    assert record["rate"] == 0


def test_client_uses_json_callback_when_supported(monkeypatch):
    """Capture final native JSON with the callback instead of native stdout."""

    class CallbackLib(RecorderLib):
        def iperf_set_test_json_callback(self, t, callback):
            self._callback = callback
            self._record["json_callback"] = True

        def iperf_run_client(self, t):
            self._callback(t, b'{"end": {"sum_sent": {"bits_per_second": 99.0}}}')
            return 0

        def iperf_get_test_json_output_string(self, t):
            return 0

    res, record = _setup_and_run(monkeypatch, CallbackLib(), {}, CallbackFFI())

    assert res.ok is True
    assert res.raw["end"]["sum_sent"]["bits_per_second"] == 99.0
    assert record["json_callback"] is True
