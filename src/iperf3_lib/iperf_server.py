"""Server wrappers backed by an isolated libiperf Python worker."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import asdict, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from .exceptions import IperfCleanupError, IperfError
from .result import Result
from .server_config import ServerConfig

if TYPE_CHECKING:
    from ._cancellation import _ExecutionControl
    from ._event_stream import EventStream
    from .events import LiveEvent, NativeEvent


class _Unset(Enum):
    TOKEN = 0


def _run_worker(options: dict[str, Any], **kwargs: Any) -> Result:
    """Load the process transport only when a server operation is admitted."""
    from ._execution import run_worker

    return run_worker("server", options, **kwargs)


def _validate_call(
    on_event: Callable[[NativeEvent], None] | None,
    on_result: Callable[[Result], None] | None,
    timeout: float | None,
) -> None:
    for name, callback in (("on_event", on_event), ("on_result", on_result)):
        if callback is not None and not callable(callback):
            raise TypeError(f"{name} must be callable")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")


class Server:
    """Run a validated server in a separate Python process using libiperf.

    ``Server(port, bind_host)`` remains available for existing callers.
    New settings are supplied with ``Server(config=ServerConfig(...))``.
    A server instance admits one operation at a time. Each operation snapshots
    its configuration, so later mutations cannot change an active listener.
    """

    def __init__(
        self,
        port: int | _Unset = _Unset.TOKEN,
        bind_host: str | None | _Unset = _Unset.TOKEN,
        *,
        config: ServerConfig | None = None,
    ) -> None:
        """Detach the supplied configuration and validate legacy arguments."""
        if config is not None:
            if not isinstance(config, ServerConfig):
                raise TypeError("config must be a ServerConfig")
            if port is not _Unset.TOKEN or bind_host is not _Unset.TOKEN:
                raise ValueError("config cannot be combined with port or bind_host")
            self.config = replace(config)
        else:
            self.config = ServerConfig(
                port=5201 if port is _Unset.TOKEN else port,
                bind_address=None if bind_host is _Unset.TOKEN else bind_host,
            )
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()

    @property
    def port(self) -> int:
        """Return the configured listener port (legacy alias)."""
        return self.config.port

    @port.setter
    def port(self, value: int) -> None:
        self.config = replace(self.config, port=value)

    @property
    def bind_host(self) -> str | None:
        """Return the configured local address (legacy alias)."""
        return self.config.bind_address

    @bind_host.setter
    def bind_host(self, value: str | None) -> None:
        self.config = replace(self.config, bind_address=value)

    def _snapshot(self, *, events: bool = False) -> dict[str, Any]:
        if not isinstance(self.config, ServerConfig):
            raise TypeError("config must be a ServerConfig")
        return asdict(replace(self.config, json_stream=True) if events else replace(self.config))

    def run_once(
        self,
        *,
        on_event: Callable[[NativeEvent], None] | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Return one server result; an optional timeout bounds the worker.

        Native test failures are retained as failed results. Worker setup
        failures raise an exception, and an elapsed timeout raises TimeoutError.
        """
        return self._run_once(on_event=on_event, timeout=timeout)

    def _run_once(
        self,
        *,
        on_event: Callable[[NativeEvent], None] | None = None,
        timeout: float | None = None,
        _control: _ExecutionControl | None = None,
        _on_live_event: Callable[[LiveEvent], None] | None = None,
        _options: dict[str, Any] | None = None,
    ) -> Result:
        """Retain server ownership until the operation and its worker have ended."""
        _validate_call(on_event, None, timeout)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Server already has an active operation")
        release_control = _control
        try:
            execution_options = {"_control": _control} if _control is not None else {}
            if _on_live_event is not None:
                execution_options["_on_live_event"] = _on_live_event
            return _run_worker(
                _options
                if _options is not None
                else self._snapshot(events=on_event is not None or _on_live_event is not None),
                on_event=on_event,
                max_runs=1,
                timeout=timeout,
                **execution_options,
            )
        except IperfCleanupError as exc:
            release_control = exc._control
            raise
        finally:
            if release_control is None or release_control.cleanup_confirmed:
                self._run_lock.release()
            else:
                release_control.when_reaped(self._run_lock.release)

    def events_once(self, *, timeout: float | None = None) -> EventStream:
        """Create an owned live-event context for one isolated server run."""
        from ._event_stream import EventStream

        def admit():
            _validate_call(None, None, timeout)
            options = self._snapshot(events=True)
            return lambda control, sink: self._run_once(
                timeout=timeout, _control=control, _on_live_event=sink, _options=options
            )

        return EventStream(admit)

    def serve_forever(
        self,
        *,
        on_result: Callable[[Result], None] | None = None,
        on_event: Callable[[NativeEvent], None] | None = None,
        max_runs: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """Serve sequential tests, passing each completed attempt to on_result.

        Each native test is freed before its result callback is delivered.
        ``stop()`` is cooperative between tests; use ``timeout`` to bound a
        blocked listener or an active test. A native idle timeout ends the loop.
        Callback errors propagate after the worker is shut down.
        Without an on_result handler, native failures raise IperfError.
        """
        _validate_call(on_event, on_result, timeout)
        if max_runs is not None:
            if isinstance(max_runs, bool) or not isinstance(max_runs, int):
                raise TypeError("max_runs must be an integer")
            if max_runs < 1:
                raise ValueError("max_runs must be positive")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Server already has an active operation")
        release_control = None
        try:
            options = self._snapshot(events=on_event is not None)
            if self._stop_event.is_set():
                return
            result = _run_worker(
                options,
                on_event=on_event,
                on_result=on_result,
                max_runs=max_runs,
                should_stop=self._stop_event.is_set,
                timeout=timeout,
            )
            if (
                on_result is None
                and not result.ok
                and result.execution is not None
                and result.execution.status == "failed"
            ):
                raise IperfError(result.error or "Native server failed")
        except IperfCleanupError as exc:
            release_control = exc._control
            raise
        finally:
            if release_control is None or release_control.cleanup_confirmed:
                self._run_lock.release()
            else:
                release_control.when_reaped(self._run_lock.release)

    async def aserve_once(
        self,
        *,
        on_event: Callable[[NativeEvent], None] | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Await one isolated server result with cancellation cleanup.

        Cancellation stops the owned worker before propagating CancelledError.
        A callback already running must return before cleanup releases this
        instance for another operation. This uses the built-in isolated path
        rather than delegating to an overridden ``run_once()`` method.
        """
        from ._cancellation import _run_async

        return await _run_async(
            lambda control: self._run_once(on_event=on_event, timeout=timeout, _control=control)
        )

    def stop(self) -> None:
        """Request that serve_forever stop before its next native test."""
        self._stop_event.set()
