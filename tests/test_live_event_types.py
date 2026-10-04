"""Canonical typed projection, bounded evidence and legacy archive compatibility."""

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path

import pytest

from iperf3_lib import _ipc
from iperf3_lib._cancellation import _ExecutionControl
from iperf3_lib._live_event_types import (
    LiveProjector,
    clone_live_event,
    event_identity,
    live_event_size,
    make_delivery_gap_event,
    make_terminal_event,
)
from iperf3_lib.events import (
    DeliveryGapPayload,
    IntervalPayload,
    LiveEvent,
    MalformedPayload,
    Measurement,
    NativeDocumentPayload,
    NativeEndPayload,
    NativeErrorPayload,
    NativeEvent,
    NativeStartPayload,
    ServerOutputPayload,
    UnknownPayload,
)
from iperf3_lib.exceptions import IperfCleanupError
from iperf3_lib.result import ExecutionMetadata, Result, result_from_iperf_json

FIXTURES = Path(__file__).parent / "fixtures" / "native"
NATIVE = sorted(FIXTURES.glob("*/*.json"))


def frame(kind, data, sequence=1):
    """Build validated-frame-shaped input with separated arrival and measurement time."""
    return {
        "type": "live_event",
        "kind": kind,
        "data": copy.deepcopy(data),
        "request_id": "a" * 32,
        "worker_id": "b" * 32,
        "run_index": 1,
        "capture_sequence": sequence,
        "arrival_offset_seconds": 2.5,
        "time": 1_900_000_000.0,
    }


def scalar_measurement(value):
    """Exclude fragment-relative diagnostic paths while retaining all TCP values."""
    result = asdict(value)
    if result.get("tcp"):
        result["tcp"].pop("evidence_paths", None)
    return result


