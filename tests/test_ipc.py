"""Bounded frame decoding, strict session identities and queue admission contracts."""

import concurrent.futures
import io
import json
import queue
import struct
import threading

import pytest

from iperf3_lib import _ipc
from iperf3_lib._ipc import BoundedFrameQueue, FrameDecoder, IPCError, Session


def _wire(body: bytes) -> bytes:
    return struct.pack("!I", len(body)) + body


def _message(frame: bytes) -> dict:
    return FrameDecoder().feed(frame)[0]


def test_unicode_round_trip_and_wire_length():
    """The prefix counts UTF-8 bytes and control characters are escaped correctly."""
    message = {"name": "測定🙂", "value": '\\"\n\t\x00' * 5000, "nested": [None, True, 2, 0.5]}
    frame = _ipc.encode_frame(message)
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    assert json.loads(frame[4:]) == message
    assert _message(frame) == message


def test_decoder_accepts_every_split_and_coalesced_frames():
    """Pipe reads may end at any byte, including inside a Unicode code point."""
    first = _ipc.encode_frame({"value": "🙂雪"})
    second = _ipc.encode_frame({"value": 2})
    for split in range(len(first) + 1):
        decoder = FrameDecoder()
        output = decoder.feed(first[:split]) + decoder.feed(first[split:])
        assert output == [{"value": "🙂雪"}]
        decoder.eof()
    decoder = FrameDecoder()
    output = []
    for byte in first:
        output.extend(decoder.feed(bytes([byte])))
    output.extend(decoder.feed(second + first))
    assert output == [{"value": "🙂雪"}, {"value": 2}, {"value": "🙂雪"}]
    decoder.eof()


def test_decoder_sized_frames_count_original_wire_bytes():
    """Queue accounting uses the received representation, including legal whitespace."""
    frame = _wire(b'{ "value" : 1 }')
    assert FrameDecoder().feed_sized(frame) == [({"value": 1}, len(frame))]


@pytest.mark.parametrize(
    "prefix", [b"\x00", b"\x00\x00\x00", struct.pack("!I", 2), _wire(b"{}")[:-1]]
)
def test_decoder_rejects_partial_frame_at_eof(prefix):
    """A partial header or body cannot be mistaken for a successful terminal EOF."""
    decoder = FrameDecoder()
    assert decoder.feed(prefix) == []
    with pytest.raises(IPCError, match="truncated"):
        decoder.eof()
    with pytest.raises(IPCError, match="failed"):
        decoder.eof()
    with pytest.raises(IPCError, match="closed or failed"):
        decoder.feed(_wire(b"{}"))


@pytest.mark.parametrize("length", [0, _ipc.MAX_FRAME_BYTES + 1, (1 << 32) - 1])
def test_decoder_rejects_invalid_advertised_size_without_body(length):
    """Oversize is rejected as soon as the four-byte header arrives."""
    decoder = FrameDecoder()
    with pytest.raises(IPCError, match="invalid byte length"):
        decoder.feed(struct.pack("!I", length))
    with pytest.raises(IPCError, match="failed"):
        decoder.feed(b"anything")


def test_decoder_clean_eof_closes_admission():
    """An empty stream is structurally valid but cannot receive later bytes."""
    decoder = FrameDecoder()
    decoder.eof()
    decoder.eof()
    with pytest.raises(IPCError, match="closed"):
        decoder.feed(b"")


class _ShortReader(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 1))


def test_read_frame_handles_short_reads_without_consuming_next_frame():
    """The blocking worker reader leaves the continuation frame in its input pipe."""
    first = _ipc.encode_frame({"value": 1})
    stream = _ShortReader(first + _ipc.encode_frame({"value": 2}))
    assert _ipc.read_frame(stream) == {"value": 1}
    assert stream.tell() == len(first)
    assert _ipc.read_frame(stream) == {"value": 2}
    assert _ipc.read_frame(stream) is None


