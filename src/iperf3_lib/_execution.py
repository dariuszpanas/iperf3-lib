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
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from . import _ipc
from ._cancellation import _ExecutionControl
from ._live_event_types import LiveProjector, make_delivery_gap_event
from .events import LiveEvent, NativeEvent, WorkerStatePayload
from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .result import Diagnostic, ExecutionMetadata, Result, result_from_iperf_json


class _Responses:
    """Validate the worker state machine before admitting callbacks or results."""

    def __init__(
        self, session: _ipc.Session, role: str, pid: int, *, live_events: bool = False
    ) -> None:
        self.session, self.role, self.pid = session, role, pid
        self.run = 1
        self.runs = 0
        self.phase = "startup"
        self.producer: dict[str, Any] | None = None
        self.failed = False
        self.event_sequence = 0
        self.live_events = live_events
        self.live_received = self.capture_sequence = 0
        self.arrival_offset = 0.0
        self.capture_gaps: dict[str, int] = {}
        self.terminal_at: float | None = None
        self.lock = threading.Lock()

    def accept(self, item: dict[str, Any]) -> None:
        """Reject identity, ordering and payload errors before queue admission."""
        with self.lock:
            self.session.validate(item)
            kind = item["type"]
            if self.phase == "terminal":
                raise _ipc.IPCError("Native worker sent data after its terminal receipt")
            if item["run_index"] != self.run:
                raise _ipc.IPCError("Native worker sent an unexpected run index")
            if kind == "ready":
                producer = item.get("producer")
                if (
                    self.phase != "startup"
                    or type(item.get("pid")) is not int
                    or item["pid"] != self.pid
                    or not isinstance(producer, dict)
                    or any(
                        not isinstance(producer.get(key), str) or not producer[key]
                        for key in ("package_version", "python_version", "native_version")
                    )
                    or not isinstance(producer.get("library_selector"), dict)
                ):
                    raise _ipc.IPCError("Invalid native worker readiness receipt")
                self.producer, self.phase = producer, "running"
            elif kind == "event":
                if (
                    self.phase != "running"
                    or not isinstance(item.get("kind"), str)
                    or "data" not in item
                    or type(item.get("sequence")) is not int
                    or item["sequence"] <= self.event_sequence
                    or not _finite_number(item.get("time"))
                ):
                    raise _ipc.IPCError("Invalid or out-of-order native worker event")
                self.event_sequence = item["sequence"]
            elif kind == "live_event":
                if self.phase != "running" or not self.live_events:
                    raise _ipc.IPCError("Unexpected typed native worker event")
                _validate_live_frame(item)
                sequence = item["capture_sequence"]
                if (
                    sequence is not None
                    and sequence <= self.capture_sequence
                    or sequence is not None
                    and self.capture_gaps
                    or sequence is not None
                    and item["arrival_offset_seconds"] < self.arrival_offset
                ):
                    raise _ipc.IPCError("Out-of-order typed native worker event")
                if sequence is not None:
                    self.capture_sequence = sequence
                else:
                    stage = item["data"]["stage"]
                    if stage in self.capture_gaps:
                        raise _ipc.IPCError("Duplicate native capture gap")
                    self.capture_gaps[stage] = item["data"]["dropped"]
                if sequence is not None:
                    self.arrival_offset = item["arrival_offset_seconds"]
                self.live_received += 1
            elif kind == "result":
                if (
                    self.phase != "running"
                    or not isinstance(item.get("raw"), dict)
                    or not isinstance(item.get("evidence", {}), dict)
                    or item.get("error") is not None
                    and not isinstance(item["error"], str)
                    or any(
                        not _finite_number(item.get(key))
                        for key in ("started_at", "completed_at", "elapsed")
                    )
                    or any(
                        type(item.get(key, 0)) is not int or item.get(key, 0) < 0
                        for key in ("events_emitted", "events_dropped")
                    )
                    or item.get("events_dropped", 0) > item.get("events_emitted", 0)
                    or item.get("events_emitted", 0) < self.event_sequence
                    or not isinstance(item.get("native_events", []), list)
                    or "native_returncode" in item
                    and type(item["native_returncode"]) is not int
                    or self.producer is None
                    or item.get("native_version") != self.producer["native_version"]
                ):
                    raise _ipc.IPCError("Invalid or out-of-order native worker result")
                if "capture" in item or self.live_events:
                    _validate_capture(item.get("capture"))
                    capture = item["capture"]
                    if (
                        capture["live_emitted"] != self.live_received + capture["live_dropped"]
                        or self.capture_sequence > capture["callbacks"]
                        or not self.live_events
                        and capture["live_emitted"]
                        or any(
                            capture[f"{stage}_dropped"] != dropped
                            for stage, dropped in self.capture_gaps.items()
                        )
                    ):
                        raise _ipc.IPCError("Native capture counters contradict typed delivery")
                self.runs += 1
                self.phase = "continuation" if self.role == "server" else "ending"
            elif kind == "error":
                if (
                    self.failed
                    or not isinstance(item.get("class"), str)
                    or not isinstance(item.get("message"), str)
                ):
                    raise _ipc.IPCError("Invalid native worker error receipt")
                self.failed, self.phase = True, "ending"
            elif kind == "terminal":
                if (
                    self.phase != "ending"
                    or type(item.get("runs")) is not int
                    or item["runs"] != self.runs
                    or item.get("status") != ("failed" if self.failed else "completed")
                ):
                    raise _ipc.IPCError("Invalid or premature native worker terminal receipt")
                self.phase = "terminal"
                self.terminal_at = time.monotonic()
            else:
                raise _ipc.IPCError("Unexpected native worker message type")

    def continue_run(self, keep_running: bool) -> int:
        """Authorize the next run before its command becomes visible to the child."""
        with self.lock:
            if self.phase != "continuation":
                raise _ipc.IPCError("Native worker is not waiting for a continuation")
            completed_run = self.run
            if keep_running:
                self.run += 1
                self.event_sequence = 0
                self.live_received = self.capture_sequence = 0
                self.arrival_offset = 0.0
                self.capture_gaps = {}
                self.phase = "running"
            else:
                self.phase = "ending"
            return completed_run


