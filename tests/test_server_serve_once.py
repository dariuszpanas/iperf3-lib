"""Sequential server callback and operation-bound validation."""

import pytest

from iperf3_lib.iperf_server import Server
from iperf3_lib.result import Result


def test_loop_uses_one_worker_and_passes_each_attempt(monkeypatch):
    """Keep one worker for a bounded sequence and preserve failed result evidence."""
    import iperf3_lib.iperf_server as module

    server = Server(bind_host="127.0.0.1")
    results = [Result(ok=True), Result(ok=False, error="native error")]
    observed = []
    events = []
    calls = []

    def worker(options, **kwargs):
        calls.append((options, kwargs))
        assert kwargs["max_runs"] == 2
        assert kwargs["timeout"] == 10
        assert not kwargs["should_stop"]()
        kwargs["on_event"]("event")
        for result in results:
            kwargs["on_result"](result)
        return results[-1]

    monkeypatch.setattr(module, "_run_worker", worker)
    assert (
        server.serve_forever(
            on_result=observed.append, on_event=events.append, max_runs=2, timeout=10
        )
        is None
    )
    assert len(calls) == 1
    assert calls[0][0]["bind_address"] == "127.0.0.1"
    assert observed == results
    assert events == ["event"]


def test_callback_error_propagates_and_releases_instance_lock(monkeypatch):
    """A callback exception remains visible and cannot wedge subsequent calls."""
    import iperf3_lib.iperf_server as module

    def callback(result):
        raise LookupError("callback failed")

    def worker(options, **kwargs):
        kwargs["on_result"](Result(ok=True))

    monkeypatch.setattr(module, "_run_worker", worker)
    server = Server()
    with pytest.raises(LookupError, match="callback failed"):
        server.serve_forever(on_result=callback)
    assert not server._run_lock.locked()


@pytest.mark.parametrize("value", [True, "2", 2.0, 0, -1])
def test_loop_rejects_invalid_max_runs(value):
    """Require an explicit positive integer when a run count is bounded."""
    with pytest.raises((ValueError, TypeError), match="max_runs"):
        Server().serve_forever(max_runs=value)


def test_loop_rejects_invalid_result_handler():
    """Require the per-result callback to be callable."""
    with pytest.raises(TypeError, match="on_result"):
        Server().serve_forever(on_result=42)