@pytest.mark.parametrize("frame", [b"\x00", struct.pack("!I", 2), _wire(b"{}")[:-1]])
def test_read_frame_rejects_truncation(frame):
    """Worker command EOF cannot silently discard an incomplete command."""
    with pytest.raises(IPCError, match="truncated"):
        _ipc.read_frame(io.BytesIO(frame))


def test_read_frame_rejects_oversized_header_before_reading_body():
    """A peer cannot force an oversized allocation using a forged length prefix."""
    stream = io.BytesIO(struct.pack("!I", _ipc.MAX_FRAME_BYTES + 1) + b"unread")
    with pytest.raises(IPCError, match="invalid byte length"):
        _ipc.read_frame(stream)
    assert stream.tell() == 4


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":1,"a":2}',
        b'{"nested":{"a":1,"a":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":1e309}',
        b'{"a":"\xff"}',
        b'{"a":"\\ud800"}',
        b'{"a":"\\udfff"}',
        b"[]",
        b"null",
        b"1",
        b"{}{}",
        b'{"a":',
        b'{"a":' + b"[" * 70 + b"0" + b"]" * 70 + b"}",
    ],
)
def test_decoder_rejects_non_strict_or_ambiguous_json(body):
    """Malformed, ambiguous and nonfinite payloads fail before application delivery."""
    with pytest.raises(IPCError):
        FrameDecoder().feed(_wire(body))
    with pytest.raises(IPCError):
        _ipc.read_frame(io.BytesIO(_wire(body)))


@pytest.mark.parametrize("value", [float("inf"), float("nan"), b"bytes", {1: "key"}, "\ud800"])
def test_encoder_rejects_values_outside_strict_json(value):
    """Encoding cannot smuggle nonfinite numbers, nonstring keys or invalid Unicode."""
    with pytest.raises(IPCError):
        _ipc.encode_frame({"value": value})


def test_encoder_rejects_cycles_and_excess_nesting():
    """Recursive input fails as a transport error rather than exhausting the stack."""
    cycle = []
    cycle.append(cycle)
    with pytest.raises(IPCError, match="circular"):
        _ipc.encode_frame({"value": cycle})
    nested = []
    for _ in range(70):
        nested = [nested]
    with pytest.raises(IPCError, match="nesting"):
        _ipc.encode_frame({"value": nested})


@pytest.mark.parametrize(
    "text", ["ascii", "🙂雪", "\x00" * 10000], ids=["ascii", "unicode", "escaped"]
)
def test_encoder_enforces_exact_serialized_byte_limit(text):
    """ASCII, multibyte and escaped strings all share an exact encoded-byte bound."""
    message = {"value": text}
    size = len(_ipc.encode_frame(message)) - 4
    assert len(_ipc.encode_frame(message, max_bytes=size)) == size + 4
    with pytest.raises(IPCError, match="byte limit"):
        _ipc.encode_frame(message, max_bytes=size - 1)


def test_encoder_escapes_large_string_in_bounded_segments(monkeypatch):
    """A huge escaped token is never passed wholesale to the JSON string encoder."""
    original = _ipc.json.dumps
    largest = 0

    def bounded_dumps(value, **kwargs):
        nonlocal largest
        if isinstance(value, str):
            largest = max(largest, len(value))
        return original(value, **kwargs)

    monkeypatch.setattr(_ipc.json, "dumps", bounded_dumps)
    with pytest.raises(IPCError, match="byte limit"):
        _ipc.encode_frame({"value": "\x00" * 100000}, max_bytes=200000)
    assert largest <= 4096


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, _ipc.MAX_FRAME_BYTES + 1])
def test_encoder_rejects_invalid_caller_limit(limit):
    """Callers cannot disable the protocol frame cap with an invalid override."""
    with pytest.raises(ValueError):
        _ipc.encode_frame({}, max_bytes=limit)


