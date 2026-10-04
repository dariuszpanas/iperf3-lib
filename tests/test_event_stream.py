"""Owned live consumption, bounded wakeups, cancellation and facade snapshots."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from iperf3_lib import _event_stream as module
from iperf3_lib._cancellation import _ExecutionControl
from iperf3_lib._event_stream import EventStream
from iperf3_lib._live_event_types import make_delivery_gap_event
from iperf3_lib.exceptions import IperfCleanupError
from iperf3_lib.result import Result


def sample():
    """Build a small typed observation without loading native code."""
    return make_delivery_gap_event(stage="synthetic", dropped=1, delivery_sequence=1)


async def started(signal):
    """Wait for an executor milestone with an explicit failure deadline."""
    async with asyncio.timeout(3):
        while not signal.is_set():
            await asyncio.sleep(0.001)


def blocking_operation(entered, cleaned, *, cleanup_error=None, release=None):
    """Make a cancellable stand-in that acknowledges cleanup before returning."""

    def operation(control, sink):
        entered.set()
        try:
            while True:
                control.check()
                time.sleep(0.001)
        finally:
            if release is not None:
                assert release.wait(3)
            cleaned.set()
            if cleanup_error is not None:
                raise cleanup_error

    return operation


@pytest.mark.asyncio
async def test_inert_construction_and_required_context():
    """No operation or admission occurs until context entry."""
    calls = []
    stream = EventStream(lambda: calls.append(True))
    assert calls == []
    with pytest.raises(RuntimeError, match="async with"):
        await stream.result()
    with pytest.raises(RuntimeError, match="async with"):
        await anext(stream)
    await stream.aclose()
    await stream.aclose()
    with pytest.raises(RuntimeError, match="more than once"):
        await stream.__aenter__()
    assert calls == []


@pytest.mark.asyncio
async def test_terminal_follows_finished_owner_and_complete_result():
    """A native observation cannot publish completion before executor cleanup."""
    cleaned = threading.Event()
    result = Result(ok=True, raw={"preserved": {"value": 7}})

    def operation(control, sink):
        sink(sample())
        cleaned.set()
        return result

    async with EventStream(lambda: operation) as stream:
        events = [event async for event in stream]
        assert [event.kind for event in events] == ["delivery_gap", "terminal"]
        assert [event.delivery_sequence for event in events] == [1, 2]
        assert cleaned.is_set() and stream._owner.done()
        assert await stream.result() is result
    assert await stream.result() is result
    await stream.aclose()
    with pytest.raises(RuntimeError, match="more than once"):
        await stream.__aenter__()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["break", "close", "body_error"])
async def test_early_exit_waits_for_cleanup(stop):
    """Break, explicit close and application failure all finish owned cleanup."""
    entered, cleaned = threading.Event(), threading.Event()
    blocking = blocking_operation(entered, cleaned)

    def operation(control, sink):
        sink(sample())
        return blocking(control, sink)

    async def run():
        async with EventStream(lambda: operation) as stream:
            async for _event in stream:
                if stop == "body_error":
                    raise ValueError("body failed")
                if stop == "close":
                    await stream.aclose()
                break
        assert cleaned.is_set()

    if stop == "body_error":
        with pytest.raises(ValueError, match="body failed"):
            await run()
    else:
        await run()
    assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter", ["next", "result", "close"])
async def test_repeated_waiter_cancellation_cannot_abandon_cleanup(waiter):
    """Repeated task cancellation stays pending until executor cleanup finishes."""
    entered, cleaned, release = (threading.Event() for _ in range(3))
    stream = EventStream(lambda: blocking_operation(entered, cleaned, release=release))
    await stream.__aenter__()
    await started(entered)

    async def wait():
        if waiter == "next":
            await anext(stream)
        elif waiter == "result":
            await stream.result()
        else:
            await stream.aclose()

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    task.cancel("first cancellation")
    await asyncio.sleep(0.01)
    task.cancel("second cancellation")
    await asyncio.sleep(0.01)
    assert not task.done() and not cleaned.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and stream._owner.done()
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [False, True])
async def test_cleanup_error_precedes_cancellation_and_body_failure(body):
    """A failed reaping attempt is never replaced by cancellation or body errors."""
    entered, cleaned = threading.Event(), threading.Event()
    error = IperfCleanupError("retained owner", control=_ExecutionControl())
    with pytest.raises(IperfCleanupError) as caught:
        async with EventStream(
            lambda: blocking_operation(entered, cleaned, cleanup_error=error)
        ) as stream:
            await started(entered)
            if body:
                raise ValueError("application")
            await stream.aclose()
    assert caught.value is error and cleaned.is_set()
    if body:
        assert isinstance(error.__cause__, (ValueError, asyncio.CancelledError))


@pytest.mark.asyncio
@pytest.mark.parametrize("observe", [False, True])
async def test_operation_failure_terminal_and_unobserved_exit_error(observe):
    """Iteration preserves terminal evidence and context exit cannot hide failure."""
    failure = ValueError("worker setup rejected")

    def operation(control, sink):
        raise failure

    async def run():
        async with EventStream(lambda: operation) as stream:
            events = [event async for event in stream]
            assert [event.kind for event in events] == ["terminal"]
            if observe:
                with pytest.raises(ValueError) as caught:
                    await stream.result()
                assert caught.value is failure

    if observe:
        await run()
    else:
        with pytest.raises(ValueError) as caught:
            await run()
        assert caught.value is failure


@pytest.mark.asyncio
async def test_body_exception_precedes_ordinary_owned_error():
    """An application failure remains primary after an ordinary worker failure."""

    def operation(control, sink):
        raise RuntimeError("operation")

    with pytest.raises(ValueError, match="application"):
        async with EventStream(lambda: operation) as stream:
            await anext(stream)
            raise ValueError("application")


@pytest.mark.asyncio
async def test_second_consumer_is_rejected_without_cancelling_owner():
    """Only one task can read the event iterator."""
    entered, cleaned = threading.Event(), threading.Event()
    async with EventStream(lambda: blocking_operation(entered, cleaned)) as stream:
        await started(entered)
        first = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="one event consumer"):
            await anext(stream)
        assert not stream._owner.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["items", "bytes"])
async def test_slow_consumer_bounds_coalesces_wakeups_and_reserves_terminal(monkeypatch, bound):
    """Full buffers drop advisory observations without losing terminal or result."""
    monkeypatch.setattr(module, "MAX_STREAM_EVENTS", 3 if bound == "items" else 100)
    monkeypatch.setattr(module, "MAX_STREAM_BYTES", 100 if bound == "items" else 6)
    monkeypatch.setattr(module, "live_event_size", lambda event: 2)
    value = Result(ok=True)

    def operation(control, sink):
        for _ in range(1000):
            sink(sample())
        return value

    async with EventStream(lambda: operation) as stream:
        assert await stream.result() is value
        assert len(stream._queue) == 3 and stream._bytes == 6
        assert stream._dropped == 997
        events = [event async for event in stream]
        assert len(events) == 5 and events[-1].kind == "terminal"
        assert events[-2].payload.dropped == 997
        assert [event.delivery_sequence for event in events] == list(range(1, 6))
        assert stream._bytes == 0


@pytest.mark.asyncio
async def test_pending_thread_notifications_are_coalesced(monkeypatch):
    """A stopped consumer loop retains one callback regardless of producer volume."""
    entered, cleaned = threading.Event(), threading.Event()
    async with EventStream(lambda: blocking_operation(entered, cleaned)) as stream:
        await started(entered)
        callbacks = []
        real_loop = stream._loop
        stream._loop = SimpleNamespace(call_soon_threadsafe=callbacks.append)
        for _ in range(1000):
            stream._put(sample())
        assert len(callbacks) == 1
        callbacks.pop()()
        stream._put(sample())
        assert len(callbacks) == 1
        callbacks.pop()()
        stream._loop = real_loop


@pytest.mark.asyncio
async def test_oversized_projection_is_a_gap_and_preserves_result(monkeypatch):
    """A large advisory payload cannot turn a valid complete result into failure."""
    value = Result(ok=True)

    def oversized(event):
        raise module.IPCError("frame is too large")

    monkeypatch.setattr(module, "live_event_size", oversized)

    def operation(control, sink):
        sink(sample())
        return value

    async with EventStream(lambda: operation) as stream:
        events = [event async for event in stream]
        assert [event.kind for event in events] == ["delivery_gap", "terminal"]
        assert events[0].payload.stage == "consumer_bridge"
        assert events[0].payload.dropped == 1
        assert events[1].payload.consumer_dropped == 1
        assert await stream.result() is value


@pytest.mark.asyncio
async def test_client_snapshot_is_at_context_entry_and_legacy_calls_stay_inert(monkeypatch):
    """Mutation after admission cannot change the config passed to the worker."""
    from iperf3_lib import _execution
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    calls = []

    def worker(role, options, **kwargs):
        calls.append((role, options, kwargs))
        return Result(ok=True)

    monkeypatch.setattr(_execution, "run_worker", worker)
    client = Client(ClientConfig("127.0.0.1", duration=1))
    context = client.events()
    client.cfg.port = 5202
    assert calls == []
    async with context as stream:
        client.cfg.port = 5203
        await stream.result()
    assert calls[0][1]["port"] == 5202
    assert calls[0][1]["json_stream"] is True
    assert callable(calls[0][2]["_on_live_event"])


@pytest.mark.asyncio
async def test_server_snapshot_lock_and_reuse(monkeypatch):
    """Server stream snapshots options and holds its admission through cleanup."""
    from iperf3_lib import iperf_server

    entered, cleaned = threading.Event(), threading.Event()
    calls = []
    blocking = blocking_operation(entered, cleaned)

    def worker(options, **kwargs):
        calls.append(options)
        if len(calls) == 1:
            return blocking(kwargs["_control"], kwargs["_on_live_event"])
        return Result(ok=True)

    monkeypatch.setattr(iperf_server, "_run_worker", worker)
    server = iperf_server.Server(5201)
    context = server.events_once()
    server.port = 5202
    async with context:
        server.port = 5203
        await started(entered)
        with pytest.raises(RuntimeError, match="active operation"):
            server.run_once()
    assert cleaned.is_set() and not server._run_lock.locked()
    assert calls[0]["port"] == 5202 and calls[0]["json_stream"] is True
    async with server.events_once() as stream:
        assert (await stream.result()).ok
    assert calls[1]["port"] == 5203


@pytest.mark.asyncio
async def test_server_cleanup_failure_retains_admission_until_reaped(monkeypatch):
    """A stream cannot release server admission merely because cleanup raised."""
    from iperf3_lib import iperf_server

    callbacks = []
    retained = SimpleNamespace(cleanup_confirmed=False, when_reaped=callbacks.append)
    failure = IperfCleanupError("not reaped", control=retained)

    def worker(options, **kwargs):
        raise failure

    monkeypatch.setattr(iperf_server, "_run_worker", worker)
    server = iperf_server.Server()
    with pytest.raises(IperfCleanupError):
        async with server.events_once() as stream:
            events = [event async for event in stream]
            assert events[-1].payload.cleanup_confirmed is False
    assert server._run_lock.locked()
    with pytest.raises(RuntimeError, match="active operation"):
        server.run_once()
    assert len(callbacks) == 1
    callbacks.pop()()
    assert not server._run_lock.locked()