@pytest.mark.parametrize("path", NATIVE, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_native_fixture_projection_reuses_canonical_measurements(path):
    """Every archived endpoint/protocol/method keeps canonical association and nulls."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    role = "server" if path.stem.endswith("server") else "client"
    canonical = result_from_iperf_json(raw, reporting_role=role)
    projector = LiveProjector(role)
    start = projector.project(frame("native_start", raw["start"]), delivery_sequence=1)
    assert isinstance(start.payload, NativeStartPayload)
    assert start.payload.protocol == canonical.protocol
    assert start.payload.method == canonical.execution.method
    intervals = []
    for index, native in enumerate(raw.get("intervals", []), 2):
        event = projector.project(frame("interval", native, index), delivery_sequence=index)
        assert isinstance(event.payload, IntervalPayload)
        assert "native.incomplete" not in event.payload.diagnostics
        intervals.extend(event.payload.measurements)
        assert event.arrival_offset_seconds == 2.5
        assert event.received_at_seconds == 1_900_000_000.0
    assert [scalar_measurement(v) for v in intervals] == [
        scalar_measurement(Measurement(**asdict(v))) for v in canonical.intervals
    ]
    end = projector.project(frame("native_end", raw.get("end", {}), 100), delivery_sequence=100)
    document = projector.project(frame("native_document", raw, 101), delivery_sequence=101)
    assert isinstance(end.payload, NativeEndPayload)
    assert isinstance(document.payload, NativeDocumentPayload)
    assert end.payload.measurements == document.payload.measurements
    assert document.payload.raw == raw
    assert document.payload.intervals == tuple(
        Measurement(**asdict(v)) for v in canonical.intervals
    )
    assert all(
        v.start_seconds != document.arrival_offset_seconds for v in document.payload.intervals
    )


def test_native_event_dataclass_remains_exact_legacy_archive_shape():
    """Legacy report codecs may continue introspecting exactly the four old fields."""
    assert [v.name for v in fields(NativeEvent)] == [
        "kind",
        "data",
        "sequence",
        "received_at_seconds",
    ]
    assert not issubclass(LiveEvent, NativeEvent)


@pytest.mark.parametrize(
    ("kind", "data", "payload_type"),
    [
        ("native_error", "connection refused", NativeErrorPayload),
        ("native_error", {"error": "broken"}, NativeErrorPayload),
        (
            "server_output",
            {"native_kind": "server_output_json", "payload": {"end": {}}},
            ServerOutputPayload,
        ),
        ("unknown", {"native_kind": "future", "payload": [1, None]}, UnknownPayload),
        (
            "malformed",
            {"reason": "invalid_native_json", "error_type": "ValueError", "sample": "{"},
            MalformedPayload,
        ),
        ("delivery_gap", {"stage": "capture", "dropped": 3}, DeliveryGapPayload),
    ],
)
def test_advisory_native_kinds_keep_typed_raw_evidence(kind, data, payload_type):
    """Errors, unknowns and native end never become wrapper terminal receipts."""
    event = LiveProjector("client").project(
        frame(kind, data, None if kind == "delivery_gap" else 1), delivery_sequence=7
    )
    assert event.kind == kind and isinstance(event.payload, payload_type)
    assert event.delivery_sequence == 7
    assert event.kind != "terminal"


def test_projection_does_not_invent_direction_from_socket_number():
    """A socket and sender bit alone do not identify traffic direction."""
    event = LiveProjector("client").project(
        frame(
            "interval",
            {
                "streams": [
                    {"socket": 99, "sender": True, "seconds": 1, "bytes": 10, "bits_per_second": 80}
                ]
            },
        ),
        delivery_sequence=1,
    )
    assert isinstance(event.payload, IntervalPayload)
    assert len(event.payload.measurements) == 1
    value = event.payload.measurements[0]
    assert (value.direction, value.observation, value.scope, value.stream_id) == (
        "unknown",
        "sender",
        "stream",
        99,
    )
    assert value.packets is None and value.lost_percent is None


def test_events_detach_raw_start_measurements_and_each_other():
    """Application mutation cannot alter subsequent projections or retained native input."""
    start_data = {"test_start": {"protocol": "UDP", "reverse": 1}}
    projector = LiveProjector("client")
    start = projector.project(frame("native_start", start_data), delivery_sequence=1)
    start.payload.raw["test_start"]["reverse"] = 0
    raw = {"sum": {"seconds": 1, "bytes": 10, "bits_per_second": 80, "sender": False}}
    first = projector.project(frame("interval", raw), delivery_sequence=2)
    cloned = clone_live_event(first, delivery_sequence=8)
    cloned.payload.raw["sum"]["bytes"] = 900
    second = projector.project(frame("interval", raw), delivery_sequence=3)
    assert first.payload.measurements == second.payload.measurements
    assert first.payload.raw["sum"]["bytes"] == raw["sum"]["bytes"] == 10
    assert cloned.delivery_sequence == 8 and first.delivery_sequence == 2
    with pytest.raises(FrozenInstanceError):
        first.delivery_sequence = 9
    identity = event_identity(first)
    assert identity.request_id == first.request_id and isinstance(
        identity.payload, DeliveryGapPayload
    )
    assert live_event_size(identity) < 512


@pytest.mark.parametrize(
    "data", [{"streams": "bad"}, {"streams": [None]}, {"sum": {"bytes": "bad", "seconds": 1}}]
)
def test_invalid_native_measurement_shape_is_advisory(data):
    """Valid JSON with unusable native field shapes cannot abort independent completion."""
    event = LiveProjector("client").project(frame("interval", data), delivery_sequence=1)
    assert event.kind in {"interval", "malformed"}
    if event.kind == "malformed":
        assert event.payload.reason == "invalid_native_shape"
        assert event.capture_sequence == 1


def test_projection_size_is_bounded_after_normalized_expansion():
    """A near-limit wire event can exceed the same bound after canonical projection."""
    native = {"sum": {"bytes": 1, "seconds": 1}, "padding": "x" * (_ipc.MAX_EVENT_BYTES - 450)}
    message = frame("interval", native)
    assert len(_ipc.encode_frame(message)) <= _ipc.MAX_EVENT_BYTES + 4
    with pytest.raises(_ipc.IPCError, match="byte limit"):
        LiveProjector("client").project(message, delivery_sequence=1)


@pytest.mark.parametrize(
    ("error", "outcome", "cleanup"),
    [
        (None, "completed", True),
        (ValueError("bad"), "error", True),
        (asyncio.CancelledError(), "cancelled", True),
        (IperfCleanupError("still active", control=_ExecutionControl()), "error", False),
    ],
)
def test_terminal_describes_settled_operation_separately_from_native_status(
    error, outcome, cleanup
):
    """A failed native Result can complete the wrapper operation without throwing."""
    result = Result(
        ok=False,
        error="native error",
        execution=ExecutionMetadata(status="failed"),
        extensions={
            "iperf3_lib.worker": {"request_id": "a" * 32, "worker_id": "b" * 32, "run_index": 1},
            "iperf3_lib.event_capture": {"callbacks": 3},
            "iperf3_lib.live_delivery": {"parent_dropped": 1},
        },
    )
    terminal = make_terminal_event(result, error, delivery_sequence=8, consumer_dropped=2)
    assert terminal.payload.outcome == outcome
    assert terminal.payload.cleanup_confirmed is cleanup
    assert terminal.payload.result_available and terminal.payload.native_status == "failed"
    assert terminal.payload.consumer_dropped == 2
    assert terminal.request_id == "a" * 32 and terminal.run_index == 1
    terminal.payload.capture["callbacks"] = 999
    assert result.extensions["iperf3_lib.event_capture"]["callbacks"] == 3
    assert terminal.capture_sequence is None and terminal.arrival_offset_seconds is None


def test_synthetic_gap_never_claims_native_arrival_and_validates_count():
    """Consumer loss has no invented callback sequence or native timestamp."""
    gap = make_delivery_gap_event(stage="consumer_bridge", dropped=3, delivery_sequence=8)
    assert gap.capture_sequence is None and gap.received_at_seconds is None
    for count in (True, -1, 0, 1.2):
        with pytest.raises(ValueError):
            make_delivery_gap_event(stage="consumer_bridge", dropped=count, delivery_sequence=8)
