"""Tests for server worker isolation and result/error transport."""

import asyncio
import subprocess
import sys
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from iperf3_lib.exceptions import IperfCleanupError, IperfLibraryError
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
    import iperf3_lib.iperf_server as module

    server = Server()

    def handler(event):
        pass

    result = Result(ok=True)

    def worker(options, **kwargs):
        assert options == asdict(ServerConfig(json_stream=True))
        assert kwargs["on_event"] is handler
        assert kwargs["timeout"] == 3
        assert kwargs["max_runs"] == 1
        assert kwargs["_control"] is not None
        assert server._run_lock.locked()
        return result

    monkeypatch.setattr(module, "_run_worker", worker)
    assert await server.aserve_once(on_event=handler, timeout=3) is result
    assert not server._run_lock.locked()


@pytest.mark.asyncio
async def test_aserve_once_uses_owned_worker_without_invoking_run_once_override(monkeypatch):
    """Synchronous overrides cannot bypass async server admission and ownership."""
    import iperf3_lib.iperf_server as module

    class CustomServer(Server):
        """Supply an application override that async execution must not invoke."""

        def run_once(self, **kwargs):
            """Reject accidental delegation from the asynchronous method."""
            pytest.fail("aserve_once invoked the synchronous run_once override")

    server = CustomServer()
    native_failure = Result(ok=False, error="retained native failure")

    def worker(options, **kwargs):
        assert kwargs["_control"] is not None
        assert server._run_lock.locked()
        return native_failure

    monkeypatch.setattr(module, "_run_worker", worker)
    assert await server.aserve_once() is native_failure
    assert not server._run_lock.locked()
    assert not native_failure.ok and native_failure.error == "retained native failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_cause", ["cancel", "timeout"])
async def test_failed_cancellation_cleanup_holds_server_admission_until_reaped(
    monkeypatch, first_cause
):
    """A failed kill is explicit and cannot admit a second live server worker."""
    import iperf3_lib.iperf_server as module

    ready = asyncio.Event()
    exited = threading.Event()
    release = threading.Event()
    reaped = asyncio.Event()
    controls = []
    loop = asyncio.get_running_loop()
    server = Server()

    class UnreapableProcess:
        """Report failed OS cleanup until the test supplies eventual process exit."""

        def poll(self):
            return 0 if exited.is_set() else None

        def terminate(self):
            raise OSError("termination unavailable")

        def kill(self):
            raise OSError("kill unavailable")

        def wait(self, timeout):
            if exited.wait(min(timeout, 0.01)):
                return 0
            raise subprocess.TimeoutExpired("retained server", timeout)

    def worker(options, **kwargs):
        control = kwargs["_control"]
        controls.append(control)
        control.register(UnreapableProcess())
        if first_cause == "timeout":
            control.abort(TimeoutError("initial deadline"))
        loop.call_soon_threadsafe(ready.set)
        try:
            assert release.wait(5)
            control.wait_for_exit(5)
        finally:
            control.close()

    monkeypatch.setattr(module, "_run_worker", worker)
    task = asyncio.create_task(server.aserve_once())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        task.cancel("cancel retained server")
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(IperfCleanupError) as captured:
            await asyncio.wait_for(task, timeout=2)
        assert isinstance(
            captured.value.__cause__,
            TimeoutError if first_cause == "timeout" else asyncio.CancelledError,
        )
        assert not captured.value._control.cleanup_confirmed
        assert server._run_lock.locked()
        with pytest.raises(RuntimeError, match="already has an active operation"):
            await server.aserve_once()
    finally:
        release.set()
        try:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, IperfCleanupError):
                pass
        finally:
            # The wrapper registers its lock-release callback before the task
            # returns. Observe reaping after that callback, including on failure.
            if controls:
                controls[0].when_reaped(lambda: loop.call_soon_threadsafe(reaped.set))
            exited.set()
            if controls:
                await asyncio.wait_for(reaped.wait(), timeout=2)
    assert not server._run_lock.locked()
    result = Result(ok=True)
    monkeypatch.setattr(module, "_run_worker", lambda *args, **kwargs: result)
    assert await server.aserve_once() is result
