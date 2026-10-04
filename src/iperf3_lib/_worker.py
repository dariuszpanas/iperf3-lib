"""Private framed worker; native parsing and process-global state stay isolated."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
import threading
import time
from typing import Any, BinaryIO

from ._event_capture import BoundedDocumentCapture, EventCapture
from ._ipc import BoundedFrameQueue, IPCError, Session, read_frame
from .exceptions import IperfError, IperfLibraryError
from .ffi.api import POSSIBLE_NAMES, ffi, lib


def _native_error() -> str:
    message = lib.iperf_strerror(lib.i_errno)
    return (
        ffi.string(message).decode("utf-8", errors="replace")
        if message != ffi.NULL
        else "Native error"
    )


def _validate_request(request: dict[str, Any]) -> None:
    """Reject ambiguous private commands before allocating any native test."""
    if request["type"] != "request" or request["run_index"] != 1:
        raise IPCError("Worker requires one initial request for run 1")
    if request.get("role") not in {"client", "server"}:
        raise IPCError("Worker role must be client or server")
    if not isinstance(request.get("options"), dict):
        raise IPCError("Worker options must be an object")
    if type(request.get("events", False)) is not bool:
        raise IPCError("Worker event admission must be a boolean")
    if type(request.get("live_events", False)) is not bool:
        raise IPCError("Worker typed event admission must be a boolean")
    maximum = request.get("max_runs", 1)
    if maximum is not None and (type(maximum) is not int or maximum < 1):
        raise IPCError("Worker maximum runs must be positive or null")
    if request["role"] == "client" and maximum != 1:
        raise IPCError("Client workers execute exactly one run")
    if request.get("password") is not None and not isinstance(request["password"], str):
        raise IPCError("Worker password must be a string or null")


def _producer(version: str) -> dict[str, Any]:
    """Report observed versions and a selector, without claiming a binary path."""
    selector = os.environ.get("IPERF3_LIB")
    return {
        "package_version": importlib.metadata.version("iperf3-lib"),
        "python_version": platform.python_version(),
        "native_version": version,
        "library_selector": (
            {"source": "IPERF3_LIB", "value": selector}
            if selector
            else {"source": "platform_search", "candidates": list(POSSIBLE_NAMES)}
        ),
    }


def execute(request: dict[str, Any], output: BinaryIO, commands: BinaryIO) -> None:
    """Own framed output and free each native test before its result or terminal.

    Capture, parser retention and delivery frames have independent byte/count
    bounds. A parser settles before any result or terminal frame is published.
    """
    from .native_options import configure_native, observe_native_options

    session = Session.from_request(request)
    outgoing = BoundedFrameQueue()
    writer_error: list[Exception] = []

    def write_messages() -> None:
        try:
            while True:
                try:
                    frame = outgoing.get()
                except EOFError:
                    return
                pending = memoryview(frame)
                while pending:
                    written = output.write(pending)
                    if written is None or written <= 0:
                        raise IperfLibraryError("Native worker output transport made no progress")
                    pending = pending[written:]
                output.flush()
        except Exception as exc:
            writer_error.append(exc)
            outgoing.close()

    writer = threading.Thread(target=write_messages, daemon=True)
    try:
        writer.start()
    except BaseException:
        outgoing.close()
        if writer.ident is not None:
            writer.join(timeout=5)
        raise

    def enqueue(message: dict[str, Any], run: int, *, event: bool = False) -> bool:
        if writer_error:
            raise IperfLibraryError("Native worker output transport failed") from writer_error[0]
        return (
            session.emit(
                message,
                run_index=run,
                admit=lambda frame: outgoing.put(frame, event=event),
            )
            is not None
        )

    test = ffi.NULL
    count = 0
    run_index = 1
    failure: BaseException | None = None
    capture: EventCapture | None = None
    callback = None

    def release_test() -> None:
        nonlocal test
        if test != ffi.NULL:
            owned, test = test, ffi.NULL
            lib.iperf_free_test(owned)

    try:
        session.validate(request)
        _validate_request(request)
        role = request["role"]
        options = request["options"]
        for symbol in (
            "iperf_new_test",
            "iperf_defaults",
            "iperf_free_test",
            "iperf_get_iperf_version",
            "iperf_set_test_json_callback",
            "iperf_run_client" if role == "client" else "iperf_run_server",
        ):
            getattr(lib, symbol)
        test = lib.iperf_new_test()
        if test == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        if lib.iperf_defaults(test) < 0:
            raise IperfError(_native_error())
        setup = configure_native(test, role, options, password=request.get("password"))
        version_pointer = lib.iperf_get_iperf_version()
        if version_pointer == ffi.NULL:
            raise IperfLibraryError("Native library did not report its version")
        version = ffi.string(version_pointer).decode("utf-8")
        if not version:
            raise IperfLibraryError("Native library reported an empty version")
        while True:
            # Bind each parser to its run rather than sharing a mutable run index.
            admitted_run = run_index
            capture = EventCapture(
                ffi,
                lambda message, run=admitted_run: enqueue(message, run, event=True),
                legacy_events=request.get("events", False),
                live_events=request.get("live_events", False),
            )
            capture.start()
            callback = ffi.callback("void(iperf_test *, char *)", capture.capture)
            lib.iperf_set_test_json_callback(test, callback)
            if run_index == 1:
                enqueue(
                    {"type": "ready", "pid": os.getpid(), "producer": _producer(version)}, run_index
                )
            started_at, started = time.time(), time.monotonic()
            ret = lib.iperf_run_client(test) if role == "client" else lib.iperf_run_server(test)
            error_code = int(lib.i_errno) if ret < 0 else None
            error = _native_error() if ret < 0 else None
            evidence = observe_native_options(test, options)
            full_document = None
            getter_error = None
            getter = getattr(lib, "iperf_get_test_json_output_string", None)
            if getter is not None:
                try:
                    full_document = BoundedDocumentCapture(ffi).read(getter(test))
                except Exception as exc:
                    getter_error = f"Cannot copy native getter: {type(exc).__name__}"
            # No pointer survives free. Keep the callback alive through free,
            # then prevent parser events from racing the result/control frames.
            release_test()
            capture.close()
            capture.recover_document(full_document, getter_error)
            raw = capture.raw
            if (
                ret < 0
                and isinstance(raw.get("error"), str)
                and raw["error"] not in {"", "no error"}
            ):
                error = raw["error"]
            elif ret < 0 and error_code == 0:
                error = "Native call failed without a specific native error code"
            elif error is None and capture.native_error is not None:
                # An observed native error remains a failure even if a later
                # complete document omits it or the native return code is zero.
                error = capture.native_error
            message = {
                "type": "result",
                "raw": raw,
                "error": error,
                "native_returncode": ret,
                "native_error_code": error_code,
                "evidence": evidence,
                "native_version": version,
                "started_at": started_at,
                "completed_at": time.time(),
                "elapsed": time.monotonic() - started,
                "events_emitted": capture.events_emitted,
                "events_dropped": capture.events_dropped,
                "raw_representation": "native_complete"
                if capture.complete_document
                else "reconstructed_events",
                "native_events": capture.native_events,
                "capture": capture.metadata(),
            }
            enqueue(message, run_index)
            capture = None
            count += 1
            if role == "client":
                break
            command = read_frame(commands)
            if command is None:
                raise IPCError("Server continuation ended before a command was received")
            session.validate(command)
            if (
                command["type"] != "continue"
                or command["run_index"] != run_index
                or type(command.get("continue")) is not bool
            ):
                raise IPCError("Server continuation must identify the completed run and a boolean")
            if not command["continue"]:
                break
            maximum = request.get("max_runs", 1)
            if maximum is not None and count >= maximum:
                raise IPCError("Server continuation exceeds the requested maximum runs")
            run_index += 1
            test = lib.iperf_new_test()
            if test == ffi.NULL:
                raise IperfLibraryError("iperf_new_test failed")
            if lib.iperf_defaults(test) < 0:
                raise IperfError(_native_error())
            setup = configure_native(test, role, options, password=request.get("password"))
        _ = setup, callback
    except BaseException as exc:
        failure = exc
    finally:
        try:
            release_test()
        except BaseException as exc:
            failure = exc
        try:
            if capture is not None:
                capture.close()
        except BaseException as exc:
            failure = exc
        try:
            if failure is not None:
                if capture is not None and not capture.settled:
                    # A parser that still owns emission must never race terminal.
                    # EOF without terminal leaves process cleanup with the parent.
                    raise failure
                enqueue(
                    {
                        "type": "error",
                        "class": type(failure).__name__,
                        "message": str(failure)[:4096],
                    },
                    run_index,
                )
            enqueue(
                {"type": "terminal", "status": "failed" if failure else "completed", "runs": count},
                run_index,
            )
        finally:
            outgoing.close()
            writer.join(timeout=5)
    if writer_error or writer.is_alive():
        raise IperfLibraryError("Native worker output transport failed") from (
            writer_error[0] if writer_error else failure
        )
    if failure is not None:
        raise failure


def main() -> None:
    """Reserve binary IPC output and use one writer for every identified response."""
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0) as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            request = read_frame(sys.stdin.buffer)
            if request is None:
                raise IPCError("Native worker received no initial request")
            Session.from_request(request).validate(request)
        except Exception as exc:
            print(f"Invalid native worker request: {exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
        try:
            execute(request, output, sys.stdin.buffer)
        except Exception as exc:
            # execute owns error and terminal delivery. Never append another
            # response after its writer drained or its transport failed.
            print(f"Native worker execution failed: {exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
