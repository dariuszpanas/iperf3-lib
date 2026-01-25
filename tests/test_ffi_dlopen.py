"""Tests for FFI dynamic loading error handling in iperf3_lib."""

import pytest


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