def test_event_frame_limit_applies_to_encode_and_decode():
    """A frame small enough for results can still be too large for native events."""
    message = {"type": "event", "payload": "x" * _ipc.MAX_EVENT_BYTES}
    with pytest.raises(IPCError, match="byte limit"):
        _ipc.encode_frame(message)
    body = json.dumps(message).encode()
    with pytest.raises(IPCError, match="event exceeds"):
        FrameDecoder().feed(_wire(body))
    message["type"] = "result"
    assert _message(_ipc.encode_frame(message)) == message


def test_session_request_and_independent_direction_sequences():
    """The two transport counters are independent from each other and native events."""
    parent = Session.new()
    request = _message(parent.emit({"type": "request", "config": {}}))
    worker = Session.from_request(request)
    assert worker.validate(request) is request
    ready = _message(worker.emit({"type": "ready"}))
    assert parent.validate(ready)["transport_sequence"] == 1
    event = _message(worker.emit({"type": "event", "sequence": 99}))
    assert parent.validate(event)["sequence"] == 99
    command = _message(parent.emit({"type": "continue", "continue": True}, run_index=2))
    assert worker.validate(command)["transport_sequence"] == 2
    assert command["run_index"] == 2


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("protocol_version", 2),
        ("protocol_version", True),
        ("request_id", ""),
        ("request_id", "A" * 32),
        ("worker_id", 1),
        ("run_index", 0),
        ("run_index", True),
        ("transport_sequence", 0),
        ("transport_sequence", True),
        ("transport_sequence", 1 << 63),
        ("type", "unknown"),
        ("type", []),
    ],
)
def test_session_rejects_invalid_envelope_before_consuming_sequence(key, value):
    """A malformed frame cannot move the trusted incoming sequence counter."""
    session = Session.new()
    valid = _message(session.emit({"type": "ready"}))
    invalid = {**valid, key: value}
    with pytest.raises(IPCError):
        session.validate(invalid)
    assert session.validate(valid) is valid


def test_session_rejects_cross_session_replay_gap_and_missing_fields():
    """Identity and contiguous sequence checks precede all application effects."""
    session = Session.new()
    first = _message(session.emit({"type": "ready"}))
    for invalid in (
        {**first, "request_id": "0" * 32},
        {**first, "worker_id": "0" * 32},
        {**first, "transport_sequence": 2},
        {key: value for key, value in first.items() if key != "run_index"},
    ):
        with pytest.raises(IPCError):
            session.validate(invalid)
    session.validate(first)
    with pytest.raises(IPCError, match="replayed"):
        session.validate(first)
    second = _message(session.emit({"type": "terminal"}))
    session.validate(second)


@pytest.mark.parametrize("change", [{"type": "ready"}, {"run_index": 2}, {"transport_sequence": 2}])
def test_session_cannot_adopt_a_noninitial_request(change):
    """Worker identity adoption requires the first request for the first run."""
    initial = _message(Session.new().emit({"type": "request"}))
    with pytest.raises(IPCError, match="initial request"):
        Session.from_request({**initial, **change})


def test_session_failed_admission_does_not_create_transport_gaps():
    """Dropped events and failed controls leave the next transmitted sequence intact."""
    session = Session.new()
    assert session.emit({"type": "event"}, admit=lambda _: False) is None
    with pytest.raises(IPCError):
        session.emit({"type": "event", "value": float("nan")})
    with pytest.raises(IPCError):
        session.emit({"type": "event", "transport_sequence": 20})

    def refused(frame):
        raise IPCError("control queue full")

    with pytest.raises(IPCError, match="control queue full"):
        session.emit({"type": "terminal"}, admit=refused)
    assert _message(session.emit({"type": "terminal"}))["transport_sequence"] == 1


