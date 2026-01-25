"""Unit test for Server.run_once error handling when new_test fails."""

from types import SimpleNamespace

import pytest

from iperf3_lib.exceptions import IperfLibraryError


def test_server_new_test_fails(monkeypatch):
    """Test that run_once raises IperfLibraryError if iperf_new_test returns NULL."""
    from iperf3_lib import iperf_server as server_mod

    fake_lib = SimpleNamespace()
    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    def iperf_new_test():
        return fake_ffi.NULL

    fake_lib.iperf_new_test = iperf_new_test

    monkeypatch.setattr(server_mod, "lib", fake_lib)
    monkeypatch.setattr(server_mod, "ffi", fake_ffi)

    from iperf3_lib.iperf_server import Server

    s = Server()
    with pytest.raises(IperfLibraryError):
        s.run_once()
