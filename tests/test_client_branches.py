"""Unit tests for iperf3 client code branches and setters."""

import importlib
import json
from types import SimpleNamespace

import pytest

from iperf3_lib.config import Protocol
from iperf3_lib.result import Result


class DummyFFI:
    """Dummy FFI class for simulating cffi in tests."""

    def __init__(self):
        """Initialize DummyFFI with NULL attribute."""
        self.NULL = 0

    def string(self, s, maxlen=None):
        """Return the input string (simulate cffi.string)."""
        return s[:maxlen]

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


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (b'{"error":"native failure","end":{"sum_sent":{"bits_per_second":42}}}', False),
        (b'{"start":{},"end":{}}', False),
        (b'{"end":{"sum_sent":{"bits_per_second":"invalid"}}}', True),
    ],
)
def test_client_frees_native_test_once_after_failed_or_invalid_json(
    monkeypatch, payload, expected_error
):
    """Preserve native ownership when parsing returns failure or rejects malformed output."""

    class PayloadLib(RecorderLib):
        """Return a controlled native document and record native cleanup."""

        def iperf_get_test_json_output_string(self, test):
            """Return the document selected for this error path."""
            return payload

        def iperf_free_test(self, test):
            """Count frees of the non-null native test."""
            assert test == 1
            self._record["free_count"] = self._record.get("free_count", 0) + 1

    native = PayloadLib()
    if expected_error:
        with pytest.raises(ValueError):
            _setup_and_run(monkeypatch, native, {})
    else:
        result, _ = _setup_and_run(monkeypatch, native, {})
        assert not result.ok
        assert result.error
        assert result.completed_at_seconds is not None
        assert result.reporting_role == "client"
    assert native._record["free_count"] == 1


def test_client_bidirectional(monkeypatch):
    """Test bidirectional setter logic in client."""
    r = RecorderLib()
    res, record = _setup_and_run(monkeypatch, r, {"bidirectional": True})
    assert isinstance(res, Result)
    # json output present => ok True
    assert res.ok is True
    assert record.get("bidir") == 1


def test_client_mptcp(monkeypatch):
    """MPTCP is configured in an isolated native worker."""
    import iperf3_lib._execution as execution

    calls = []
    monkeypatch.setattr(
        execution,
        "run_worker",
        lambda role, options, **kwargs: calls.append(options) or Result(ok=True),
    )
    r = RecorderLib()
    result, _ = _setup_and_run(monkeypatch, r, {"mptcp": True})
    assert result.ok and calls[0]["mptcp"] is True


def test_client_json_stream(monkeypatch):
    """Streaming produces a completed result through the isolated worker."""
    import iperf3_lib._execution as execution

    calls = []
    monkeypatch.setattr(
        execution,
        "run_worker",
        lambda role, options, **kwargs: calls.append(options) or Result(ok=True),
    )
    r = RecorderLib()
    result, _ = _setup_and_run(monkeypatch, r, {"json_stream": True})
    assert result.ok and calls[0]["json_stream"] is True


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


@pytest.mark.parametrize("getter", [0, b'{"end":{"sum_sent":{"bits_per_second":99}}}'])
def test_direct_callback_copy_failure_requires_independent_document(monkeypatch, getter):
    """A callback overflow cannot reuse stale bytes as a successful final result."""
    from iperf3_lib import _event_capture

    class CallbackLib(RecorderLib):
        def iperf_set_test_json_callback(self, t, callback):
            self.callback = callback

        def iperf_run_client(self, t):
            self.callback(t, b'{"end":{}}')
            self.callback(t, b"x" * (_event_capture.MAX_CAPTURE_BYTES + 1))
            return 0

        def iperf_get_test_json_output_string(self, t):
            return getter

        def iperf_free_test(self, t):
            self._record["frees"] = self._record.get("frees", 0) + 1
            # The installed Python/CFFI callback is still strongly owned here.
            self.callback(t, b'{"end":{}}')

    result, record = _setup_and_run(monkeypatch, CallbackLib(), {}, CallbackFFI())
    assert record["frees"] == 1
    if getter:
        assert result.ok and result.raw == json.loads(getter)
        assert any(item.code == "execution.capture_recovered" for item in result.diagnostics)
    else:
        assert not result.ok and "Cannot capture native JSON" in result.error


def test_direct_getter_overflow_frees_native_test_once(monkeypatch):
    """Getter copying has the callback bound even when callback APIs are absent."""
    from iperf3_lib import _event_capture
    from iperf3_lib.exceptions import IperfLibraryError

    class BoundedLib(RecorderLib):
        def iperf_get_test_json_output_string(self, t):
            return b"x" * (_event_capture.MAX_CAPTURE_BYTES + 1)

        def iperf_free_test(self, t):
            self._record["frees"] = self._record.get("frees", 0) + 1

    native = BoundedLib()
    with pytest.raises(IperfLibraryError, match="capture byte limit"):
        _setup_and_run(monkeypatch, native, {})
    assert native._record["frees"] == 1


