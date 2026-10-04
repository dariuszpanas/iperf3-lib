"""Owned asynchronous consumption of bounded, detached live observations."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from types import TracebackType

from ._cancellation import _ExecutionControl, _run_async
from ._ipc import IPCError
from ._live_event_types import (
    clone_live_event,
    event_identity,
    live_event_size,
    make_delivery_gap_event,
    make_terminal_event,
)
from .events import LiveEvent
from .exceptions import IperfCleanupError
from .result import Result

MAX_STREAM_EVENTS = 128
MAX_STREAM_BYTES = 2 * 1024 * 1024

_Sink = Callable[[LiveEvent], None]
_Operation = Callable[[_ExecutionControl, _Sink], Result]


class EventStream:
    """Own one isolated operation inside an asynchronous context.

    Construction does no work. Enter the context before reading events or the
    complete result. One task may consume events; a second consumer is rejected.
    Exiting the context or closing the stream stops unfinished work and waits for
    cleanup. Iteration yields a reserved terminal after the operation settles.
    ``result()`` raises operation failures; context exit raises failures that
    have not been observed there. Cleanup failures take precedence over every
    other exception, including application exceptions and cancellation.
    After closure, already buffered events and the reserved terminal remain
    readable; no native resources remain owned after confirmed cleanup.
    """

    def __init__(self, admit: Callable[[], _Operation]) -> None:
        """Configure an inert operation factory; callers normally use Client/Server."""
        self._admit = admit
        self._entered = False
        self._closed = False
        self._intentional_close = False
        self._observed_error = False
        self._owner: asyncio.Task[Result] | None = None
        self._consumer: asyncio.Task | None = None
        self._reading = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._available: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._queue: deque[tuple[LiveEvent, int]] = deque()
        self._bytes = 0
        self._wake_pending = False
        self._dropped = 0
        self._reported_dropped = 0
        self._sequence = 0
        self._identity: LiveEvent | None = None
        self._value: Result | None = None
        self._error: BaseException | None = None
        self._terminal: LiveEvent | None = None
        self._terminal_delivered = False

    async def __aenter__(self) -> EventStream:
        """Snapshot configuration and start the single cleanup owner."""
        if self._entered or self._closed:
            raise RuntimeError("an EventStream cannot be entered more than once")
        self._entered = True
        self._loop = asyncio.get_running_loop()
        self._available = asyncio.Event()
        operation = self._admit()
        self._owner = asyncio.create_task(_run_async(lambda control: operation(control, self._put)))
        self._owner.add_done_callback(self._settled)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Finish cleanup and apply cleanup, body, then operation-error precedence."""
        try:
            await self.aclose()
        except IperfCleanupError as failure:
            if exc is not None and failure.__cause__ is None:
                raise failure from exc
            raise
        if exc is None and self._error is not None and not self._observed_error:
            if not (self._intentional_close and isinstance(self._error, asyncio.CancelledError)):
                self._observed_error = True
                raise self._error

    def __aiter__(self) -> EventStream:
        """Return the sole iterator; admission and consumer checks apply on reads."""
        self._require_entered()
        return self

    async def __anext__(self) -> LiveEvent:
        """Read bounded observations, a loss notice if needed, then one terminal."""
        self._require_entered()
        current = asyncio.current_task()
        if self._consumer is None:
            self._consumer = current
        if self._consumer is not current or self._reading:
            raise RuntimeError("an EventStream supports only one event consumer")
        self._reading = True
        try:
            while True:
                assert self._available is not None
                with self._lock:
                    if self._queue:
                        event, size = self._queue.popleft()
                        self._bytes -= size
                    elif self._reported_dropped != self._dropped:
                        lost = self._dropped - self._reported_dropped
                        self._reported_dropped = self._dropped
                        event = make_delivery_gap_event(
                            stage="consumer_bridge",
                            dropped=lost,
                            delivery_sequence=0,
                            identity=self._identity,
                        )
                    elif self._terminal is not None:
                        if self._terminal_delivered:
                            raise StopAsyncIteration
                        self._terminal_delivered = True
                        event = self._terminal
                    else:
                        self._available.clear()
                        event = None
                if event is not None:
                    self._sequence += 1
                    return clone_live_event(event, delivery_sequence=self._sequence)
                await self._available.wait()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        finally:
            self._reading = False

    async def result(self) -> Result:
        """Await the complete result without consuming or reconstructing events."""
        self._require_entered()
        assert self._owner is not None
        try:
            await asyncio.wait((self._owner,))
        except asyncio.CancelledError:
            await self.aclose()
            raise
        # The callback is normally already dispatched. Calling it here also
        # handles a just-completed task without depending on loop callback order.
        self._settled(self._owner)
        if self._error is not None:
            self._observed_error = True
            raise self._error
        assert self._value is not None
        return self._value

    async def aclose(self) -> None:
        """Stop unfinished work and wait through repeated cancellation for cleanup."""
        self._closed = True
        owner = self._owner
        if owner is None:
            return
        if not owner.done():
            self._intentional_close = True
            owner.cancel()
        cancelled: asyncio.CancelledError | None = None
        while not owner.done():
            try:
                await asyncio.wait((owner,))
            except asyncio.CancelledError as exc:
                cancelled = exc
                owner.cancel()
        self._settled(owner)
        if isinstance(self._error, IperfCleanupError):
            self._observed_error = True
            raise self._error
        if cancelled is not None:
            raise cancelled

    def _require_entered(self) -> None:
        if not self._entered or self._owner is None:
            raise RuntimeError("use EventStream inside an async with context")

    def _put(self, event: LiveEvent) -> None:
        # Parent normalization already detached this payload. Adopt it here;
        # copying for the consumer happens only when dequeuing on the event loop.
        try:
            size = live_event_size(event)
        except IPCError:
            # Typed projection can be larger than its original native frame.
            # Dropping an oversized advisory event cannot discard the result.
            size = MAX_STREAM_BYTES + 1
        with self._lock:
            self._identity = event_identity(event)
            if len(self._queue) >= MAX_STREAM_EVENTS or self._bytes + size > MAX_STREAM_BYTES:
                self._dropped += 1
            else:
                self._queue.append((event, size))
                self._bytes += size
            if not self._wake_pending:
                self._wake_pending = True
                assert self._loop is not None
                self._loop.call_soon_threadsafe(self._wake)

    def _wake(self) -> None:
        with self._lock:
            self._wake_pending = False
        assert self._available is not None
        self._available.set()

    def _settled(self, owner: asyncio.Task[Result]) -> None:
        if self._terminal is not None:
            return
        assert owner.done(), "terminal delivery requires a fully settled operation owner"
        try:
            self._value = owner.result()
        except BaseException as exc:
            self._error = exc
        with self._lock:
            self._terminal = make_terminal_event(
                self._value,
                self._error,
                delivery_sequence=0,
                identity=self._identity,
                consumer_dropped=self._dropped,
            )
        assert self._available is not None
        self._available.set()
