"""Tests for server worker isolation and result/error transport."""

import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from iperf3_lib.exceptions import IperfLibraryError
from iperf3_lib.iperf_server import Server
from iperf3_lib.result import Result
from iperf3_lib.server_config import ServerConfig


def test_run_once_passes_all_options_and_returns_worker_result(monkeypatch):
    """Admit one detached snapshot and preserve worker result identity."""
    import iperf3_lib.iperf_server as module

    config = ServerConfig(bind_address="127.0.0.1", bind_device="lo", json_stream=True)
    server = Server(config=config)
    expected = asdict(config)

    def event_handler(event):
        pass

    result = Result(ok=True, reporting_role="server")
    calls = []

    def worker(options, **kwargs):
        calls.append((options, kwargs))
        server.config.port = 9999
        assert options == expected
        return result

    monkeypatch.setattr(module, "_run_worker", worker)
    assert server.run_once(on_event=event_handler, timeout=5) is result
    assert calls == [(expected, {"on_event": event_handler, "max_runs": 1, "timeout": 5})]


def test_lazy_worker_forwarding_supplies_server_role(monkeypatch):
    """Exercise the wrapper seam without loading the native execution module."""
    import iperf3_lib.iperf_server as module

    calls = []
    result = Result(ok=False, error="native failure")

    def worker(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setitem(sys.modules, "iperf3_lib._execution", SimpleNamespace(run_worker=worker))
    assert module._run_worker({"port": 5201}, timeout=2) is result
    assert calls == [(("server", {"port": 5201}), {"timeout": 2})]


@pytest.mark.parametrize("error", [IperfLibraryError("allocation"), TimeoutError("deadline")])
def test_worker_errors_propagate_and_release_instance_lock(monkeypatch, error):
    """Setup and bounded-execution errors leave the instance reusable."""
    import iperf3_lib.iperf_server as module

    def worker(*args, **kwargs):
        raise error

    monkeypatch.setattr(module, "_run_worker", worker)
    server = Server()
    with pytest.raises(type(error), match=str(error)):
        server.run_once()
    result = Result(ok=False, error="retained native error")
    monkeypatch.setattr(module, "_run_worker", lambda *a, **kw: result)
    assert server.run_once() is result


@pytest.mark.parametrize("method", ["run_once", "serve_forever"])
@pytest.mark.parametrize("value", [False, "2", 0, -1, float("nan"), float("inf")])
def test_server_rejects_invalid_timeout_before_worker(monkeypatch, method, value):
    """Require a positive finite deadline without permissive bool coercion."""
    import iperf3_lib.iperf_server as module

    monkeypatch.setattr(module, "_run_worker", lambda *a, **kw: pytest.fail("worker admitted"))
    with pytest.raises((ValueError, TypeError), match="timeout"):
        getattr(Server(), method)(timeout=value)


@pytest.mark.parametrize("method", ["run_once", "serve_forever"])
def test_server_rejects_non_callable_event_handler(method):
    """Reject callback typos before native configuration or allocation."""
    with pytest.raises(TypeError, match="on_event"):
        getattr(Server(), method)(on_event=1)


@pytest.mark.asyncio
async def test_aserve_once_returns_result_and_forwards_options(monkeypatch):
    """Retain the real result through the asynchronous convenience method."""
    server = Server()

    def handler(event):
        pass

    result = Result(ok=True)

    def run_once(**kwargs):
        assert kwargs == {"on_event": handler, "timeout": 3}
        return result

    monkeypatch.setattr(server, "run_once", run_once)
    assert await server.aserve_once(on_event=handler, timeout=3) is result