@pytest.mark.parametrize("replacement", ["callback", "getter"])
def test_direct_later_complete_document_cannot_erase_native_error(monkeypatch, replacement):
    """A known error survives later valid JSON, including independent getter recovery."""
    clean = b'{"end":{"sum_sent":{"bits_per_second":99}}}'

    class CallbackLib(RecorderLib):
        def iperf_set_test_json_callback(self, t, callback):
            self.callback = callback

        def iperf_run_client(self, t):
            self.callback(t, b'{"error":"observed native failure","end":{}}')
            self.callback(t, clean if replacement == "callback" else b"invalid JSON")
            return 0

        def iperf_get_test_json_output_string(self, t):
            return clean

        def iperf_free_test(self, t):
            self._record["frees"] = self._record.get("frees", 0) + 1

    result, record = _setup_and_run(monkeypatch, CallbackLib(), {}, CallbackFFI())
    assert not result.ok and result.error == "observed native failure"
    assert result.execution.status == "failed"
    assert record["frees"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        b'{"end":{},"end":{"sum_sent":{"bits_per_second":99}}}',
        b'{"extra":NaN,"end":{"sum_sent":{"bits_per_second":99}}}',
    ],
)
def test_direct_getter_rejects_non_strict_native_json(monkeypatch, payload):
    """Direct final JSON follows the same duplicate/nonfinite rules as worker capture."""

    class InvalidLib(RecorderLib):
        def iperf_get_test_json_output_string(self, t):
            return payload

        def iperf_free_test(self, t):
            self._record["frees"] = self._record.get("frees", 0) + 1

    native = InvalidLib()
    with pytest.raises(ValueError):
        _setup_and_run(monkeypatch, native, {})
    assert native._record["frees"] == 1


def test_client_records_requested_native_and_observed_execution_metadata(monkeypatch):
    """Keep the request, returned settings, and actual operation clocks separate."""
    import iperf3_lib.ffi.api as api_mod
    import iperf3_lib.iperf_client as client_mod
    from iperf3_lib.config import ClientConfig

    raw = {
        "start": {
            "version": "iperf 3.21",
            "system_info": "fixture-native-system",
            "timestamp": {"timesecs": 90},
            "connecting_to": {"host": "127.0.0.1", "port": 5201},
            "test_start": {
                "protocol": "TCP",
                "duration": 10,
                "num_streams": 2,
                "reverse": 0,
                "bidir": 0,
                "target_bitrate": 500000,
            },
        },
        "end": {"sum_sent": {"bits_per_second": 800}},
    }

    class MetadataLib(RecorderLib):
        """Expose independently returned native configuration and timing evidence."""

        def iperf_get_test_json_output_string(self, test):
            """Return a result with native settings deliberately different from the request."""
            return json.dumps(raw).encode()

    wall = iter([100.0, 112.5])
    elapsed = iter([200.0, 213.0])
    native = MetadataLib()
    monkeypatch.setattr(api_mod, "ffi", DummyFFI())
    monkeypatch.setattr(api_mod, "lib", native)
    importlib.reload(client_mod)
    monkeypatch.setattr(
        client_mod,
        "time",
        SimpleNamespace(time=lambda: next(wall), monotonic=lambda: next(elapsed)),
    )
    result = client_mod.Client(ClientConfig(server="127.0.0.1", parallel=2, rate=400000)).run()
    record = native._record

    metadata = result.execution
    assert metadata.status == "completed"
    assert metadata.native_version == "iperf 3.21"
    assert metadata.native_system_info == "fixture-native-system"
    assert metadata.python_version
    assert metadata.platform
    assert metadata.timing.started_at_seconds == 100
    assert metadata.timing.completed_at_seconds == 112.5
    assert metadata.timing.elapsed_seconds == 13
    assert metadata.timing.native_started_at_seconds == 90
    assert metadata.timing.requested_duration_seconds == 10
    assert metadata.configuration.requested["rate"] == record["rate"] == 400000
    assert metadata.configuration.effective["rate"].value == 500000
    assert metadata.configuration.effective["rate"].state == "verified"
    assert metadata.configuration.effective["json_stream"].state == "unavailable"
    assert metadata.configuration.effective["json_stream"].value is None
    assert any(d.code == "configuration.difference" for d in result.diagnostics)


def test_client_executes_detached_configuration_snapshot(monkeypatch):
    """Caller mutations after admission cannot change the run or its recorded request."""
    import iperf3_lib.ffi.api as api_mod
    import iperf3_lib.iperf_client as client_mod
    from iperf3_lib.config import ClientConfig

    config = ClientConfig(server="127.0.0.1", duration=2, rate=400000)

    class MutatingLib(RecorderLib):
        """Mutate the original Python configuration after the native test is allocated."""

        def iperf_defaults(self, test):
            """Change caller-owned values during setup to exercise the snapshot boundary."""
            config.duration = 50
            config.rate = 1
            return 0

    native = MutatingLib()
    monkeypatch.setattr(api_mod, "ffi", DummyFFI())
    monkeypatch.setattr(api_mod, "lib", native)
    importlib.reload(client_mod)
    result = client_mod.Client(config).run()

    assert config.duration == 50
    assert native._record["duration"] == 2
    assert native._record["rate"] == 400000
    assert result.execution.configuration.requested["duration"] == 2
    assert result.execution.configuration.requested["rate"] == 400000
    config.rate = 123
    assert result.execution.configuration.requested["rate"] == 400000


