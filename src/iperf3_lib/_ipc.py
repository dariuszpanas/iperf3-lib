"""Private versioned, byte-bounded transport for one isolated worker session."""

from __future__ import annotations

import json
import math
import queue
import re
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO, overload

from .exceptions import IperfLibraryError

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_PENDING_EVENT_BYTES = 8 * 1024 * 1024
MAX_PENDING_EVENTS = 256
MAX_PENDING_CONTROLS = 4
MAX_PENDING_CONTROL_BYTES = 2 * MAX_FRAME_BYTES + 64 * 1024
_MAX_DEPTH = 64
_MAX_COUNTER = (1 << 63) - 1
_STRING_CHUNK = 4096
_HEADER = struct.Struct("!I")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_TYPES = frozenset(
    {"request", "continue", "ready", "event", "live_event", "result", "error", "terminal"}
)
_METADATA = frozenset(
    {"protocol_version", "request_id", "worker_id", "transport_sequence", "run_index"}
)


class IPCError(IperfLibraryError):
    """A worker frame, session identity or bounded transport contract was invalid."""


def _check_json(value: Any, maximum: int) -> None:
    """Reject cycles/deep trees and lower-bound oversize before JSON serialization."""
    active: set[int] = set()
    minimum = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal minimum
        if depth > _MAX_DEPTH:
            raise IPCError("Worker JSON nesting exceeds the protocol limit")
        kind = type(item)
        if item is None or kind is bool:
            minimum += 4 if item is None or item is True else 5
        elif kind is str:
            minimum += len(item) + 2
            if minimum > maximum:
                raise IPCError("Worker frame exceeds its byte limit")
            if _SURROGATE.search(item) is not None:
                raise IPCError("Worker JSON strings cannot contain unpaired surrogates")
        elif kind is int:
            # Every 4 bits require at least one decimal digit; this avoids
            # converting an arbitrarily large integer merely to reject it.
            minimum += max(1, item.bit_length() // 4)
        elif kind is float:
            if not math.isfinite(item):
                raise IPCError("Worker JSON cannot contain nonfinite numbers")
            minimum += 1
        elif kind in (dict, list, tuple):
            identity = id(item)
            if identity in active:
                raise IPCError("Worker JSON cannot contain circular references")
            minimum += 2 + max(0, len(item) - 1)
            if minimum > maximum:
                raise IPCError("Worker frame exceeds its byte limit")
            active.add(identity)
            try:
                if kind is dict:
                    for key, child in item.items():
                        if type(key) is not str:
                            raise IPCError("Worker JSON object keys must be strings")
                        minimum += 1
                        visit(key, depth + 1)
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            finally:
                active.remove(identity)
        else:
            raise IPCError(f"Unsupported worker JSON value: {kind.__name__}")
        if minimum > maximum:
            raise IPCError("Worker frame exceeds its byte limit")

    visit(value, 0)


def _json_chunks(value: Any) -> Iterator[bytes]:
    """Escape strings in fixed-size pieces rather than one potentially huge chunk."""
    kind = type(value)
    if kind is str:
        yield b'"'
        for start in range(0, len(value), _STRING_CHUNK):
            escaped = json.dumps(value[start : start + _STRING_CHUNK], ensure_ascii=False)
            yield escaped[1:-1].encode("utf-8")
        yield b'"'
    elif kind is dict:
        yield b"{"
        for index, (key, child) in enumerate(value.items()):
            if index:
                yield b","
            yield from _json_chunks(key)
            yield b":"
            yield from _json_chunks(child)
        yield b"}"
    elif kind in (list, tuple):
        yield b"["
        for index, child in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(child)
        yield b"]"
    else:
        yield json.dumps(value, allow_nan=False).encode("ascii")


def encode_frame(message: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    """Encode strict JSON incrementally, refusing to assemble an oversized frame."""
    if type(message) is not dict:
        raise IPCError("Worker messages must be JSON objects")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_FRAME_BYTES:
        raise ValueError("max_bytes must be within the positive protocol frame limit")
    if message.get("type") in {"event", "live_event"}:
        max_bytes = min(max_bytes, MAX_EVENT_BYTES)
    _check_json(message, max_bytes)
    body = bytearray()
    try:
        for encoded in _json_chunks(message):
            if len(body) + len(encoded) > max_bytes:
                raise IPCError("Worker frame exceeds its byte limit")
            body.extend(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IPCError("Worker message cannot be encoded as strict UTF-8 JSON") from exc
    return _HEADER.pack(len(body)) + body


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise IPCError("Worker JSON contains duplicate object keys")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise IPCError(f"Worker JSON contains a nonfinite number: {value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise IPCError("Worker JSON number is outside the finite range")
    return number


def _decode(body: bytes | bytearray) -> dict[str, Any]:
    try:
        message = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
        if type(message) is not dict:
            raise IPCError("Worker messages must be JSON objects")
        if message.get("type") in {"event", "live_event"} and len(body) > MAX_EVENT_BYTES:
            raise IPCError("Worker event exceeds its byte limit")
        _check_json(message, MAX_FRAME_BYTES)
        return message
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IPCError("Worker frame contains invalid UTF-8 JSON") from exc


class FrameDecoder:
    """Decode partial reads while retaining at most one bounded frame body."""

    def __init__(self) -> None:
        self._header = bytearray()
        self._body = bytearray()
        self._length: int | None = None
        self._ended = False
        self._failed = False

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Decode zero or more complete frames from the next descriptor read."""
        return [message for message, _ in self.feed_sized(data)]

    def feed_sized(self, data: bytes) -> list[tuple[dict[str, Any], int]]:
        """Also return each frame's actual wire bytes for parent queue accounting."""
        if self._ended or self._failed:
            raise IPCError("Worker frame decoder is already closed or failed")
        incoming = memoryview(data)
        completed = []
        try:
            while incoming:
                if self._length is None:
                    size = min(_HEADER.size - len(self._header), len(incoming))
                    self._header.extend(incoming[:size])
                    incoming = incoming[size:]
                    if len(self._header) != _HEADER.size:
                        continue
                    self._length = _HEADER.unpack(self._header)[0]
                    if not 0 < self._length <= MAX_FRAME_BYTES:
                        raise IPCError("Worker frame advertises an invalid byte length")
                    self._header.clear()
                size = min(self._length - len(self._body), len(incoming))
                self._body.extend(incoming[:size])
                incoming = incoming[size:]
                if len(self._body) == self._length:
                    completed.append((_decode(self._body), self._length + _HEADER.size))
                    self._body.clear()
                    self._length = None
        except BaseException:
            self._failed = True
            raise
        return completed

    def eof(self) -> None:
        """Validate EOF; a partial header/body is never a clean end of stream."""
        if self._failed:
            raise IPCError("Worker frame decoder failed before EOF")
        self._ended = True
        if self._header or self._length is not None:
            self._failed = True
            raise IPCError("Worker stream ended with a truncated frame")


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one frame from a blocking worker input without overreading commands."""
    header = bytearray()
    while len(header) < _HEADER.size:
        chunk = stream.read(_HEADER.size - len(header))
        if not chunk:
            if not header:
                return None
            raise IPCError("Worker stream ended with a truncated frame header")
        header.extend(chunk)
    length = _HEADER.unpack(header)[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise IPCError("Worker frame advertises an invalid byte length")
    body = bytearray()
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise IPCError("Worker stream ended with a truncated frame body")
        body.extend(chunk)
    return _decode(body)


def _headers(message: dict[str, Any]) -> None:
    if type(message) is not dict or not _METADATA.issubset(message):
        raise IPCError("Worker message is missing its session envelope")
    if (
        type(message["protocol_version"]) is not int
        or message["protocol_version"] != PROTOCOL_VERSION
    ):
        raise IPCError("Worker IPC protocol version is unsupported")
    for name in ("request_id", "worker_id"):
        if type(message[name]) is not str or _NONCE.fullmatch(message[name]) is None:
            raise IPCError(f"Worker {name} must be a 32-character hexadecimal nonce")
    for name in ("transport_sequence", "run_index"):
        if type(message[name]) is not int or not 0 < message[name] <= _MAX_COUNTER:
            raise IPCError(f"Worker {name} must be a positive protocol integer")
    if type(message.get("type")) is not str or message["type"] not in _TYPES:
        raise IPCError("Worker message type is unsupported")


class Session:
    """Validate identities and independent contiguous sequence counters per direction."""

    def __init__(self, request_id: str, worker_id: str) -> None:
        for value in (request_id, worker_id):
            if type(value) is not str or _NONCE.fullmatch(value) is None:
                raise IPCError("Session identities must be 32-character hexadecimal nonces")
        self.request_id = request_id
        self.worker_id = worker_id
        self._outgoing = 0
        self._incoming = 0
        self._lock = threading.Lock()

    @classmethod
    def new(cls) -> Session:
        """Assign fresh parent-generated identities to one disposable worker session."""
        return cls(uuid.uuid4().hex, uuid.uuid4().hex)

    @classmethod
    def from_request(cls, message: dict[str, Any]) -> Session:
        """Learn validated initial identities; validate(request) still consumes sequence one."""
        _headers(message)
        if (
            message["type"] != "request"
            or message["transport_sequence"] != 1
            or message["run_index"] != 1
        ):
            raise IPCError("Worker session must begin with the first run's initial request")
        return cls(message["request_id"], message["worker_id"])

    @overload
    def emit(self, message: dict[str, Any], *, run_index: int = 1, admit: None = None) -> bytes: ...

    @overload
    def emit(
        self,
        message: dict[str, Any],
        *,
        run_index: int = 1,
        admit: Callable[[bytes], bool],
    ) -> bytes | None: ...

    def emit(
        self,
        message: dict[str, Any],
        *,
        run_index: int = 1,
        admit: Callable[[bytes], bool] | None = None,
    ) -> bytes | None:
        """Encode and admit atomically; dropped events never consume a wire sequence."""
        if type(message) is not dict or _METADATA.intersection(message):
            raise IPCError("Application message cannot override session envelope fields")
        with self._lock:
            envelope = {
                **message,
                "protocol_version": PROTOCOL_VERSION,
                "request_id": self.request_id,
                "worker_id": self.worker_id,
                "transport_sequence": self._outgoing + 1,
                "run_index": run_index,
            }
            _headers(envelope)
            frame = encode_frame(envelope)
            if admit is not None and not admit(frame):
                return None
            self._outgoing += 1
            return frame

    def validate(self, message: dict[str, Any]) -> dict[str, Any]:
        """Reject cross-session, replayed or missing frames before application delivery."""
        _headers(message)
        with self._lock:
            if message["request_id"] != self.request_id or message["worker_id"] != self.worker_id:
                raise IPCError("Worker message belongs to another session")
            if message["transport_sequence"] != self._incoming + 1:
                raise IPCError("Worker transport sequence is replayed, missing or out of order")
            self._incoming += 1
        return message


class BoundedFrameQueue:
    """FIFO with independent serialized-byte/count budgets for events and control."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frames: deque[tuple[bytes, bool]] = deque()
        self._event_bytes = self._control_bytes = 0
        self._events = self._controls = 0
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        """Return serialized bytes currently owned by the queue."""
        with self._condition:
            return self._event_bytes + self._control_bytes

    def put(self, frame: bytes, *, event: bool = False) -> bool:
        """Drop saturated events, but fail explicitly if reserved control capacity fills."""
        if type(frame) is not bytes or len(frame) < _HEADER.size:
            raise IPCError("Worker queue requires an immutable encoded frame")
        length = _HEADER.unpack(frame[: _HEADER.size])[0]
        if length != len(frame) - _HEADER.size or not 0 < length <= MAX_FRAME_BYTES:
            raise IPCError("Worker queue frame has an invalid byte length")
        if event and length > MAX_EVENT_BYTES:
            raise IPCError("Worker event exceeds its byte limit")
        with self._condition:
            if self._closed:
                raise IPCError("Worker frame queue is closed")
            if event:
                if (
                    self._events >= MAX_PENDING_EVENTS
                    or self._event_bytes + len(frame) > MAX_PENDING_EVENT_BYTES
                ):
                    return False
                self._events += 1
                self._event_bytes += len(frame)
            else:
                if (
                    self._controls >= MAX_PENDING_CONTROLS
                    or self._control_bytes + len(frame) > MAX_PENDING_CONTROL_BYTES
                ):
                    raise IPCError("Worker control queue capacity was exceeded")
                self._controls += 1
                self._control_bytes += len(frame)
            self._frames.append((frame, event))
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> bytes:
        """Take one frame, raising queue.Empty on timeout or EOFError after close/drain."""
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and nonnegative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._frames:
                if self._closed:
                    raise EOFError("Worker frame queue is drained")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            frame, event = self._frames.popleft()
            if event:
                self._events -= 1
                self._event_bytes -= len(frame)
            else:
                self._controls -= 1
                self._control_bytes -= len(frame)
            return frame

    def close(self) -> None:
        """End admission and wake blocked readers without discarding admitted frames."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
