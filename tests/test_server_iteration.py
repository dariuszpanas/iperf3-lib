"""Server operation nonreentry and cooperative stop regressions."""

import pytest

from iperf3_lib.iperf_server import Server
from iperf3_lib.result import Result


@pytest.mark.parametrize("outer", ["run_once", "serve_forever"])
@pytest.mark.parametrize("inner", ["run_once", "serve_forever"])
def test_same_server_rejects_reentrant_operations(monkeypatch, outer, inner):
    """An event or result callback cannot start a second operation on its server."""
    import iperf3_lib.iperf_server as module

    server = Server()

    def worker(*args, **kwargs):
        with pytest.raises(RuntimeError, match="already has an active"):
            getattr(server, inner)()
        return Result(ok=True)

    monkeypatch.setattr(module, "_run_worker", worker)
    getattr(server, outer)()
    assert not server._run_lock.locked()


def test_prestopped_loop_does_not_start_worker(monkeypatch):
    """A pre-existing cooperative stop request prevents listener allocation."""
    import iperf3_lib.iperf_server as module

    monkeypatch.setattr(module, "_run_worker", lambda *a, **kw: pytest.fail("worker admitted"))
    server = Server()
    server.stop()
    assert server.serve_forever() is None


def test_stop_is_cooperative_between_results(monkeypatch):
    """Relay the stop event to the persistent worker without starting another run."""
    import iperf3_lib.iperf_server as module

    server = Server()
    results = [Result(ok=True), Result(ok=False, error="failure")]
    observed = []

    def callback(result):
        observed.append(result)
        server.stop()

    def worker(options, **kwargs):
        for result in results:
            if kwargs["should_stop"]():
                break
            kwargs["on_result"](result)
        return observed[-1]

    monkeypatch.setattr(module, "_run_worker", worker)
    server.serve_forever(on_result=callback)
    assert observed == results[:1]


def test_run_once_remains_available_after_stopping_loop(monkeypatch):
    """Preserve the legacy distinction between one-shot serving and loop stop."""
    import iperf3_lib.iperf_server as module

    result = Result(ok=True)
    monkeypatch.setattr(module, "_run_worker", lambda *a, **kw: result)
    server = Server()
    server.stop()
    assert server.run_once() is result