def test_client_revalidates_mutated_config_before_native_allocation(monkeypatch):
    """Invalid post-construction values are rejected before any native resources exist."""
    import iperf3_lib.ffi.api as api_mod
    import iperf3_lib.iperf_client as client_mod
    from iperf3_lib.config import ClientConfig

    config = ClientConfig(server="127.0.0.1")
    config.duration = -1

    class UnallocatedLib(RecorderLib):
        """Fail if native allocation occurs before configuration validation."""

        def iperf_new_test(self):
            """Prevent an invalid request from reaching native code."""
            pytest.fail("invalid request allocated a native test")

    monkeypatch.setattr(api_mod, "ffi", DummyFFI())
    monkeypatch.setattr(api_mod, "lib", UnallocatedLib())
    importlib.reload(client_mod)
    with pytest.raises(ValueError, match="duration"):
        client_mod.Client(config).run()


def test_native_failure_retains_returned_json_and_request_metadata(monkeypatch):
    """A negative native status must not discard its available result document."""
    native = RecorderLib(run_client_ret=-1)
    result, _ = _setup_and_run(monkeypatch, native, {"duration": 2})

    assert not result.ok
    assert result.error == "err"
    assert result.raw["end"]["sum_sent"]["bits_per_second"] == 1234.0
    assert result.execution.status == "failed"
    assert result.execution.configuration.requested["duration"] == 2
    assert result.execution.timing.elapsed_seconds >= 0
    assert any(d.code == "execution.native_error" for d in result.diagnostics)


@pytest.mark.parametrize("failed", [False, True])
def test_client_rate_intent_applied_once_with_detached_artifact_metadata(monkeypatch, failed):
    """Resolve once, retain caller intent, and free the test once on either native outcome."""
    import iperf3_lib.ffi.api as api_mod
    import iperf3_lib.iperf_client as client_mod
    from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.intent import RateIntent

    cfg = ClientConfig("127.0.0.1", parallel=3)

    class IntentLib(RecorderLib):
        """Count rate setter and cleanup while returning matching native rate evidence."""

        def iperf_set_test_rate(self, test, rate):
            """Apply only the resolved rate and mutate caller state after admission."""
            self._record["rate_count"] = self._record.get("rate_count", 0) + 1
            self._record["rate"] = rate
            cfg.parallel = 100

        def iperf_get_test_json_output_string(self, test):
            """Report the native setting independently of requested metadata."""
            return b'{"start":{"test_start":{"reverse":0,"target_bitrate":3,"num_streams":3}},"end":{"sum_sent":{"bits_per_second":1}}}'

        def iperf_free_test(self, test):
            """Count exact native lifecycle cleanup."""
            self._record["free_count"] = self._record.get("free_count", 0) + 1

    native = IntentLib(run_client_ret=-1 if failed else 0)
    monkeypatch.setattr(api_mod, "ffi", DummyFFI())
    monkeypatch.setattr(api_mod, "lib", native)
    importlib.reload(client_mod)
    result = client_mod.Client(cfg, rate_intent=RateIntent(aggregate_bps_per_direction=10)).run()
    assert native._record["rate_count"] == native._record["free_count"] == 1
    assert native._record["rate"] == 3
    assert result.ok is not failed
    assert result.execution.configuration.requested["rate"] == 3
    assert result.execution.configuration.requested["parallel"] == 3
    assert result.execution.configuration.effective["rate"].value == 3
    assert not any(d.code == "configuration.difference" for d in result.diagnostics)
    extension = result.extensions["iperf3_lib.rate_intent"]
    assert extension["caller_config"]["rate"] is None
    assert extension["caller_config"]["parallel"] == 3
    assert extension["intent"]["aggregate_bps_per_direction"] == 10
    assert extension["resolution"]["unused_bps_per_direction"] == 1
    assert (
        loads_artifact(dumps_artifact(artifact_from_result(result))).result.extensions
        == result.extensions
    )


def test_conflicting_rate_intent_is_rejected_before_native_allocation(monkeypatch):
    """Invalid rate admission must not allocate native resources."""
    import iperf3_lib.iperf_client as client_mod
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.intent import RateIntent

    monkeypatch.setattr(client_mod, "lib", None)
    with pytest.raises(ValueError, match="cannot both"):
        client_mod.Client(
            ClientConfig("host", rate=0), rate_intent=RateIntent(per_stream_bps=0)
        ).run()
