import importlib
from types import SimpleNamespace


def test_has_symbol_monkeypatched(monkeypatch):
    # Create a fake lib object with different attributes
    fake_lib = SimpleNamespace()
    fake_lib.iperf_set_test_bidirectional = lambda *a, **k: None
    # monkeypatch the ffi.api.lib object before importing capabilities
    import py_iperf3.ffi.api as api

    monkeypatch.setattr(api, "lib", fake_lib)

    # reload capabilities so it picks up the patched lib
    import py_iperf3.capabilities as caps

    importlib.reload(caps)

    assert caps.HAS_BIDIR is True
    # ensure a non-existent symbol is false
    assert caps.has_symbol("non_existing_symbol") is False
