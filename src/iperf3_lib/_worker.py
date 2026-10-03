"""Private framed worker; native parsing and process-global state stay isolated."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
import threading
import time
from typing import Any, BinaryIO

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

    Event frames have byte and count bounds, with reserved control capacity.
    Copied native JSON and reconstruction data are not a total memory bound.
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
        raw: dict[str, Any] = {}
        callback_errors: list[str] = []
        sequence = 0
        dropped = 0
        native_events: list[dict[str, Any]] = []
        full_json_seen = False

        def capture(_test: Any, payload: Any) -> None:
            nonlocal raw, sequence, dropped, full_json_seen
            try:
                if payload == ffi.NULL:
                    return
                data = json.loads(ffi.string(payload).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("native JSON must be an object")
                if "event" in data and "data" in data:
                    native_events.append(data)
                    kind = data["event"]
                    part = data["data"]
                    if kind == "start":
                        raw["start"] = part
                    elif kind == "interval":
                        raw.setdefault("intervals", []).append(part)
                    elif kind == "end":
                        raw["end"] = part
                    elif kind == "error":
                        raw["error"] = part if isinstance(part, str) else str(part)
                    elif kind == "complete" and isinstance(part, dict):
                        raw = part
                        full_json_seen = True
                    elif kind in {"server_output_json", "server_output_text"}:
                        raw[kind] = part
                    sequence += 1
                    if request.get("events", False):
                        message = {
                            "type": "event",
                            "kind": kind,
                            "data": part,
                            "sequence": sequence,
                            "time": time.time(),
                        }
                        try:
                            admitted = enqueue(message, run_index, event=True)
                        except IPCError:
                            # An event exceeding its wire budget is lost
                            # delivery, just like event queue overflow.
                            admitted = False
                        if not admitted:
                            dropped += 1
                else:
                    raw = data
                    full_json_seen = True
            except Exception as exc:
                callback_errors.append(f"Cannot capture native event: {type(exc).__name__}")

        callback = ffi.callback("void(iperf_test *, char *)", capture)
        lib.iperf_set_test_json_callback(test, callback)
        enqueue({"type": "ready", "pid": os.getpid(), "producer": _producer(version)}, run_index)
        while True:
            raw = {}
            native_events = []
            full_json_seen = False
            sequence = dropped = 0
            callback_errors.clear()
            started_at, started = time.time(), time.monotonic()
            ret = lib.iperf_run_client(test) if role == "client" else lib.iperf_run_server(test)
            error_code = int(lib.i_errno) if ret < 0 else None
            error = _native_error() if ret < 0 else None
            if (
                ret < 0
                and isinstance(raw.get("error"), str)
                and raw["error"] not in {"", "no error"}
            ):
                error = raw["error"]
            elif ret < 0 and error_code == 0:
                error = "Native call failed without a specific native error code"
            if callback_errors:
                error = callback_errors[0]
            message = {
                "type": "result",
                "raw": raw,
                "error": error,
                "native_returncode": ret,
                "native_error_code": error_code,
                "evidence": observe_native_options(test, options),
                "native_version": version,
                "started_at": started_at,
                "completed_at": time.time(),
                "elapsed": time.monotonic() - started,
                "events_emitted": sequence,
                "events_dropped": dropped,
                "raw_representation": "native_complete"
                if full_json_seen
                else "reconstructed_events",
                "native_events": native_events if not full_json_seen else [],
            }
            release_test()
            enqueue(message, run_index)
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
            lib.iperf_set_test_json_callback(test, callback)
        _ = setup, callback
    except BaseException as exc:
        failure = exc
    finally:
        try:
            release_test()
        except BaseException as exc:
            failure = exc
        try:
            if failure is not None:
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
