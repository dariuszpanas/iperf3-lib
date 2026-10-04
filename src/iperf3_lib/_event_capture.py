"""Bounded native byte copying and parser-owned streaming reconstruction."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ._ipc import IPCError, _check_json
from .exceptions import IperfLibraryError

MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PENDING_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_PENDING_CAPTURES = 256
MAX_RETAINED_BYTES = 12 * 1024 * 1024
MAX_DIAGNOSTICS = 8


def _native_object(copied: bytes, maximum: int) -> dict[str, Any]:
    repeated_rates: list[dict[str, Any]] = []

    def invalid_constant(value):
        raise ValueError(f"nonfinite native JSON: {value}")

    def object_pairs(pairs):
        result = {}
        repeated_rate = False
        for key, value in pairs:
            if key in result:
                if (
                    key != "target_bitrate"
                    or repeated_rate
                    or type(value) is not int
                    or type(result[key]) is not int
                    or value < 0
                    or value != result[key]
                ):
                    raise ValueError("duplicate native JSON field")
                repeated_rate = True
                repeated_rates.append(result)
            result[key] = value
        return result

    data = json.loads(
        copied.decode("utf-8"), parse_constant=invalid_constant, object_pairs_hook=object_pairs
    )
    if not isinstance(data, dict):
        raise ValueError("native JSON must be an object")
    # libiperf server start output repeats this integer once, identically, in
    # both streamed start.data and a complete document's start object. Keep this
    # observed native quirk separate from strict transport JSON validation.
    wrapped = "event" in data and "data" in data
    start = data.get("data") if wrapped and data.get("event") == "start" else None
    if not wrapped:
        start = data.get("start")
    if any(value is not start for value in repeated_rates):
        raise ValueError("duplicate native JSON field outside native start")
    _check_json(data, maximum)
    return data


def _size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


class BoundedDocumentCapture:
    """Copy bounded document history without decoding in the C callback."""

    def __init__(
        self,
        ffi: Any,
        *,
        limit: int = MAX_CAPTURE_BYTES,
        pending_bytes: int = MAX_PENDING_CAPTURE_BYTES,
        pending_items: int = MAX_PENDING_CAPTURES,
    ) -> None:
        """Retain the explicit native copy limit and no native pointer."""
        self.ffi = ffi
        self.limit = limit
        self.pending_limit = pending_bytes
        self.item_limit = pending_items
        self.payload: bytes | None = None
        self.error: str | None = None
        self.native_error: str | None = None
        self._documents: deque[bytes] = deque()
        self._pending_bytes = 0

    def read(self, pointer: Any) -> bytes | None:
        """Copy a getter value with the same bound as callback capture."""
        if pointer == self.ffi.NULL:
            return None
        copied = self.ffi.string(pointer, self.limit + 1)
        if len(copied) > self.limit:
            raise IperfLibraryError("Native JSON exceeds the capture byte limit")
        return copied

    def capture(self, _test: Any, pointer: Any) -> None:
        """Contain every native callback failure and retain bounded copied bytes."""
        try:
            copied = self.read(pointer)
            if copied is not None:
                if (
                    len(self._documents) >= self.item_limit
                    or self._pending_bytes + len(copied) > self.pending_limit
                ):
                    self.error = "Native JSON callback history exceeds the capture bound"
                    return
                self._documents.append(copied)
                self._pending_bytes += len(copied)
                self.payload = copied
        except BaseException as exc:
            self.error = f"Cannot capture native JSON: {type(exc).__name__}"

    def parse_document(self, payload: bytes) -> dict[str, Any]:
        """Decode strict bounded native JSON outside the native callback."""
        return _native_object(payload, self.limit)

    def finalize(self) -> None:
        """Latch admitted native errors on the owner thread and release prior bytes."""
        while self._documents:
            copied = self._documents.popleft()
            self._pending_bytes -= len(copied)
            try:
                data = self.parse_document(copied)
            except Exception as exc:
                if self.error is None:
                    self.error = f"Cannot decode native JSON: {type(exc).__name__}"
                continue
            observed_error = data.get("error")
            if self.native_error is None and observed_error:
                text = (
                    observed_error
                    if isinstance(observed_error, str)
                    else json.dumps(observed_error, ensure_ascii=False)
                )
                if text != "no error":
                    self.native_error = text[:4096]


class EventCapture:
    """Own one native run's bounded queue, parser, evidence and delivery counters."""

    def __init__(
        self,
        ffi: Any,
        emit: Callable[[dict[str, Any]], bool],
        *,
        legacy_events: bool = False,
        live_events: bool = False,
        capture_bytes: int = MAX_CAPTURE_BYTES,
        pending_bytes: int = MAX_PENDING_CAPTURE_BYTES,
        pending_items: int = MAX_PENDING_CAPTURES,
        retained_bytes: int = MAX_RETAINED_BYTES,
    ) -> None:
        """Configure bounded capture before installing the native callback."""
        self.ffi = ffi
        self.emit = emit
        self.legacy_events = legacy_events
        self.live_events = live_events
        self.capture_bytes = capture_bytes
        self.pending_limit = pending_bytes
        self.item_limit = pending_items
        self.retained_limit = retained_bytes
        self.raw: dict[str, Any] = {}
        self.native_events: list[dict[str, Any]] = []
        self.events_emitted = self.events_dropped = 0
        self.callbacks = self.copied = self.capture_dropped = self.malformed = 0
        self.retention_dropped = self.live_emitted = self.live_dropped = 0
        self.complete_document = False
        self.complete_document_source: str | None = None
        self.getter_error: str | None = None
        self.native_error: str | None = None
        self._complete_sequence = 0
        self._last_capture_loss = 0
        self.diagnostics: list[str] = []
        self._retained_bytes = 0
        self._pending_bytes = 0
        self._queue: deque[tuple[bytes, int, float, float]] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._failure: BaseException | None = None
        self._started = time.monotonic()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the parser before admitting native callback bytes."""
        self._thread = threading.Thread(target=self._parse, name="iperf-event-parser", daemon=True)
        self._thread.start()

    def capture(self, _test: Any, pointer: Any) -> None:
        """Copy bounded bytes and metadata; never parse or encode inside C."""
        try:
            with self._condition:
                self.callbacks += 1
                sequence = self.callbacks
            offset, arrived = time.monotonic() - self._started, time.time()
            if pointer == self.ffi.NULL:
                raise ValueError("null native payload")
            copied = self.ffi.string(pointer, self.capture_bytes + 1)
            if len(copied) > self.capture_bytes:
                raise ValueError("native payload exceeds capture byte limit")
            with self._condition:
                if (
                    self._closed
                    or len(self._queue) >= self.item_limit
                    or self._pending_bytes + len(copied) > self.pending_limit
                ):
                    self.capture_dropped += 1
                    self._last_capture_loss = sequence
                    return
                self._queue.append((copied, sequence, offset, arrived))
                self._pending_bytes += len(copied)
                self.copied += 1
                self._condition.notify()
        except BaseException as exc:
            with self._condition:
                self.capture_dropped += 1
                self._last_capture_loss = self.callbacks
                self._diagnose(f"Cannot copy native event: {type(exc).__name__}")

    def close(self) -> None:
        """Drain and join before the owner may publish result or terminal frames."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise IperfLibraryError("Native event parser did not stop")
        if self._failure is not None:
            raise IperfLibraryError("Native event parser failed") from self._failure

    @property
    def settled(self) -> bool:
        """Whether parser emissions have ended, including startup/failure paths."""
        return self._thread is None or not self._thread.is_alive()

    def metadata(self) -> dict[str, Any]:
        """Return detached counters and limits after parser completion."""
        return {
            "schema_version": 1,
            "callbacks": self.callbacks,
            "copied": self.copied,
            "capture_dropped": self.capture_dropped,
            "malformed": self.malformed,
            "retention_dropped": self.retention_dropped,
            "live_emitted": self.live_emitted,
            "live_dropped": self.live_dropped,
            "complete_document": self.complete_document,
            "complete_document_source": self.complete_document_source,
            "getter_error": self.getter_error,
            "reconstruction_complete": not (
                self.capture_dropped or self.malformed or self.retention_dropped
            ),
            "diagnostics": list(self.diagnostics),
            "limits": {
                "payload_bytes": self.capture_bytes,
                "pending_bytes": self.pending_limit,
                "pending_items": self.item_limit,
                "retained_bytes": self.retained_limit,
                "diagnostics": MAX_DIAGNOSTICS,
            },
        }

    def recover_document(self, payload: bytes | None, error: str | None = None) -> None:
        """Parse an independently bounded getter copy after callback parsing has settled."""
        self.getter_error = error[:256] if error else None
        if payload is None:
            return
        try:
            data = _native_object(payload, self.capture_bytes)
            if "event" in data and "data" in data:
                raise ValueError("getter must contain a complete native document")
            size = _size(data) + 128
            if size > self.retained_limit:
                raise ValueError("getter document exceeds retained evidence byte limit")
        except Exception as exc:
            self.getter_error = f"Cannot decode native getter: {type(exc).__name__}"
            return
        self.raw, self.native_events = data, []
        self._retained_bytes = size
        self.complete_document = True
        self.complete_document_source = "getter"

    def _diagnose(self, message: str) -> None:
        with self._condition:
            if len(self.diagnostics) < MAX_DIAGNOSTICS:
                self.diagnostics.append(message[:256])

    def _deliver(self, message: dict[str, Any]) -> bool:
        try:
            return self.emit(message)
        except IPCError:
            return False

    def _live(
        self,
        kind: str,
        data: Any,
        sequence: int | None,
        offset: float | None,
        arrived: float | None,
    ) -> None:
        if self.live_events:
            self.live_emitted += 1
            if not self._deliver(
                {
                    "type": "live_event",
                    "kind": kind,
                    "data": data,
                    "capture_sequence": sequence,
                    "arrival_offset_seconds": offset,
                    "time": arrived,
                }
            ):
                self.live_dropped += 1

    def _parse(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._closed:
                        self._condition.wait()
                    if not self._queue:
                        break
                    copied, sequence, offset, arrived = self._queue.popleft()
                    self._pending_bytes -= len(copied)
                self._consume(copied, sequence, offset, arrived)
            if self.complete_document and self._last_capture_loss > self._complete_sequence:
                self.complete_document = False
                self.complete_document_source = None
                self._diagnose("Native callback capture failed after the last complete document")
            for stage, dropped in (
                ("capture", self.capture_dropped),
                ("retention", self.retention_dropped),
            ):
                if dropped:
                    self._live(
                        "delivery_gap",
                        {"stage": stage, "dropped": dropped},
                        None,
                        None,
                        None,
                    )
        except BaseException as exc:
            self._failure = exc
            with self._condition:
                self.capture_dropped += len(self._queue)
                self._queue.clear()
                self._pending_bytes = 0
                self._closed = True

    def _consume(self, copied: bytes, sequence: int, offset: float, arrived: float) -> None:
        try:
            data = _native_object(copied, self.capture_bytes)
            wrapped = "event" in data and "data" in data
            if wrapped and not isinstance(data["event"], str):
                raise ValueError("native event kind must be a string")
        except Exception as exc:
            self.malformed += 1
            self.complete_document = False
            self.complete_document_source = None
            self._diagnose(f"Cannot parse native event: {type(exc).__name__}")
            self._live(
                "malformed",
                {
                    "reason": "invalid_native_json",
                    "error_type": type(exc).__name__,
                    "sample": copied[:256].decode("utf-8", errors="replace"),
                },
                sequence,
                offset,
                arrived,
            )
            return
        kind = data["event"] if wrapped else "complete"
        part = data["data"] if wrapped else data
        if wrapped:
            self.events_emitted += 1
            if self.legacy_events and not self._deliver(
                {
                    "type": "event",
                    "kind": kind,
                    "data": part,
                    "sequence": self.events_emitted,
                    "time": arrived,
                }
            ):
                self.events_dropped += 1
        typed_kind = {
            "start": "native_start",
            "interval": "interval",
            "end": "native_end",
            "error": "native_error",
            "complete": "native_document",
        }.get(kind)
        if typed_kind is None:
            typed_kind = (
                "server_output"
                if kind in {"server_output_json", "server_output_text"}
                else "unknown"
            )
            live_data = {"native_kind": kind, "payload": part}
        else:
            live_data = part
        if (kind in {"start", "interval", "end", "complete"} and not isinstance(part, dict)) or (
            kind == "error" and not isinstance(part, (str, dict))
        ):
            self.malformed += 1
            self.complete_document = False
            self.complete_document_source = None
            self._diagnose("Native measurement event requires an object")
            self._live(
                "malformed",
                {
                    "reason": "invalid_native_json",
                    "error_type": "ValueError",
                    "sample": copied[:256].decode("utf-8", errors="replace"),
                },
                sequence,
                offset,
                arrived,
            )
            return
        if self.native_error is None:
            observed_error = (
                part if kind == "error" else part.get("error") if kind == "complete" else None
            )
            if observed_error:
                text = (
                    observed_error
                    if isinstance(observed_error, str)
                    else json.dumps(observed_error, ensure_ascii=False)
                )
                if text != "no error":
                    self.native_error = text[:4096]
        self._live(typed_kind, live_data, sequence, offset, arrived)
        if self.complete_document and wrapped and kind != "complete":
            if kind not in {"start", "interval", "end", "error"}:
                # Advisory future/server-output envelopes cannot rewrite a
                # complete native document or fabricate a protocol failure.
                return
            self.malformed += 1
            self._diagnose("Native event followed a complete native document")
            # The preceding document no longer covers the observed native stream.
            # Only a later independent getter may re-establish completeness.
            self.complete_document = False
            self.complete_document_source = None
        # Charge both reconstruction and original envelopes, including object keys.
        # Complete documents replace the projection and discard duplicated envelopes.
        size = _size(data)
        error_text = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
        raw_cost = _size(error_text) if kind == "error" else _size(part)
        required = (
            size + 128 if kind == "complete" else self._retained_bytes + size + raw_cost + 128
        )
        if required > self.retained_limit:
            self.retention_dropped += 1
            if kind == "complete":
                self.complete_document = False
                self.complete_document_source = None
            self._diagnose("Native reconstruction exceeds retained evidence byte limit")
            return
        self._retained_bytes = required
        if kind == "complete":
            self.raw = part
            self.native_events = []
            self.complete_document = True
            self.complete_document_source = "callback"
            self._complete_sequence = sequence
            return
        if not self.complete_document:
            self.native_events.append(data)
        if kind == "start":
            self.raw["start"] = part
        elif kind == "interval":
            self.raw.setdefault("intervals", []).append(part)
        elif kind == "end":
            self.raw["end"] = part
        elif kind == "error":
            self.raw["error"] = error_text
        elif kind in {"server_output_json", "server_output_text"}:
            self.raw[kind] = part
