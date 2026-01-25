"""Tests for the Server.serve_forever method with limited iterations."""

from types import SimpleNamespace

import pytest

from iperf3_lib.iperf_server import Server


def test_serve_forever_limited(monkeypatch):
    """Test serve_forever with a fake lib, stopping after a few iterations."""
    # Patch module-level lib so _run_iteration can be observed and we can stop after N iterations
    import iperf3_lib.iperf_server as server_mod

    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    fake_lib = SimpleNamespace()
    calls = {"count": 0}

    def fake_run_server(t):
        calls["count"] += 1
        return 0

    def fake_reset_test(t):
        calls["reset_" + str(calls["count"])] = True

    # Minimal methods needed by serve_forever setup
    def fake_new_test():
        return object()

    def fake_defaults(t):
        return 0

    def fake_set_role(t, c):
        return None

    def fake_set_port(t, p):
        return None

    def fake_set_hostname(t, h):
        return None

    def fake_free_test(t):
        return None

    fake_lib.iperf_run_server = fake_run_server
    fake_lib.iperf_reset_test = fake_reset_test
    fake_lib.iperf_new_test = fake_new_test
    fake_lib.iperf_defaults = fake_defaults
    fake_lib.iperf_set_test_role = fake_set_role
    fake_lib.iperf_set_test_server_port = fake_set_port
    fake_lib.iperf_set_test_server_hostname = fake_set_hostname
    fake_lib.iperf_free_test = fake_free_test

    monkeypatch.setattr(server_mod, "ffi", fake_ffi)
    monkeypatch.setattr(server_mod, "lib", fake_lib)

    s = Server()

    # Replace _run_iteration with one that raises a sentinel after 3 iterations to stop the loop
    original = s._run_iteration

    def limited_run_iteration(t):
        if calls["count"] >= 3:
            raise RuntimeError("stop")
        return original(t)

    monkeypatch.setattr(s, "_run_iteration", limited_run_iteration)

    # Now call serve_forever and expect RuntimeError to bubble up
    with pytest.raises(RuntimeError):
        s.serve_forever()

    assert calls["count"] >= 1
