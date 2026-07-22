"""Tests for FFI dynamic loading error handling in iperf3_lib."""

import pytest

from iperf3_lib.exceptions import IperfLibraryError


def test_dlopen_env_override(monkeypatch):
    """Test that _dlopen raises OSError if IPERF3_LIB env var is set to a bad path."""
    import iperf3_lib.ffi.api as api

    # Ensure env var branch is used
    monkeypatch.setenv("IPERF3_LIB", "/nonexistent/path/libiperf.so")

    # Make ffi.dlopen raise to simulate failure
    def raising_dlopen(path):
        raise OSError("dlopen failed")

    monkeypatch.setattr(api.ffi, "dlopen", raising_dlopen)

    with pytest.raises(OSError):
        api._dlopen()


def test_dlopen_all_names_fail(monkeypatch):
    """Test that _dlopen raises OSError if all possible names fail."""
    import iperf3_lib.ffi.api as api

    # Ensure env var not set
    monkeypatch.delenv("IPERF3_LIB", raising=False)

    # Make ffi.dlopen always raise
    def raising_dlopen(name):
        raise OSError("dlopen failed")

    monkeypatch.setattr(api.ffi, "dlopen", raising_dlopen)

    with pytest.raises(OSError):
        api._dlopen()


def test_lazy_library_defers_loading_and_wraps_errors(monkeypatch):
    """Do not call dlopen until a symbol is used, then expose a package error."""
    import iperf3_lib.ffi.api as api

    calls = []

    def raising_dlopen():
        calls.append(True)
        raise OSError("native library unavailable")

    monkeypatch.setattr(api, "_dlopen", raising_dlopen)
    lazy = api._LazyLibrary()
    assert calls == []

    with pytest.raises(IperfLibraryError, match="native library unavailable"):
        lazy.iperf_new_test()
    assert calls == [True]


def test_cdef_covers_supported_public_apis():
    """Declare every upstream public API used by the direct ABI backend."""
    from iperf3_lib.ffi.symbols import CDEF

    for symbol in (
        "set_protocol",
        "iperf_get_test_protocol_id",
        "iperf_set_test_bind_address",
        "iperf_get_test_bind_address",
        "iperf_set_test_json_callback",
        "iperf_get_iperf_version",
    ):
        assert symbol in CDEF
