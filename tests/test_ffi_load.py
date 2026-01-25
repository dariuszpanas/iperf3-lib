import os

import pytest

from py_iperf3.ffi.api import lib


def test_lib_loaded():
    # If lib couldn't be loaded, import would have raised already.
    # Check existence of a core symbol.
    assert hasattr(lib, "iperf_new_test")


@pytest.mark.integration
def test_env_override_load(monkeypatch):
    # We just ensure that env var doesn't break loading if unset/missing.
    monkeypatch.setenv("PY_IPERF3_LIB", os.getenv("PY_IPERF3_LIB", ""))
    assert hasattr(lib, "iperf_defaults")
