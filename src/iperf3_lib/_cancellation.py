"""Private cancellation arbitration and ownership of one disposable worker."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import Callable

from .exceptions import IperfCleanupError, IperfLibraryError
from .result import Result

_TERMINATE_GRACE = 2.0
_CLEANUP_BUDGET = 4.0
_RETAINED_LOCK = threading.Lock()
_RETAINED_CONTROLS: set[_ExecutionControl] = set()


class _ExecutionControl:
    """Keep cancellation, completion and process cleanup under one owner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cleanup_attempted = threading.Event()
        self._reaped = threading.Event()
        self._cause: BaseException | None = None
        self._completed = False
        self._started = False
        self._process: subprocess.Popen[str] | None = None
        self._owner: threading.Thread | None = None
        self._cleanup_error: IperfCleanupError | None = None
        self._reap_callbacks: list[Callable[[], None]] = []

    def begin(self) -> bool:
        """Admit an executor invocation unless cancellation already won."""
        with self._lock:
            if self._cause is not None:
                return False
            self._started = True
            return True

    def request_cancel(self, error: asyncio.CancelledError | None = None) -> bool:
        """Record cancellation and return whether executor work has started."""
        self.abort(error if error is not None else asyncio.CancelledError())
        with self._lock:
            return self._started

    def abort(self, error: BaseException) -> None:
        """Commit the first terminal failure and wake the process owner."""
        with self._lock:
            if not self._completed and self._cause is None:
                self._cause = error
                self._stop.set()

    def check(self) -> None:
        """Raise the winning terminal cause before admitting further work."""
        with self._lock:
            cause = self._cause
        if cause is not None:
            raise cause

    def complete(self) -> None:
        """Commit completion only after the caller has validated and reaped."""
        with self._lock:
            if self._cause is not None:
                raise self._cause
            self._completed = True

    @property
    def cleanup_confirmed(self) -> bool:
        """Whether no child was acquired, or its exit has been reaped."""
        with self._lock:
            return self._process is None or self._reaped.is_set()

    def when_reaped(self, callback: Callable[[], None]) -> None:
        """Release retained server admission only when the process is gone."""
        with self._lock:
            if self._process is not None and not self._reaped.is_set():
                self._reap_callbacks.append(callback)
                return
        callback()

    def register(self, process: subprocess.Popen[str]) -> None:
        """Transfer a just-created process to its single shutdown owner."""
        with self._lock:
            if self._process is not None:
                raise RuntimeError("Execution control already owns a process")
            self._process = process
        owner = threading.Thread(target=self._supervise, daemon=True)
        self._owner = owner
        owner.start()

    def wait_for_exit(self, timeout: float) -> bool:
        """Wait for the owner to observe natural exit, checking cancellation."""
        deadline = time.monotonic() + timeout
        while not self._reaped.wait(min(0.05, max(0, deadline - time.monotonic()))):
            self.check()
            if time.monotonic() >= deadline:
                return False
        self.check()
        return True

    def close(self) -> None:
        """Finish one bounded cleanup attempt without abandoning ownership."""
        if self._process is None:
            return
        self._stop.set()
        if self._owner is None or self._owner.ident is None:
            # Thread startup can fail after Popen succeeded. The invoking
            # thread remains the owner until this emergency cleanup finishes.
            self._stop_process()
        self._cleanup_attempted.wait()
        if self._cleanup_error is not None:
            raise self._cleanup_error from self._cause

    def _mark_reaped(self) -> None:
        with self._lock:
            self._reaped.set()
            callbacks, self._reap_callbacks = self._reap_callbacks, []
        with _RETAINED_LOCK:
            _RETAINED_CONTROLS.discard(self)
        try:
            for callback in callbacks:
                callback()
        finally:
            self._cleanup_attempted.set()

    def _supervise(self) -> None:
        process = self._process
        assert process is not None
        try:
            while not self._stop.wait(0.02):
                if process.poll() is not None:
                    self._mark_reaped()
                    return
        except Exception as exc:
            self.abort(IperfLibraryError(f"Cannot observe native worker exit: {exc}"))
        self._stop_process()

    def _stop_process(self) -> None:
        process = self._process
        assert process is not None
        deadline = time.monotonic() + _CLEANUP_BUDGET
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=min(_TERMINATE_GRACE, max(0, deadline - time.monotonic())))
                except Exception:
                    process.kill()
                    process.wait(timeout=max(0, deadline - time.monotonic()))
            self._mark_reaped()
            return
        except Exception as exc:
            failure = exc
        # Exit can race either signal. Check once more before reporting a
        # failed cleanup rather than mistaking ProcessLookupError for a leak.
        try:
            if process.poll() is not None:
                self._mark_reaped()
                return
        except Exception:
            pass
        self._cleanup_error = IperfCleanupError(
            "Native worker cleanup could not be confirmed; process ownership is retained",
            control=self,
        )
        self._cleanup_error.add_note(f"Cleanup failure: {failure}")
        # A thread-start failure can leave no running reaper. Keep a library
        # root even if the caller drops the exception and cyclic GC runs.
        with _RETAINED_LOCK:
            _RETAINED_CONTROLS.add(self)
        self._cleanup_attempted.set()
        # Do not drop the Popen handle after reporting failed cleanup. Keep
        # collecting the eventual exit; server admission remains held meanwhile.
        if threading.current_thread() is self._owner:
            self._reap_later()
        else:
            try:
                owner = threading.Thread(target=self._reap_later, daemon=True)
                self._owner = owner
                owner.start()
            except Exception:
                # The registry retains ownership when resource exhaustion
                # prevents a background reaper; do not claim automatic retry.
                self._cleanup_error.add_note("Background reaper could not start")

    def _reap_later(self) -> None:
        process = self._process
        assert process is not None
        while True:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                time.sleep(0.1)
                continue
            self._mark_reaped()
            return


async def _run_async(operation: Callable[[_ExecutionControl], Result]) -> Result:
    """Propagate task cancellation and wait for owned native cleanup."""
    control = _ExecutionControl()

    def invoke() -> Result:
        if not control.begin():
            control.check()
        return operation(control)

    future = asyncio.get_running_loop().run_in_executor(None, invoke)
    try:
        # wait() leaves executor work owned when this coroutine is cancelled.
        # Unlike shield(), it does not log the eventual worker exception as
        # unhandled on Python 3.14 while we are already awaiting cleanup.
        await asyncio.wait((future,))
        return future.result()
    except asyncio.CancelledError as cancelled:
        started = control.request_cancel(cancelled)
        if not started:
            # A queued invocation may outlive this await, but begin() prevents
            # it from running the operation. Observe its eventual exception.
            def consume(done: asyncio.Future[Result]) -> None:
                if not done.cancelled():
                    done.exception()

            future.add_done_callback(consume)
            raise
        while not future.done():
            try:
                await asyncio.wait((future,))
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except IperfCleanupError as exc:
            if exc.__cause__ is None:
                raise exc from cancelled
            raise
        except BaseException:
            pass
        # Asyncio cancellation remains the outward signal even if a deadline
        # won internally just before delivery. Cleanup errors retain that
        # winning cause instead of replacing their diagnostic chain.
        raise cancelled
