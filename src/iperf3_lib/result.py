"""Result and statistics models for iperf3 test runs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Literal

ObservationPoint = Literal["sender", "receiver"]
ReportingRole = Literal["client", "server"]


@dataclass
class FlowStats:
    """Statistics reported for one direction of a test flow."""

    direction: str
    sender: SumStats | None = None
    receiver: SumStats | None = None


@dataclass
class IntervalStats:
    """An interval measurement with its elapsed time and observed rates."""

    start_seconds: float | None
    end_seconds: float | None
    bits_per_second: float | None = None
    direction: str = "unknown"
    observation: ObservationPoint | None = None
    stream_id: int | None = None


@dataclass
class Diagnostic:
    """A native or normalized data quality diagnostic."""

    message: str
    severity: Literal["info", "warning", "error"] = "info"


@dataclass
class SumStats:
    """Summary measurements from one endpoint observation of a traffic flow."""

    bits_per_second: float | None = None
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None
    direction: str | None = None
    observation: ObservationPoint | None = None


@dataclass
class EndStats:
    """Compatibility view of the primary flow's endpoint observations."""

    sum_sent: SumStats | None = None
    sum_received: SumStats | None = None


@dataclass
class Result:
    """Top-level result object for an iperf3 test run."""

    ok: bool
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    end: EndStats | None = None
    protocol: str | None = None
    bidirectional: bool = False
    flows: list[FlowStats] = field(default_factory=list)
    intervals: list[IntervalStats] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    started_at_seconds: float | None = None
    duration_seconds: float | None = None
    completed_at_seconds: float | None = None
    reporting_role: ReportingRole | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized result as JSON-compatible data."""
        from dataclasses import asdict

        return asdict(self)

    @property
    def summary_mbps(self) -> float:
        """Return the first available summary rate, including zero, or 0.0 if absent."""
        summaries = (
            (self.end.sum_sent, self.end.sum_received)
            if self.end is not None
            else tuple(part for flow in self.flows for part in (flow.sender, flow.receiver))
        )
        for part in summaries:
            if part is not None and part.bits_per_second is not None:
                return part.bits_per_second / 1_000_000.0
        return 0.0


def result_from_iperf_json(
    raw: dict[str, Any], *, reporting_role: ReportingRole | None = None
) -> Result:
    """Normalize native output without inventing measurements or endpoint provenance.

    A native error or absence of measured end summaries produces a failed result.
    Partial measurements remain available alongside data-quality diagnostics.
    ``reporting_role`` identifies the endpoint that produced the JSON; native
    connecting/accepted-connection markers provide the same provenance when present.
    """
    if not isinstance(raw, dict):
        raise ValueError("iperf JSON root must be an object")
    if reporting_role is not None and reporting_role not in ("client", "server"):
        raise ValueError("reporting_role must be client, server, or None")
    native_error = raw.get("error")
    if "error" in raw and not isinstance(native_error, str):
        raise ValueError("iperf JSON error value must be a string")
    diagnostics: list[Diagnostic] = []

    def _diagnose(message: str, severity: Literal["info", "warning", "error"] = "warning"):
        diagnostic = Diagnostic(message, severity)
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)

    start = raw.get("start")
    if start is None:
        _diagnose("Native JSON start metadata is missing.")
    start = {} if start is None else start
    if not isinstance(start, dict):
        raise ValueError("iperf JSON start value must be an object")
    test_start = start.get("test_start")
    if test_start is None:
        _diagnose("Native JSON test_start configuration is missing.")
    test_start = {} if test_start is None else test_start
    end = raw.get("end")
    end = {} if end is None else end
    if not isinstance(start, dict) or not isinstance(test_start, dict):
        raise ValueError("iperf JSON start and test_start values must be objects")
    if not isinstance(end, dict):
        raise ValueError("iperf JSON end value must be an object")
    timestamp = start.get("timestamp")
    timestamp = {} if timestamp is None else timestamp
    if not isinstance(timestamp, dict):
        raise ValueError("iperf JSON timestamp value must be an object")

    def _optional_number(data: dict[str, Any], name: str, *, integer: bool = False):
        value = data.get(name)
        if value is None:
            return None
        valid_type = isinstance(value, int) if integer else isinstance(value, Real)
        if isinstance(value, bool) or not valid_type:
            raise ValueError(f"iperf JSON {name} must be numeric")
        if not math.isfinite(value):
            raise ValueError(f"iperf JSON {name} must be finite")
        return value

    def _optional_flag(data: dict[str, Any], name: str) -> bool | None:
        if name not in data:
            return None
        value = data[name]
        if not isinstance(value, (bool, int)):
            raise ValueError(f"iperf JSON {name} flag must be a boolean or integer")
        if value not in (False, True, 0, 1):
            raise ValueError(f"iperf JSON {name} flag must be zero or one")
        return bool(value)

    role_evidence = set()
    if reporting_role is not None:
        role_evidence.add(reporting_role)
    for marker, role in (("connecting_to", "client"), ("accepted_connection", "server")):
        if marker in start:
            if not isinstance(start[marker], dict):
                raise ValueError(f"iperf JSON {marker} value must be an object")
            role_evidence.add(role)
    if len(role_evidence) > 1:
        reporting_role = None
        _diagnose("Conflicting reporting-role provenance; reporting endpoint is unknown.")
    elif role_evidence:
        reporting_role = "client" if "client" in role_evidence else "server"

    native_bidir = _optional_flag(test_start, "bidir")
    alias_bidir = _optional_flag(test_start, "bidirectional")
    if native_bidir is not None and alias_bidir is not None and native_bidir != alias_bidir:
        raise ValueError("iperf JSON bidir and bidirectional flags conflict")
    bidirectional = native_bidir if native_bidir is not None else alias_bidir
    reverse = _optional_flag(test_start, "reverse")
    has_reverse_summaries = any(
        end.get(name) is not None
        for name in ("sum_sent_bidir_reverse", "sum_received_bidir_reverse")
    )
    if bidirectional is False and has_reverse_summaries:
        raise ValueError("iperf JSON bidirectional flag conflicts with reverse-flow end summaries")
    if bidirectional is None:
        bidirectional = has_reverse_summaries
        if bidirectional:
            _diagnose("Bidirectional mode was inferred from native reverse-flow end summaries.")
    if bidirectional and reverse:
        raise ValueError("iperf JSON reverse and bidirectional flags conflict")
    primary_direction = (
        "client_to_server"
        if bidirectional or reverse is False
        else "server_to_client"
        if reverse is True
        else "unknown"
    )
    if primary_direction == "unknown":
        _diagnose("Traffic direction is unknown because native direction flags are missing.")

    has_end_measurements = False

    def _sum(
        data: Any, name: str, direction: str, observation: ObservationPoint
    ) -> SumStats | None:
        nonlocal has_end_measurements
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("iperf JSON summary values must be objects")
        values = {
            key: _optional_number(data, key, integer=key in {"retransmits", "bytes"})
            for key in (
                "bits_per_second",
                "retransmits",
                "lost_percent",
                "jitter_ms",
                "bytes",
                "seconds",
            )
        }
        if any(value is not None for value in values.values()):
            has_end_measurements = True
        if values["bits_per_second"] is None:
            _diagnose(f"Native {name}.bits_per_second is missing.")
        return SumStats(
            bits_per_second=values["bits_per_second"],
            retransmits=values["retransmits"],
            lost_percent=values["lost_percent"],
            jitter_ms=values["jitter_ms"],
            direction=direction,
            observation=observation,
        )

    flow_sources = [(primary_direction, "")]
    if bidirectional:
        flow_sources.append(("server_to_client", "_bidir_reverse"))
    flows = []
    for direction, suffix in flow_sources:
        flow = FlowStats(
            direction=direction,
            sender=_sum(end.get(f"sum_sent{suffix}"), f"end.sum_sent{suffix}", direction, "sender"),
            receiver=_sum(
                end.get(f"sum_received{suffix}"), f"end.sum_received{suffix}", direction, "receiver"
            ),
        )
        flows.append(flow)
        if flow.sender is None or flow.receiver is None:
            _diagnose(f"Native endpoint summaries are incomplete for {direction}.")
    primary = flows[0]
    end_stats = (
        EndStats(sum_sent=primary.sender, sum_received=primary.receiver)
        if primary.sender is not None or primary.receiver is not None
        else None
    )

    intervals: list[IntervalStats] = []
    native_intervals = raw.get("intervals")
    native_intervals = [] if native_intervals is None else native_intervals
    if not isinstance(native_intervals, list):
        raise ValueError("iperf JSON intervals value must be an array")
    native_end_streams = end.get("streams")
    native_end_streams = [] if native_end_streams is None else native_end_streams
    if not isinstance(native_end_streams, list):
        raise ValueError("iperf JSON end streams value must be an array")
    socket_senders: dict[int, set[bool]] = {}

    def _record_socket_sender(data: dict[str, Any]) -> None:
        socket = _optional_number(data, "socket", integer=True)
        sender = _optional_flag(data, "sender")
        if socket is not None and sender is not None:
            socket_senders.setdefault(socket, set()).add(sender)

    for stream in native_end_streams:
        if not isinstance(stream, dict):
            raise ValueError("iperf JSON end stream values must be objects")
        for key in ("sender", "receiver", "udp"):
            endpoint = stream.get(key)
            if endpoint is None:
                continue
            if not isinstance(endpoint, dict):
                raise ValueError("iperf JSON end stream observations must be objects")
            # Nested sender is the local stream role, even inside a receiver observation.
            _record_socket_sender(endpoint)
    for interval in native_intervals:
        if not isinstance(interval, dict):
            raise ValueError("iperf JSON interval values must be objects")
        native_streams = interval.get("streams")
        native_streams = [] if native_streams is None else native_streams
        if not isinstance(native_streams, list):
            raise ValueError("iperf JSON interval streams value must be an array")
        for stream in native_streams:
            if not isinstance(stream, dict):
                raise ValueError("iperf JSON stream interval values must be objects")
            _record_socket_sender(stream)

    def _interval(
        data: dict[str, Any], direction: str, observation, stream_id=None
    ) -> IntervalStats:
        start_seconds = _optional_number(data, "start")
        end_seconds = _optional_number(data, "end")
        bitrate = _optional_number(data, "bits_per_second")
        if start_seconds is None or end_seconds is None:
            _diagnose("Native interval boundaries are missing; elapsed time is unknown.")
        if bitrate is None:
            _diagnose("Native interval throughput is missing.")
        if observation is None:
            _diagnose(
                "Native interval observation is unknown because sender provenance is missing."
            )
        return IntervalStats(start_seconds, end_seconds, bitrate, direction, observation, stream_id)

    for interval in native_intervals:
        summary_keys = (
            "sum",
            "sum_sent",
            "sum_received",
            "sum_bidir_reverse",
            "sum_sent_bidir_reverse",
            "sum_received_bidir_reverse",
        )
        for summary_key in summary_keys:
            sums = interval.get(summary_key)
            if sums is None:
                continue
            if not isinstance(sums, dict):
                raise ValueError("iperf JSON interval summary values must be objects")
            reverse_flow = "bidir_reverse" in summary_key
            if reverse_flow and not bidirectional:
                if native_bidir is False or alias_bidir is False or reverse is True:
                    raise ValueError(
                        "iperf JSON direction flags conflict with reverse-flow intervals"
                    )
                _diagnose(
                    "Native reverse-flow interval exists without bidirectional mode metadata."
                )
            sender = _optional_flag(sums, "sender")
            observation = None
            if summary_key.startswith("sum_sent"):
                observation = "sender"
            elif summary_key.startswith("sum_received"):
                observation = "receiver"
            elif sender is not None:
                observation = "sender" if sender else "receiver"
            intervals.append(
                _interval(
                    sums,
                    "server_to_client" if reverse_flow else primary_direction,
                    observation,
                )
            )
        native_streams = interval.get("streams")
        native_streams = [] if native_streams is None else native_streams
        for stream in native_streams:
            stream_id = _optional_number(stream, "socket", integer=True)
            sender = _optional_flag(stream, "sender")
            senders = socket_senders.get(stream_id, set())
            direction = primary_direction
            if len(senders) > 1:
                sender = None
                direction = "unknown"
                _diagnose(f"Conflicting native sender provenance for socket {stream_id}.")
            else:
                if sender is None and senders:
                    sender = next(iter(senders))
                if bidirectional:
                    if reporting_role is None or sender is None:
                        direction = "unknown"
                        _diagnose(
                            "Bidirectional stream direction is unknown without reporting role "
                            "and local sender provenance."
                        )
                    else:
                        direction = (
                            "client_to_server"
                            if (reporting_role == "client") == sender
                            else "server_to_client"
                        )
                elif (
                    reporting_role is not None
                    and sender is not None
                    and direction != "unknown"
                    and sender
                    != ((reporting_role == "client") == (direction == "client_to_server"))
                ):
                    direction = "unknown"
                    _diagnose(
                        f"Native sender provenance for socket {stream_id} conflicts with test direction."
                    )
            observation = None if sender is None else "sender" if sender else "receiver"
            intervals.append(_interval(stream, direction, observation, stream_id))

    error = native_error
    if native_error is not None:
        _diagnose(native_error or "Native JSON contains an empty error message.", "error")
    elif not has_end_measurements:
        error = "Incomplete native JSON: no end-of-test endpoint measurements."
        _diagnose(error, "error")
    ok = error is None

    protocol = test_start.get("protocol")
    if isinstance(protocol, str):
        protocol = protocol.lower()
    elif protocol is not None:
        raise ValueError("iperf JSON protocol must be a string")
    started_at = _optional_number(timestamp, "timesecs")
    duration = _optional_number(test_start, "duration")
    return Result(
        ok=ok,
        error=error,
        raw=raw,
        end=end_stats,
        protocol=protocol,
        bidirectional=bidirectional,
        flows=flows,
        intervals=intervals,
        diagnostics=diagnostics,
        started_at_seconds=started_at,
        duration_seconds=duration,
        completed_at_seconds=(
            started_at + duration
            if ok and started_at is not None and duration is not None
            else None
        ),
        reporting_role=reporting_role,
    )