def test_concurrent_event_admission_keeps_transmitted_sequences_contiguous():
    """Native callbacks may race, but admitted frames retain their exact FIFO sequence."""
    session = Session.new()
    frames = []

    def emit(index):
        def admit(frame):
            if index % 3 == 0:
                return False
            frames.append(frame)
            return True

        return session.emit({"type": "event", "sequence": index}, admit=admit)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(emit, range(100)))
    assert len(frames) == sum(frame is not None for frame in results) == 66
    assert [_message(frame)["transport_sequence"] for frame in frames] == list(range(1, 67))


def test_event_count_limit_preserves_control_capacity_and_fifo():
    """Saturation drops only new events; ready/results/terminal remain ordered."""
    pending = BoundedFrameQueue()
    event = _ipc.encode_frame({"type": "event"})
    for _ in range(_ipc.MAX_PENDING_EVENTS):
        assert pending.put(event, event=True)
    assert not pending.put(event, event=True)
    controls = [_ipc.encode_frame({"type": kind}) for kind in ("ready", "result", "terminal")]
    for frame in controls:
        assert pending.put(frame)
    expected = [event] * _ipc.MAX_PENDING_EVENTS + controls
    assert pending.pending_bytes == sum(map(len, expected))
    assert [pending.get(timeout=0) for _ in expected] == expected
    assert pending.pending_bytes == 0
    assert pending.put(event, event=True)


def test_event_byte_limit_releases_capacity_when_dequeued(monkeypatch):
    """Serialized bytes independently constrain event admission below the count cap."""
    pending = BoundedFrameQueue()
    frame = _ipc.encode_frame({"type": "event", "payload": "x" * 100})
    monkeypatch.setattr(_ipc, "MAX_PENDING_EVENT_BYTES", len(frame) * 2)
    assert pending.put(frame, event=True)
    assert pending.put(frame, event=True)
    assert not pending.put(frame, event=True)
    assert pending.get(timeout=0) == frame
    assert pending.put(frame, event=True)


@pytest.mark.parametrize("byte_budget", [False, True])
def test_control_capacity_exhaustion_is_explicit(monkeypatch, byte_budget):
    """Reliable controls never disappear silently when either reserve is exhausted."""
    pending = BoundedFrameQueue()
    frame = _ipc.encode_frame({"type": "result"})
    count = _ipc.MAX_PENDING_CONTROLS
    if byte_budget:
        count = 2
        monkeypatch.setattr(_ipc, "MAX_PENDING_CONTROL_BYTES", count * len(frame))
    for _ in range(count):
        assert pending.put(frame)
    with pytest.raises(IPCError, match="control queue capacity"):
        pending.put(frame)
    pending.get(timeout=0)
    assert pending.put(frame)


def test_queue_close_drains_frames_and_wakes_waiting_reader():
    """Shutdown releases blocked delivery without losing admitted controls."""
    pending = BoundedFrameQueue()
    frame = _ipc.encode_frame({"type": "terminal"})
    pending.put(frame)
    pending.close()
    assert pending.get(timeout=0) == frame
    with pytest.raises(EOFError):
        pending.get(timeout=0)
    with pytest.raises(IPCError, match="closed"):
        pending.put(frame)

    empty = BoundedFrameQueue()
    started = threading.Event()

    def read():
        started.set()
        with pytest.raises(EOFError):
            empty.get(timeout=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        assert started.wait(2)
        empty.close()
        future.result(timeout=2)


@pytest.mark.parametrize("frame", [b"", b"\x00", _wire(b"{}")[:-1], bytearray(_wire(b"{}"))])
def test_queue_rejects_unbounded_or_mutable_frame_admission(frame):
    """Only complete immutable frames can consume a transport queue's byte budget."""
    with pytest.raises(IPCError):
        BoundedFrameQueue().put(frame)


def test_queue_empty_timeout_and_invalid_wait_budget():
    """A caller can poll without blocking and cannot request an unbounded numeric wait."""
    pending = BoundedFrameQueue()
    with pytest.raises(queue.Empty):
        pending.get(timeout=0)
    for value in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            pending.get(timeout=value)
