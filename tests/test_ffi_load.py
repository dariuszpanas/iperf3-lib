"""Tests for loading the libiperf shared library via FFI."""

import os

import pytest

from iperf3_lib.ffi.api import lib


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
