import pytest


def test_dlopen_env_override(monkeypatch):
    import py_iperf3.ffi.api as api

    # Ensure env var branch is used
    monkeypatch.setenv("PY_IPERF3_LIB", "/nonexistent/path/libiperf.so")

    # Make ffi.dlopen raise to simulate failure
    def raising_dlopen(path):
        raise OSError("dlopen failed")

    monkeypatch.setattr(api.ffi, "dlopen", raising_dlopen)

    with pytest.raises(OSError):
        api._dlopen()


def test_dlopen_all_names_fail(monkeypatch):
    import py_iperf3.ffi.api as api

    # Ensure env var not set
    monkeypatch.delenv("PY_IPERF3_LIB", raising=False)

    # Make ffi.dlopen always raise
    def raising_dlopen(name):
        raise OSError("dlopen failed")

    monkeypatch.setattr(api.ffi, "dlopen", raising_dlopen)

    with pytest.raises(OSError):
        api._dlopen()