def _finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _validate_live_frame(item: dict[str, Any]) -> None:
    kind, data, sequence = item.get("kind"), item.get("data"), item.get("capture_sequence")
    if (
        not isinstance(kind, str)
        or not {"data", "capture_sequence", "arrival_offset_seconds", "time"}.issubset(item)
        or kind != "delivery_gap"
        and not _finite_number(item.get("arrival_offset_seconds"))
        or kind != "delivery_gap"
        and not _finite_number(item.get("time"))
        or (
            sequence is not None
            and (type(sequence) is not int or not 0 < sequence <= (1 << 63) - 1)
        )
        or "capture_sequence" not in item
    ):
        raise _ipc.IPCError("Invalid typed native event provenance")
    if kind == "delivery_gap":
        valid = (
            sequence is None
            and item.get("arrival_offset_seconds") is None
            and item.get("time") is None
            and isinstance(data, dict)
            and set(data) == {"stage", "dropped"}
            and data["stage"] in ("capture", "retention")
            and type(data["dropped"]) is int
            and data["dropped"] > 0
        )
    elif sequence is None:
        valid = False
    elif kind in {"native_start", "interval", "native_end", "native_document"}:
        valid = isinstance(data, dict)
    elif kind == "native_error":
        valid = isinstance(data, (str, dict))
    elif kind in {"unknown", "server_output"}:
        valid = (
            isinstance(data, dict)
            and set(data) == {"native_kind", "payload"}
            and isinstance(data["native_kind"], str)
        )
    elif kind == "malformed":
        valid = (
            isinstance(data, dict)
            and set(data) == {"reason", "error_type", "sample"}
            and data["reason"] == "invalid_native_json"
            and isinstance(data["error_type"], str)
            and len(data["error_type"]) <= 256
            and isinstance(data["sample"], str)
            and len(data["sample"]) <= 256
        )
    else:
        valid = False
    if not valid:
        raise _ipc.IPCError("Invalid typed native event payload")


_CAPTURE_COUNTERS = (
    "callbacks",
    "copied",
    "capture_dropped",
    "malformed",
    "retention_dropped",
    "live_emitted",
    "live_dropped",
)
_CAPTURE_LIMITS = {
    "payload_bytes": 8 * 1024 * 1024,
    "pending_bytes": 16 * 1024 * 1024,
    "pending_items": 256,
    "retained_bytes": 12 * 1024 * 1024,
    "diagnostics": 8,
}


