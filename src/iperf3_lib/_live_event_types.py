"""Detached runtime projections; this is deliberately not an archive codec."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, replace
from typing import Any, Literal

from . import _ipc
from .events import (
    DeliveryGapPayload,
    IntervalPayload,
    LiveEvent,
    LivePayload,
    MalformedPayload,
    Measurement,
    NativeDocumentPayload,
    NativeEndPayload,
    NativeErrorPayload,
    NativeStartPayload,
    ServerOutputPayload,
    TerminalPayload,
    UnknownPayload,
)
from .result import IntervalStats, Result, SumStats, result_from_iperf_json


def clone_live_event(event: LiveEvent, *, delivery_sequence: int | None = None) -> LiveEvent:
    """Detach nested JSON without changing native provenance."""
    cloned = copy.deepcopy(event)
    return (
        cloned
        if delivery_sequence is None
        else replace(cloned, delivery_sequence=delivery_sequence)
    )


def event_identity(event: LiveEvent) -> LiveEvent:
    """Return an internal identity carrier without retaining native payload bytes."""
    return LiveEvent(
        "delivery_gap",
        DeliveryGapPayload("identity", 0),
        event.request_id,
        event.worker_id,
        event.run_index,
        event.delivery_sequence,
    )


def live_event_size(event: LiveEvent) -> int:
    """Bound a projected envelope by strict JSON wire bytes including its header."""
    return len(_ipc.encode_frame({"type": "live_event", "event": asdict(event)}))


def make_delivery_gap_event(
    *,
    stage: str,
    dropped: int,
    delivery_sequence: int,
    identity: LiveEvent | None = None,
) -> LiveEvent:
    """Create a synthetic gap without claiming a native callback or arrival."""
    if type(dropped) is not int or dropped < 1:
        raise ValueError("dropped must be a positive integer")
    return LiveEvent(
        "delivery_gap",
        DeliveryGapPayload(stage, dropped),
        identity.request_id if identity else None,
        identity.worker_id if identity else None,
        identity.run_index if identity else None,
        delivery_sequence,
    )


def make_terminal_event(
    result: Result | None,
    error: BaseException | None,
    *,
    delivery_sequence: int,
    identity: LiveEvent | None = None,
    consumer_dropped: int = 0,
) -> LiveEvent:
    """Describe an already-settled owner; callers must finish cleanup first."""
    from .exceptions import IperfCleanupError

    if type(consumer_dropped) is not int or consumer_dropped < 0:
        raise ValueError("consumer_dropped must be a nonnegative integer")
    outcome = (
        "completed"
        if error is None
        else "cancelled"
        if isinstance(error, asyncio.CancelledError)
        else "error"
    )
    extensions = result.extensions if result is not None else {}
    worker = extensions.get("iperf3_lib.worker")
    if identity is None and isinstance(worker, dict):
        request_id, worker_id, run_index = (
            worker.get(key) for key in ("request_id", "worker_id", "run_index")
        )
        identity = LiveEvent(
            "delivery_gap",
            DeliveryGapPayload("identity", 0),
            request_id if isinstance(request_id, str) else None,
            worker_id if isinstance(worker_id, str) else None,
            run_index if type(run_index) is int else None,
        )
    payload = TerminalPayload(
        outcome=outcome,
        result_available=result is not None,
        native_status=result.execution.status if result and result.execution else None,
        cleanup_confirmed=not isinstance(error, IperfCleanupError),
        consumer_dropped=consumer_dropped,
        capture=copy.deepcopy(extensions.get("iperf3_lib.event_capture")),
        delivery=copy.deepcopy(extensions.get("iperf3_lib.live_delivery")),
        error_type=type(error).__name__ if error is not None else None,
        error_message=str(error)[:1024] if error is not None else None,
    )
    return LiveEvent(
        "terminal",
        payload,
        identity.request_id if identity else None,
        identity.worker_id if identity else None,
        identity.run_index if identity else None,
        delivery_sequence,
    )


def _measurement(value: IntervalStats | SumStats, **identity: Any) -> Measurement:
    fields = asdict(value)
    fields.update(identity)
    if fields.get("direction") is None:
        fields["direction"] = "unknown"
    return Measurement(**fields)


def _summaries(result: Result) -> tuple[Measurement, ...]:
    values = [
        _measurement(value, scope="aggregate", direction=flow.direction)
        for flow in result.flows
        for value in (flow.sender, flow.receiver)
        if value is not None
    ]
    values.extend(
        _measurement(value, scope="stream", direction=stream.direction, stream_id=stream.stream_id)
        for stream in result.streams
        for value in (stream.sender, stream.receiver, stream.unattributed)
        if value is not None
    )
    return tuple(values)


class LiveProjector:
    """Reuse canonical normalization with detached, per-run start provenance."""

    def __init__(self, role: Literal["client", "server"]) -> None:
        self.role = role
        self.start: dict[str, Any] = {}

    def project(self, message: dict[str, Any], *, delivery_sequence: int) -> LiveEvent:
        """Project an already validated bounded worker frame into a typed envelope."""
        try:
            return self._project(message, delivery_sequence=delivery_sequence)
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as error:
            # Strict JSON can still have the wrong native field shapes. Such
            # advisory evidence must not prevent an independent final result.
            return LiveEvent(
                "malformed",
                MalformedPayload("invalid_native_shape", type(error).__name__),
                message["request_id"],
                message["worker_id"],
                message["run_index"],
                delivery_sequence,
                message["capture_sequence"],
                message["arrival_offset_seconds"],
                message["time"],
            )

    def _project(self, message: dict[str, Any], *, delivery_sequence: int) -> LiveEvent:
        kind, data = message["kind"], copy.deepcopy(message["data"])
        payload: LivePayload
        if kind in {"native_start", "interval", "native_end", "native_document"}:
            raw: dict[str, Any] = data if kind == "native_document" else {"start": self.start}
            if kind == "native_start":
                self.start = copy.deepcopy(data)
                raw = {"start": data}
            elif kind == "interval":
                raw["intervals"] = [data]
            elif kind == "native_end":
                raw["end"] = data
            normalized = result_from_iperf_json(raw, reporting_role=self.role)
            diagnostics = tuple(
                d.code for d in normalized.diagnostics if d.code != "native.incomplete"
            )[:8]
            method = normalized.execution.method if normalized.execution else "unknown"
            if kind == "native_start":
                payload = NativeStartPayload(data, normalized.protocol, method, self.role)
            elif kind == "interval":
                payload = IntervalPayload(
                    data, tuple(_measurement(v) for v in normalized.intervals), diagnostics
                )
            elif kind == "native_end":
                payload = NativeEndPayload(data, _summaries(normalized), diagnostics)
            else:
                payload = NativeDocumentPayload(
                    data,
                    normalized.protocol,
                    method,
                    _summaries(normalized),
                    tuple(_measurement(v) for v in normalized.intervals),
                    diagnostics,
                )
        elif kind == "native_error":
            error = (
                data
                if isinstance(data, str)
                else data.get("error")
                if isinstance(data, dict)
                else None
            )
            payload = NativeErrorPayload(data, error if isinstance(error, str) else None)
        elif kind == "server_output":
            payload = ServerOutputPayload(data["native_kind"], data["payload"])
        elif kind == "unknown":
            payload = UnknownPayload(data["native_kind"], data["payload"])
        elif kind == "malformed":
            payload = MalformedPayload(data["reason"], data.get("error_type"), data.get("sample"))
        elif kind == "delivery_gap":
            payload = DeliveryGapPayload(data["stage"], data["dropped"])
        else:
            raise _ipc.IPCError("Unrecognized typed worker event")
        event = LiveEvent(
            kind,
            payload,
            message["request_id"],
            message["worker_id"],
            message["run_index"],
            delivery_sequence,
            message["capture_sequence"],
            message["arrival_offset_seconds"],
            message["time"],
        )
        live_event_size(event)
        return event
