"""Unit test for has_symbol and feature detection in iperf3_lib.capabilities."""

import importlib
from types import SimpleNamespace

import pytest

from iperf3_lib.exceptions import IperfLibraryError


def test_has_symbol_monkeypatched(monkeypatch):
    """Test has_symbol and feature flags with monkeypatched lib."""
    # Create a fake lib object with different attributes
    fake_lib = SimpleNamespace()
    fake_lib.iperf_set_test_bidirectional = lambda *a, **k: None
    # monkeypatch the ffi.api.lib object before importing capabilities
    import iperf3_lib.ffi.api as api

    monkeypatch.setattr(api, "lib", fake_lib)

    # reload capabilities so it picks up the patched lib
    import iperf3_lib.capabilities as caps

    importlib.reload(caps)

    assert caps.HAS_BIDIR is True
    assert caps.HAS_MPTCP is False
    assert caps.HAS_JSON_STREAM is False
    # ensure a non-existent symbol is false
    assert caps.has_symbol("non_existing_symbol") is False


class UnavailableLibrary:
    """Fail every lookup without doing native work."""

    def __init__(self, error):
        """Keep the requested probe failure."""
        self.error = error
        self.lookups = []

    def __getattr__(self, name):
        """Record and reject attempted native access."""
        self.lookups.append(name)
        raise self.error


def test_import_and_offline_report_do_not_load_native(monkeypatch):
    """Offline inspection must work where the native library cannot be loaded."""
    import iperf3_lib.capabilities as caps
    import iperf3_lib.ffi.api as api

    native = UnavailableLibrary(AssertionError("unexpected native load"))
    monkeypatch.setattr(api, "lib", native)
    importlib.reload(caps)
    report = caps.get_capabilities(probe_native=False)
    assert native.lookups == []
    assert report.library.state == "unprobed"
    assert report.execution.status == "not_provided"
    assert report.tested_native_versions == ("3.19.1", "3.21")
    assert report.tested_platforms == ("Linux",)
    assert all(feature.runtime == "not_run" for feature in report.features)
    assert all(
        symbol.state == "unknown" for feature in report.features for symbol in feature.symbols
    )
    assert caps.HAS_MPTCP is False and caps.HAS_JSON_STREAM is False
    assert native.lookups  # Legacy flags explicitly probe symbols, unlike the offline report.


@pytest.mark.parametrize(
    "error,state",
    [
        (IperfLibraryError("not installed"), "unavailable"),
        (OSError("missing file"), "unavailable"),
        (RuntimeError("probe broke"), "error"),
    ],
)
def test_probe_failures_remain_distinct_from_missing_symbols(monkeypatch, error, state):
    """A library load/probe failure never says native symbols are absent."""
    import iperf3_lib.capabilities as caps
    import iperf3_lib.ffi.api as api

    monkeypatch.setattr(api, "lib", UnavailableLibrary(error))
    report = caps.get_capabilities()
    assert report.library.state == state
    assert report.library.diagnostics
    assert all(
        symbol.state == "unknown" for feature in report.features for symbol in feature.symbols
    )
    assert caps.has_symbol("anything") is False


def test_capabilities_separate_present_absent_undeclared_and_wrapper_support(monkeypatch):
    """Public-header knowledge and present symbols do not claim exposed wrapper support."""
    import iperf3_lib.capabilities as caps
    import iperf3_lib.ffi.api as api

    native = SimpleNamespace(
        iperf_get_iperf_version=lambda: b"3.21",
        iperf_set_test_json_stream=lambda: None,
        iperf_set_test_pacing_timer=lambda: None,
    )
    monkeypatch.setattr(api, "lib", native)
    monkeypatch.setattr(api, "ffi", SimpleNamespace(NULL=None, string=lambda value: value))
    report = caps.get_capabilities()
    assert report.library.state == "available" and report.library.version == "3.21"
    features = {feature.name: feature for feature in report.features}
    assert features["bidirectional"].symbols[0].state == "absent"
    assert features["json_stream"].wrapper == "supported"
    assert features["json_stream"].symbols[0].state == "present"
    assert features["pacing_timer"].wrapper == "supported"
    assert features["pacing_timer"].symbols[0].declared is True
    assert features["pacing_timer"].symbols[0].state == "present"
    assert features["hard_cancellation"].wrapper == "unsupported"
    assert caps.HAS_PROTOCOL_SELECTION is False
    with pytest.raises(AttributeError):
        _ = caps.DOES_NOT_EXIST


@pytest.mark.parametrize("value,message", [(None, "null"), (b"", "empty")])
def test_empty_native_version_is_explicit(monkeypatch, value, message):
    """Successful loading without a usable version retains a diagnostic."""
    import iperf3_lib.capabilities as caps
    import iperf3_lib.ffi.api as api

    monkeypatch.setattr(api, "lib", SimpleNamespace(iperf_get_iperf_version=lambda: value))
    monkeypatch.setattr(api, "ffi", SimpleNamespace(NULL=None, string=lambda value: value))
    report = caps.get_capabilities()
    assert report.library.state == "available"
    assert report.library.version is None
    assert message in report.library.diagnostics[0]


def test_version_decode_and_symbol_probe_errors_are_retained(monkeypatch):
    """Unexpected per-symbol failures remain unknown, with the original failure context."""
    import iperf3_lib.capabilities as caps
    import iperf3_lib.ffi.api as api

    class BrokenSymbol:
        """Load a version successfully but reject feature probes unexpectedly."""

        def iperf_get_iperf_version(self):
            """Supply a valid version pointer stand-in."""
            return b"3.21"

        def __getattr__(self, name):
            """Simulate an unexpected lookup failure after loading."""
            raise RuntimeError("symbol probe broken")

    monkeypatch.setattr(api, "lib", BrokenSymbol())
    monkeypatch.setattr(api, "ffi", SimpleNamespace(NULL=None, string=lambda value: value))
    report = caps.get_capabilities()
    assert report.library.state == "available"
    assert report.features[0].symbols[0].state == "unknown"
    assert "symbol probe broken" in report.features[0].symbols[0].diagnostic
    monkeypatch.setattr(api, "lib", SimpleNamespace(iperf_get_iperf_version=lambda: b"\xff"))
    assert caps.get_capabilities().library.state == "error"


def test_supplied_result_evidence_does_not_upgrade_static_capability_claims():
    """Keep execution-native provenance separate from the local library probe."""
    from iperf3_lib.capabilities import get_capabilities
    from iperf3_lib.result import Result, result_from_iperf_json

    result = result_from_iperf_json(
        {
            "start": {"version": "iperf 3.19.1", "test_start": {"protocol": "UDP"}},
            "end": {"sum_sent": {"bits_per_second": 1}},
        }
    )
    report = get_capabilities(probe_native=False, result=result)
    assert report.execution.status == "completed"
    assert report.execution.native_version == "iperf 3.19.1"
    assert report.execution.verified_settings == ("protocol",)
    assert report.library.version is None
    assert all(feature.runtime == "not_run" for feature in report.features)
    assert get_capabilities(probe_native=False, result=Result(True)).execution.status == "unknown"
    with pytest.raises(TypeError):
        get_capabilities(probe_native=1)
    with pytest.raises(TypeError):
        get_capabilities(result="bad")