def _validate_capture(capture: Any) -> None:
    keys = {
        *_CAPTURE_COUNTERS,
        "schema_version",
        "complete_document",
        "complete_document_source",
        "getter_error",
        "reconstruction_complete",
        "diagnostics",
        "limits",
    }
    if (
        not isinstance(capture, dict)
        or set(capture) != keys
        or type(capture["schema_version"]) is not int
        or capture["schema_version"] != 1
        or any(
            type(capture[key]) is not int or not 0 <= capture[key] <= (1 << 63) - 1
            for key in _CAPTURE_COUNTERS
        )
        or type(capture["complete_document"]) is not bool
        or type(capture["reconstruction_complete"]) is not bool
        or not isinstance(capture["limits"], dict)
        or capture["limits"] != _CAPTURE_LIMITS
        or any(type(value) is not int for value in capture["limits"].values())
        or not isinstance(capture["diagnostics"], list)
        or len(capture["diagnostics"]) > 8
        or any(not isinstance(value, str) or len(value) > 256 for value in capture["diagnostics"])
        or capture["getter_error"] is not None
        and (not isinstance(capture["getter_error"], str) or len(capture["getter_error"]) > 256)
    ):
        raise _ipc.IPCError("Invalid native capture receipt")
    if (
        capture["callbacks"] != capture["copied"] + capture["capture_dropped"]
        or capture["malformed"] > capture["copied"]
        or capture["retention_dropped"] > capture["copied"]
        or capture["live_dropped"] > capture["live_emitted"]
        or capture["complete_document_source"] not in (None, "callback", "getter")
        or capture["complete_document"] != (capture["complete_document_source"] is not None)
        or capture["reconstruction_complete"]
        != (
            not (capture["capture_dropped"] or capture["malformed"] or capture["retention_dropped"])
        )
    ):
        raise _ipc.IPCError("Inconsistent native capture receipt")


class _Inbox:
    """Bound queued decoded frames by their wire bytes and reserve control space."""

    def __init__(self) -> None:
        self.items: deque[tuple[dict[str, Any], int, bool]] = deque()
        self.condition = threading.Condition()
        self.event_bytes = self.events = self.control_bytes = self.controls = 0
        self.dropped = 0
        self.live_dropped = 0
        self.finished = False
        self.failure: Exception | None = None

    def put(self, item: dict[str, Any], size: int) -> None:
        """Drop only events, reserving a bounded FIFO allowance for control frames."""
        event = item["type"] in {"event", "live_event"}
        with self.condition:
            if event:
                if (
                    size > _ipc.MAX_EVENT_BYTES + 4
                    or self.events >= _ipc.MAX_PENDING_EVENTS
                    or self.event_bytes + size > _ipc.MAX_PENDING_EVENT_BYTES
                ):
                    if item["type"] == "live_event":
                        self.live_dropped += 1
                    else:
                        self.dropped += 1
                    return
                self.events += 1
                self.event_bytes += size
            else:
                if (
                    self.controls >= _ipc.MAX_PENDING_CONTROLS
                    or self.control_bytes + size > _ipc.MAX_PENDING_CONTROL_BYTES
                ):
                    raise _ipc.IPCError("Native worker exceeded the control queue allowance")
                self.controls += 1
                self.control_bytes += size
                if item["type"] == "result":
                    item["_parent_dropped"] = self.dropped
                    item["_parent_live_dropped"] = self.live_dropped
                    self.dropped = 0
                    self.live_dropped = 0
            self.items.append((item, size, event))
            self.condition.notify()

    def finish(self, failure: Exception | None = None) -> None:
        """Publish a clean EOF or transport failure independently of queue capacity."""
        with self.condition:
            self.failure, self.finished = failure, True
            self.condition.notify_all()

    def get(self, timeout: float) -> dict[str, Any]:
        """Take the oldest frame, reporting transport errors before more callbacks."""
        with self.condition:
            if not self.items and not self.finished:
                self.condition.wait(timeout)
            if self.failure is not None:
                raise self.failure
            if self.items:
                item, size, event = self.items.popleft()
                if event:
                    self.events -= 1
                    self.event_bytes -= size
                else:
                    self.controls -= 1
                    self.control_bytes -= size
                return item
            if self.finished:
                return {"type": "eof"}
            raise queue.Empty


def _worker_command() -> list[str]:
    """Select the guarded Linux bootstrap without changing other platform paths."""
    if sys.platform == "linux":
        bootstrap = Path(__file__).with_name("_worker_lifetime.py").resolve()
        return [sys.executable, str(bootstrap), str(os.getpid())]
    return [sys.executable, "-m", "iperf3_lib._worker"]


