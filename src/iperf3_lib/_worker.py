"""Private one-session worker; never invoke native argument parsing in the host."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any, TextIO

from .exceptions import IperfError, IperfLibraryError
from .ffi.api import ffi, lib


def _native_error() -> str:
    message = lib.iperf_strerror(lib.i_errno)
    return (
        ffi.string(message).decode("utf-8", errors="replace")
        if message != ffi.NULL
        else "Native error"
    )


def execute(request: dict[str, Any], output: TextIO, commands: TextIO) -> None:
    """Allocate, configure, run and free one native test in this disposable process."""
    from .native_options import configure_native, observe_native_options

    role = request["role"]
    options = request["options"]
    test = ffi.NULL
    outgoing: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
    writer_error: list[Exception] = []

    def write_messages() -> None:
        try:
            while (item := outgoing.get()) is not None:
                output.write(json.dumps(item, allow_nan=False) + "\n")
                output.flush()
        except Exception as exc:
            writer_error.append(exc)

    writer = threading.Thread(target=write_messages, daemon=True)
    writer.start()

    def enqueue(item: dict[str, Any] | None) -> None:
        while not writer_error:
            try:
                outgoing.put(item, timeout=0.1)
                return
            except queue.Full:
                continue
        raise IperfLibraryError("Native worker output transport failed")

    try:
        test = lib.iperf_new_test()
        if test == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        if lib.iperf_defaults(test) < 0:
            raise IperfError(_native_error())
        setup = configure_native(test, role, options, password=request.get("password"))
        version = ffi.string(lib.iperf_get_iperf_version()).decode("utf-8")
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
                    if request.get("events"):
                        try:
                            outgoing.put_nowait(
                                {
                                    "type": "event",
                                    "kind": kind,
                                    "data": part,
                                    "sequence": sequence,
                                    "time": time.time(),
                                }
                            )
                        except queue.Full:
                            dropped += 1
                else:
                    raw = data
                    full_json_seen = True
            except Exception as exc:
                callback_errors.append(f"Cannot decode native event: {type(exc).__name__}")

        callback = ffi.callback("void(iperf_test *, char *)", capture)
        lib.iperf_set_test_json_callback(test, callback)
        count = 0
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
                # Some server failures return -1 after clearing i_errno. The
                # copied JSON retains the specific authentication/policy error.
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
            lib.iperf_free_test(test)
            test = ffi.NULL
            enqueue(message)
            count += 1
            if role == "client":
                break
            command = commands.readline()
            if not command or not json.loads(command).get("continue"):
                break
            if request.get("max_runs") is not None and count >= request["max_runs"]:
                break
            # libiperf reset retains its previous allocated JSON output string.
            # A new test per session prevents stale output and native leakage.
            test = lib.iperf_new_test()
            if test == ffi.NULL:
                raise IperfLibraryError("iperf_new_test failed")
            if lib.iperf_defaults(test) < 0:
                raise IperfError(_native_error())
            setup = configure_native(test, role, options, password=request.get("password"))
            lib.iperf_set_test_json_callback(test, callback)
        # These references must outlive every native call and free.
        _ = setup, callback
    finally:
        if test != ffi.NULL:
            lib.iperf_free_test(test)
        if not writer_error:
            enqueue(None)
        writer.join(timeout=5)
    if writer_error or writer.is_alive():
        raise IperfLibraryError("Native worker output transport failed")


def main() -> None:
    """Reserve an IPC descriptor before redirecting native stdout away from it."""
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1) as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            request = json.loads(sys.stdin.readline())
            execute(request, output, sys.stdin)
        except Exception as exc:
            output.write(
                json.dumps(
                    {
                        "type": "error",
                        "class": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                + "\n"
            )
            output.flush()
        output.write('{"type":"done"}\n')
        output.flush()


if __name__ == "__main__":
    main()
