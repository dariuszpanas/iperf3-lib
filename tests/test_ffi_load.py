"""Tests for loading the libiperf shared library via FFI."""

import os

import pytest

from iperf3_lib.ffi.api import ffi, lib


@pytest.mark.integration
def test_lib_loaded():
    """Test that the lib object exposes the iperf_new_test symbol."""
    # If lib couldn't be loaded, import would have raised already.
    # Check existence of a core symbol.
    assert hasattr(lib, "iperf_new_test")


@pytest.mark.integration
def test_env_override_load(monkeypatch):
    """Test that setting IPERF3_LIB env var does not break loading."""
    # We just ensure that env var doesn't break loading if unset/missing.
    monkeypatch.setenv("IPERF3_LIB", os.getenv("IPERF3_LIB", ""))
    assert hasattr(lib, "iperf_defaults")


@pytest.mark.integration
def test_bind_address_api_round_trip():
    """Verify the declared bind-address ABI against the loaded native library."""
    test = lib.iperf_new_test()
    assert test != ffi.NULL
    try:
        assert lib.iperf_defaults(test) == 0
        address = ffi.new("char[]", b"127.0.0.1")
        lib.iperf_set_test_bind_address(test, address)

        configured = lib.iperf_get_test_bind_address(test)
        assert configured != ffi.NULL
        assert ffi.string(configured) == b"127.0.0.1"
    finally:
        lib.iperf_free_test(test)
