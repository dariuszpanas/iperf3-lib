"""Additional unit tests for Server and error handling."""

from types import SimpleNamespace

import pytest

from iperf3_lib.exceptions import IperfError
from iperf3_lib.iperf_server import Server


def test_run_iteration_check_raises(monkeypatch):
    """Test that _run_iteration raises IperfError on negative return from run_server."""
    # Patch lib so iperf_run_server returns a negative value -> _check raises
    import iperf3_lib.iperf_server as server_mod

    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    # mimic cffi new
    def ffi_new(spec, val):
        return val

    fake_ffi.new = ffi_new
    # mimic cffi.string used in _check
    fake_ffi.string = lambda s: s

    fake_lib = SimpleNamespace()
    fake_lib.i_errno = 0

    def iperf_strerror(errno):
        return b"err"

    fake_lib.iperf_strerror = iperf_strerror

    def iperf_run_server(t):
        return -1

    def iperf_reset_test(t):
        return None

    fake_lib.iperf_run_server = iperf_run_server
    fake_lib.iperf_reset_test = iperf_reset_test

    monkeypatch.setattr(server_mod, "ffi", fake_ffi)
    monkeypatch.setattr(server_mod, "lib", fake_lib)

    s = Server()
    with pytest.raises(IperfError):
        s._run_iteration(object())


def test_run_once_with_bind_host(monkeypatch):
    """Test that run_once calls set_test_server_hostname when bind_host is set."""
    # Ensure iperf_set_test_server_hostname is called when bind_host is set
    import iperf3_lib.iperf_server as server_mod

    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    # mimic cffi new
    def ffi_new(spec, val):
        return val

    fake_ffi.new = ffi_new
    # mimic cffi.string
    fake_ffi.string = lambda s: s

    fake_lib = SimpleNamespace()
    called = {}
    fake_lib.i_errno = 0

    def iperf_strerror(errno):
        return b"err"

    fake_lib.iperf_strerror = iperf_strerror

    def iperf_new_test():
        return object()

    def iperf_defaults(t):
        return 0

    def iperf_set_test_role(t, c):
        return None

    def iperf_set_test_server_port(t, p):
        return None

    def iperf_set_test_server_hostname(t, h):
        called["hostname"] = h

    def iperf_run_server(t):
        return 0

    def iperf_reset_test(t):
        return None

    def iperf_free_test(t):
        return None

    fake_lib.iperf_new_test = iperf_new_test
    fake_lib.iperf_defaults = iperf_defaults
    fake_lib.iperf_set_test_role = iperf_set_test_role
    fake_lib.iperf_set_test_server_port = iperf_set_test_server_port
    fake_lib.iperf_set_test_server_hostname = iperf_set_test_server_hostname
    fake_lib.iperf_run_server = iperf_run_server
    fake_lib.iperf_reset_test = iperf_reset_test
    fake_lib.iperf_free_test = iperf_free_test

    monkeypatch.setattr(server_mod, "ffi", fake_ffi)
    monkeypatch.setattr(server_mod, "lib", fake_lib)

    s = Server(bind_host="0.0.0.0")
    s.run_once()
    # hostname stored as bytes from ffi.new("char[]", b"...") -> decode for comparison
    assert called.get("hostname").decode() == "0.0.0.0"
