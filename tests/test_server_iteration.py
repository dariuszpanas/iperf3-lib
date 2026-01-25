"""Unit tests for Server._run_iteration logic."""

from types import SimpleNamespace

from iperf3_lib.iperf_server import Server


def test_run_iteration(monkeypatch):
    """Test that _run_iteration calls run_server and reset_test on the lib."""
    # Create fake lib/ffi and a fake test object
    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    fake_lib = SimpleNamespace()
    calls = {}

    def iperf_run_server(t):
        calls["ran"] = True
        return 0

    def iperf_reset_test(t):
        calls["reset"] = True

    fake_lib.iperf_run_server = iperf_run_server
    fake_lib.iperf_reset_test = iperf_reset_test

    # patch module-level lib/ffi
    import iperf3_lib.iperf_server as server_mod

    monkeypatch.setattr(server_mod, "ffi", fake_ffi)
    monkeypatch.setattr(server_mod, "lib", fake_lib)

    # run iteration against a dummy test object
    s = Server()
    s._run_iteration(object())

    assert calls.get("ran") is True
    assert calls.get("reset") is True
