"""Bounded transport for a disposable Python/libiperf process."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from .events import NativeEvent
from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .result import Diagnostic, ExecutionMetadata, Result, result_from_iperf_json


def _decode_result(message: dict[str, Any], role: Literal["client", "server"]) -> Result:
    """Decode copied native output without trusting a child-supplied Python object."""
    raw = message.get("raw")
    result = (
        result_from_iperf_json(raw, reporting_role=role)
        if isinstance(raw, dict) and raw
        else Result(ok=False, error=message.get("error") or "No JSON returned by libiperf")
    )
    result.reporting_role = role
    if result.execution is None:
        result.execution = ExecutionMetadata(status="completed" if result.ok else "incomplete")
    metadata = result.execution
    if metadata.native_version is None:
        metadata.native_version = message.get("native_version")
    metadata.timing.started_at_seconds = message["started_at"]
    metadata.timing.completed_at_seconds = message["completed_at"]
    metadata.timing.elapsed_seconds = message["elapsed"]
    result.started_at_seconds = message["started_at"]
    result.completed_at_seconds = message["completed_at"]
    if message.get("error"):
        result.ok = False
        result.error = message["error"]
        metadata.status = "failed"
        result.diagnostics.append(Diagnostic(result.error, "error", code="execution.native_error"))
    elif not raw:
        metadata.status = "incomplete"
    result.extensions["iperf3_lib.native_configuration"] = message.get("evidence", {})
    if "native_returncode" in message:
        result.extensions["iperf3_lib.native_status"] = {
            "returncode": message["native_returncode"],
            "error_code": message.get("native_error_code"),
        }
        if message["native_returncode"] < 0 and message.get("native_error_code") == 0:
            result.diagnostics.append(
                Diagnostic(
                    "The native call returned failure without setting its process-global error code; original native JSON is retained.",
                    "warning",
                    code="execution.native_error_detail_missing",
                    evidence_paths=["/extensions/iperf3_lib.native_status"],
                )
            )
    result.extensions["iperf3_lib.event_delivery"] = {
        "emitted": message.get("events_emitted", 0),
        "dropped": message.get("events_dropped", 0),
        "queue_capacity": 256,
    }
    if message.get("raw_representation") == "reconstructed_events" and (
        raw or message.get("native_events")
    ):
        result.extensions["iperf3_lib.native_json"] = {
            "representation": "reconstructed_events",
            "events": message.get("native_events", []),
        }
        result.diagnostics.append(
            Diagnostic(
                "This native version supplies streaming events without a complete final JSON document; raw is reconstructed from retained events.",
                "info",
                code="execution.reconstructed_json",
                evidence_paths=["/extensions/iperf3_lib.native_json/events"],
            )
        )
    return result


def run_worker(
    role: Literal["client", "server"],
    options: dict[str, Any],
    *,
    password: str | None = None,
    on_event: Callable[[NativeEvent], None] | None = None,
    on_result: Callable[[Result], None] | None = None,
    max_runs: int | None = 1,
    should_stop: Callable[[], bool] | None = None,
    timeout: float | None = None,
) -> Result:
    """Run native code in a child and reap it on every return/error path.

    Timeout includes worker startup and the complete serving session. It raises
    TimeoutError after terminating/reaping the child; this is process cleanup,
    not a claim that native cancellation or C finalizers executed. Callback
    errors stop further delivery and are raised after the active run finishes.
    """
    if role not in {"client", "server"}:
        raise ValueError("role must be client or server")
    for name, callback in (("on_event", on_event), ("on_result", on_result)):
        if callback is not None and not callable(callback):
            raise TypeError(f"{name} must be callable")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
    if max_runs is not None and (type(max_runs) is not int or max_runs < 1):
        raise ValueError("max_runs must be a positive integer or None")
    if should_stop is not None and should_stop():
        return Result(
            ok=False,
            error="Server stopped before accepting a test",
            reporting_role=role,
            execution=ExecutionMetadata(status="incomplete"),
        )
    request = {
        "role": role,
        "options": options,
        "password": password,
        "events": on_event is not None,
        "max_runs": max_runs,
    }
    environment = os.environ.copy()
    # Preserve the calling interpreter's import resolution, including editable
    # source checkouts, without requiring multiprocessing's __main__ guard.
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
    started = time.monotonic()
    messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
    dropped = 0
    closing = threading.Event()
    expired = threading.Event()
    termination_lock = threading.Lock()

    def stop_child() -> None:
        with termination_lock:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def expire() -> None:
        expired.set()
        stop_child()

    watchdog = None
    if timeout is not None:
        watchdog = threading.Timer(max(0, timeout - (time.monotonic() - started)), expire)
        watchdog.daemon = True

    def enqueue(item: dict[str, Any]) -> None:
        while not closing.is_set():
            try:
                messages.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_messages() -> None:
        nonlocal dropped
        try:
            for line in stdout:
                if closing.is_set():
                    break
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("invalid worker message")
                if item.get("type") == "event":
                    try:
                        messages.put_nowait(item)
                    except queue.Full:
                        dropped += 1
                else:
                    enqueue(item)
        except (ValueError, OSError) as exc:
            enqueue({"type": "transport_error", "message": str(exc)})
        finally:
            enqueue({"type": "eof"})

    reader = threading.Thread(target=read_messages, daemon=True)
    callback_error: Exception | None = None
    last_result: Result | None = None
    count = 0
    process = subprocess.Popen(
        [sys.executable, "-m", "iperf3_lib._worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None and process.stdout is not None
    stdout = process.stdout
    try:
        if watchdog is not None:
            assert timeout is not None
            watchdog.interval = max(0, timeout - (time.monotonic() - started))
            watchdog.start()
        reader.start()
        try:
            process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
            process.stdin.flush()
        except BrokenPipeError as exc:
            if expired.is_set():
                raise TimeoutError("Native process deadline expired during startup") from exc
            raise IperfLibraryError("Native worker closed its input during startup") from exc
        while True:
            if expired.is_set() or (timeout is not None and time.monotonic() - started >= timeout):
                raise TimeoutError(f"Native {role} exceeded the {timeout:g}s process deadline")
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if expired.is_set():
                raise TimeoutError(f"Native {role} exceeded the {timeout:g}s process deadline")
            kind = message.get("type")
            if kind == "event":
                if on_event is not None and callback_error is None:
                    try:
                        on_event(
                            NativeEvent(
                                message["kind"],
                                message["data"],
                                message["sequence"],
                                message["time"],
                            )
                        )
                    except Exception as exc:
                        callback_error = exc
            elif kind == "result":
                last_result = _decode_result(message, role)
                if role == "server":
                    last_result.extensions["iperf3_lib.server_config"] = json.loads(
                        json.dumps(options)
                    )
                delivery = last_result.extensions["iperf3_lib.event_delivery"]
                assert isinstance(delivery, dict)
                delivery["dropped"] = int(message.get("events_dropped", 0)) + dropped
                dropped = 0
                count += 1
                native_completed = last_result.ok
                if on_result is not None and callback_error is None:
                    try:
                        on_result(last_result)
                    except Exception as exc:
                        callback_error = exc
                if role == "server":
                    if expired.is_set():
                        raise TimeoutError(
                            f"Native {role} exceeded the {timeout:g}s process deadline"
                        )
                    keep_running = (
                        callback_error is None
                        and native_completed
                        and (max_runs is None or count < max_runs)
                        and (should_stop is None or not should_stop())
                    )
                    process.stdin.write(json.dumps({"continue": keep_running}) + "\n")
                    process.stdin.flush()
            elif kind == "error":
                error_type = {
                    "TypeError": TypeError,
                    "ValueError": ValueError,
                    "IperfError": IperfError,
                    "IperfLibraryError": IperfLibraryError,
                    "UnsupportedFeatureError": UnsupportedFeatureError,
                    "FileNotFoundError": FileNotFoundError,
                }.get(message.get("class"), IperfLibraryError)
                raise error_type(message.get("message", "Native worker failed"))
            elif kind == "done":
                process.wait(timeout=5)
                if process.returncode:
                    raise IperfLibraryError(f"Native worker exited with code {process.returncode}")
                if callback_error is not None:
                    raise callback_error
                if last_result is None:
                    raise IperfLibraryError("Native worker returned no result")
                return last_result
            elif kind in {"eof", "transport_error"}:
                process.wait(timeout=5)
                raise IperfLibraryError(
                    f"Native worker ended without completing its response (exit {process.returncode})"
                )
    finally:
        closing.set()
        if watchdog is not None:
            watchdog.cancel()
        stop_child()
        try:
            process.stdin.close()
        except OSError:
            pass
        if reader.ident is not None:
            reader.join(timeout=1)
        stdout.close()
        if watchdog is not None and watchdog.ident is not None:
            watchdog.join(timeout=1)