def _decode_result(message: dict[str, Any], role: Literal["client", "server"]) -> Result:
    """Decode copied native output without trusting a child-supplied Python object."""
    raw = message.get("raw")
    try:
        result = (
            result_from_iperf_json(raw, reporting_role=role)
            if isinstance(raw, dict) and raw
            else Result(ok=False, error=message.get("error") or "No JSON returned by libiperf")
        )
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        result = Result(
            ok=False,
            raw=raw if isinstance(raw, dict) else {},
            error="Native JSON could not be normalized",
            execution=ExecutionMetadata(status="incomplete"),
            diagnostics=[
                Diagnostic(
                    "The retained native document has invalid measurement field shapes.",
                    "warning",
                    code="execution.malformed_native_json",
                    evidence_paths=["/raw"],
                )
            ],
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
        "queue_capacity": _ipc.MAX_PENDING_EVENTS,
        "queue_bytes": _ipc.MAX_PENDING_EVENT_BYTES,
        "event_bytes": _ipc.MAX_EVENT_BYTES,
    }
    if "capture" in message:
        capture = message["capture"]
        _validate_capture(capture)
        result.extensions["iperf3_lib.event_capture"] = json.loads(json.dumps(capture))
        if not capture["complete_document"] and not capture["reconstruction_complete"]:
            if metadata.status != "failed":
                result.ok = False
                metadata.status = "incomplete"
            result.diagnostics.append(
                Diagnostic(
                    "Native event capture lost or malformed reconstruction evidence; retained measurements are partial.",
                    "warning",
                    code="execution.incomplete_capture",
                    evidence_paths=["/extensions/iperf3_lib.event_capture"],
                )
            )
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
    _control: _ExecutionControl | None = None,
    _on_live_event: Callable[[LiveEvent], None] | None = None,
) -> Result:
    """Run native code in a child and reap it on every return/error path.

    Timeout includes worker startup and the complete serving session. It raises
    TimeoutError after terminating/reaping the child; this is process cleanup,
    not a claim that native cancellation or C finalizers executed. Callback
    errors stop further delivery and are raised after the active run finishes.
    """
    if role not in {"client", "server"}:
        raise ValueError("role must be client or server")
    for name, callback in (
        ("on_event", on_event),
        ("on_result", on_result),
        ("_on_live_event", _on_live_event),
    ):
        if callback is not None and not callable(callback):
            raise TypeError(f"{name} must be callable")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
    if max_runs is not None and (type(max_runs) is not int or max_runs < 1):
        raise ValueError("max_runs must be a positive integer or None")
    control = _control if _control is not None else _ExecutionControl()
    control.check()
    if should_stop is not None and should_stop():
        return Result(
            ok=False,
            error="Server stopped before accepting a test",
            reporting_role=role,
            execution=ExecutionMetadata(status="incomplete"),
        )
    request = {
        "type": "request",
        "role": role,
        "options": options,
        "password": password,
        "events": on_event is not None,
        "live_events": _on_live_event is not None,
        "max_runs": max_runs,
    }
    session = _ipc.Session.new()
    environment = os.environ.copy()
    # Preserve the calling interpreter's import resolution, including editable
    # source checkouts, without requiring multiprocessing's __main__ guard.
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
    started = time.monotonic()
    messages = _Inbox()
    closing = threading.Event()

    def expire() -> None:
        control.abort(TimeoutError(f"Native {role} exceeded the {timeout:g}s process deadline"))

    def check() -> None:
        if timeout is not None and time.monotonic() - started >= timeout:
            expire()
        control.check()

    watchdog = None
    if timeout is not None:
        watchdog = threading.Timer(max(0, timeout - (time.monotonic() - started)), expire)
        watchdog.daemon = True

    def read_messages() -> None:
        failure: Exception | None = None
        try:
            # Nonblocking descriptor reads keep shutdown independent of an
            # inherited pipe writer. Closing a TextIOWrapper while another
            # thread holds its read lock can otherwise block indefinitely.
            decoder = _ipc.FrameDecoder()
            while not closing.is_set():
                if (
                    responses.terminal_at is not None
                    and time.monotonic() - responses.terminal_at > 5
                ):
                    raise _ipc.IPCError(
                        "Native worker did not close output after its terminal receipt"
                    )
                try:
                    chunk = os.read(stdout.fileno(), 65536)
                except BlockingIOError:
                    closing.wait(0.02)
                    continue
                if not chunk:
                    decoder.eof()
                    if responses.phase != "terminal":
                        # Native parsers can exit directly before sending any
                        # receipt. Allow a short natural-exit observation to
                        # retain that status; an open-ended wait would delay
                        # containment when a live child only closes its pipe.
                        control.wait_for_exit(timeout=0.25)
                        raise _ipc.IPCError(
                            "Native worker ended without completing its response "
                            f"(exit {process.returncode})"
                        )
                    if not control.wait_for_exit(timeout=5):
                        raise _ipc.IPCError(
                            "Native worker did not exit after completing its response"
                        )
                    break
                if responses.phase == "terminal":
                    raise _ipc.IPCError("Native worker sent bytes after its terminal receipt")
                for item, size in decoder.feed_sized(chunk):
                    responses.accept(item)
                    messages.put(item, size)
                # Even an incomplete extra header/body after terminal is invalid.
                if responses.phase == "terminal":
                    decoder.eof()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # wait_for_exit checks the existing cancellation owner. Its
                # BaseException must reach the caller, not escape this thread.
                control.abort(exc)
            failure = (
                exc
                if isinstance(exc, IperfLibraryError)
                else _ipc.IPCError(f"Native worker transport failed: {exc}")
            )
            # Invalid transport must stop native work even while application
            # code blocks the invoking thread inside an already admitted callback.
            control.abort(failure)
        finally:
            messages.finish(failure)

    def write_message(message: dict[str, Any], *, run_index: int = 1) -> None:
        data = memoryview(session.emit(message, run_index=run_index))
        while data:
            check()
            try:
                written = os.write(stdin_descriptor, data)
            except BlockingIOError:
                closing.wait(0.02)
                continue
            if written == 0:
                raise BrokenPipeError("Native worker stopped accepting input")
            data = data[written:]

    reader = threading.Thread(target=read_messages, daemon=True)
    callback_error: Exception | None = None
    worker_error: Exception | None = None
    last_result: Result | None = None
    count = 0
    delivery_sequence = projection_dropped = projection_malformed = 0
    projector = LiveProjector(role)

    def deliver_live(event: LiveEvent) -> None:
        nonlocal callback_error
        if _on_live_event is not None and callback_error is None:
            control.check()
            try:
                _on_live_event(event)
            except Exception as exc:
                callback_error = exc

    def state_event(state: Literal["starting", "ready"], run_index: int = 1) -> LiveEvent:
        nonlocal delivery_sequence
        delivery_sequence += 1
        return LiveEvent(
            "worker_state",
            WorkerStatePayload(state, role),
            session.request_id,
            session.worker_id,
            run_index,
            delivery_sequence,
        )

    deliver_live(state_event("starting"))
    if callback_error is not None:
        raise callback_error
    process = subprocess.Popen(
        _worker_command(),
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
    responses = _Responses(session, role, process.pid, live_events=_on_live_event is not None)
    stdout = process.stdout
    try:
        control.register(process)
        stdin_descriptor = process.stdin.fileno()
        os.set_blocking(stdin_descriptor, False)
        os.set_blocking(stdout.fileno(), False)
        if watchdog is not None:
            assert timeout is not None
            watchdog.interval = max(0, timeout - (time.monotonic() - started))
            watchdog.start()
        reader.start()
        check()
        try:
            write_message(request)
        except BrokenPipeError as exc:
            check()
            raise IperfLibraryError("Native worker closed its input during startup") from exc
        while True:
            check()
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            check()
            kind = message.get("type")
            if kind == "ready":
                deliver_live(state_event("ready"))
                continue
            if kind == "event":
                if on_event is not None and callback_error is None:
                    control.check()
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
            elif kind == "live_event":
                if _on_live_event is not None and callback_error is None:
                    delivery_sequence += 1
                    try:
                        event = projector.project(message, delivery_sequence=delivery_sequence)
                    except _ipc.IPCError:
                        projection_dropped += 1
                    else:
                        if event.kind == "malformed" and message["kind"] != "malformed":
                            projection_malformed += 1
                        deliver_live(event)
            elif kind == "result":
                last_result = _decode_result(message, role)
                last_result.extensions["iperf3_lib.worker"] = {
                    "protocol_version": _ipc.PROTOCOL_VERSION,
                    "request_id": message["request_id"],
                    "worker_id": message["worker_id"],
                    "pid": process.pid,
                    "run_index": message["run_index"],
                    "producer": json.loads(json.dumps(responses.producer)),
                }
                if role == "server":
                    last_result.extensions["iperf3_lib.server_config"] = json.loads(
                        json.dumps(options)
                    )
                delivery = last_result.extensions["iperf3_lib.event_delivery"]
                assert isinstance(delivery, dict)
                delivery["dropped"] = (
                    int(message.get("events_dropped", 0)) + message["_parent_dropped"]
                )
                if _on_live_event is not None:
                    capture = message["capture"]
                    parent_dropped = message["_parent_live_dropped"]
                    last_result.extensions["iperf3_lib.live_delivery"] = {
                        "emitted": capture["live_emitted"],
                        "worker_dropped": capture["live_dropped"],
                        "parent_dropped": parent_dropped,
                        "projection_dropped": projection_dropped,
                        "projection_malformed": projection_malformed,
                        "queue_capacity": _ipc.MAX_PENDING_EVENTS,
                        "queue_bytes": _ipc.MAX_PENDING_EVENT_BYTES,
                        "event_bytes": _ipc.MAX_EVENT_BYTES,
                    }
                    identity = LiveEvent(
                        "worker_state",
                        WorkerStatePayload("ready", role),
                        session.request_id,
                        session.worker_id,
                        message["run_index"],
                    )
                    for stage, dropped in (
                        ("worker_delivery", capture["live_dropped"]),
                        ("parent_delivery", parent_dropped),
                        ("projection", projection_dropped),
                    ):
                        if dropped:
                            delivery_sequence += 1
                            deliver_live(
                                make_delivery_gap_event(
                                    stage=stage,
                                    dropped=dropped,
                                    delivery_sequence=delivery_sequence,
                                    identity=identity,
                                )
                            )
                    projection_dropped = 0
                    if projection_malformed:
                        last_result.diagnostics.append(
                            Diagnostic(
                                "Some live native measurements could not be projected; malformed event diagnostics retain their capture provenance.",
                                "warning",
                                code="execution.malformed_live_event",
                                evidence_paths=[
                                    "/extensions/iperf3_lib.live_delivery/projection_malformed"
                                ],
                            )
                        )
                    projection_malformed = 0
                count += 1
                native_completed = last_result.ok
                if on_result is not None and callback_error is None:
                    control.check()
                    try:
                        on_result(last_result)
                    except Exception as exc:
                        callback_error = exc
                if role == "server":
                    check()
                    keep_running = (
                        callback_error is None
                        and native_completed
                        and (max_runs is None or count < max_runs)
                        and (should_stop is None or not should_stop())
                    )
                    control.check()
                    run_index = responses.continue_run(keep_running)
                    if keep_running:
                        projector = LiveProjector(role)
                    write_message(
                        {"type": "continue", "continue": keep_running}, run_index=run_index
                    )
            elif kind == "error":
                error_type = {
                    "TypeError": TypeError,
                    "ValueError": ValueError,
                    "IperfError": IperfError,
                    "IperfLibraryError": IperfLibraryError,
                    "UnsupportedFeatureError": UnsupportedFeatureError,
                    "FileNotFoundError": FileNotFoundError,
                }.get(message.get("class"), IperfLibraryError)
                worker_error = error_type(message["message"])
            elif kind == "terminal":
                # The reader requires clean EOF and rejects all trailing bytes.
                continue
            elif kind == "eof":
                if not control.wait_for_exit(timeout=5):
                    raise IperfLibraryError(
                        "Native worker did not exit after completing its response"
                    )
                check()
                if worker_error is not None:
                    raise worker_error
                if process.returncode:
                    raise IperfLibraryError(f"Native worker exited with code {process.returncode}")
                if callback_error is not None:
                    raise callback_error
                if last_result is None:
                    raise IperfLibraryError("Native worker returned no result")
                control.complete()
                return last_result
    except BaseException as exc:
        if timeout is not None and time.monotonic() - started >= timeout:
            expire()
        control.abort(exc)
        control.check()
        raise
    finally:
        closing.set()
        if watchdog is not None:
            watchdog.cancel()
        try:
            control.close()
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            if reader.ident is not None:
                reader.join(timeout=1)
            try:
                stdout.close()
            except OSError:
                pass
            if watchdog is not None and watchdog.ident is not None:
                watchdog.join(timeout=1)
