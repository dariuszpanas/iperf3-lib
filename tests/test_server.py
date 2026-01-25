from types import SimpleNamespace

import pytest

from py_iperf3.exceptions import IperfLibraryError


def test_server_new_test_fails(monkeypatch):
    from py_iperf3 import libiperf_server as server_mod

    fake_lib = SimpleNamespace()
    fake_ffi = SimpleNamespace()
    fake_ffi.NULL = 0

    def iperf_new_test():
        return fake_ffi.NULL

    fake_lib.iperf_new_test = iperf_new_test

    monkeypatch.setattr(server_mod, "lib", fake_lib)
    monkeypatch.setattr(server_mod, "ffi", fake_ffi)

    from py_iperf3.libiperf_server import Server

    s = Server()
    with pytest.raises(IperfLibraryError):
        s.run_once()
