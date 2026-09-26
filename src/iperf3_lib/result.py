"""Result and statistics models for iperf3 test runs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Literal

ObservationPoint = Literal["sender", "receiver"]


@dataclass
class FlowStats:
    """Statistics reported for one direction of a test flow."""

    direction: str
    sender: SumStats | None = None
    receiver: SumStats | None = None


@dataclass
class IntervalStats:
    """An interval measurement with its elapsed time and observed rates."""

    start_seconds: float
    end_seconds: float
    bits_per_second: float | None = None
    direction: str = "client_to_server"
    observation: ObservationPoint = "receiver"
    stream_id: int | None = None


@dataclass
class Diagnostic:
    """A native or normalized data quality diagnostic."""

    message: str
    severity: Literal["info", "warning", "error"] = "info"


@dataclass
class SumStats:
    """Summary statistics for a test direction (sent/received)."""

    bits_per_second: float = 0
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None
    direction: str | None = None
    observation: ObservationPoint | None = None


@dataclass
class EndStats:
    """End-of-test statistics for both directions."""

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

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized result as JSON-compatible data."""
        from dataclasses import asdict

        return asdict(self)

    @property
    def summary_mbps(self) -> float:
        """Return the summary throughput in Mbps, or 0.0 if unavailable."""
        summaries = (
            (self.end.sum_sent, self.end.sum_received)
            if self.end is not None
            else tuple(part for flow in self.flows for part in (flow.sender, flow.receiver))
        )
        for part in summaries:
            if part and part.bits_per_second:
                return part.bits_per_second / 1_000_000.0
        return 0.0


def result_from_iperf_json(raw: dict[str, Any]) -> Result:
    """Normalize the stable summary/interval parts of iperf3 JSON output."""
    if not isinstance(raw, dict):
        raise ValueError("iperf JSON root must be an object")
    start = raw.get("start")
    start = {} if start is None else start
    if not isinstance(start, dict):
        raise ValueError("iperf JSON start value must be an object")
    test_start = start.get("test_start")
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

    def _sum(data: Any) -> SumStats | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("iperf JSON summary values must be objects")
        return SumStats(
            bits_per_second=_optional_number(data, "bits_per_second") or 0.0,
            retransmits=_optional_number(data, "retransmits", integer=True),
            lost_percent=_optional_number(data, "lost_percent"),
            jitter_ms=_optional_number(data, "jitter_ms"),
        )

    end_stats = EndStats(
        sum_sent=_sum(end.get("sum_sent")),
        sum_received=_sum(end.get("sum_received")),
    )
    if end_stats.sum_sent is None and end_stats.sum_received is None:
        end_stats = None

    bidirectional = test_start.get("bidirectional", False)
    reverse = test_start.get("reverse", False)
    if not isinstance(bidirectional, (bool, int)) or not isinstance(reverse, (bool, int)):
        raise ValueError("iperf JSON direction flags must be booleans or integers")
    if bidirectional not in (False, True, 0, 1) or reverse not in (False, True, 0, 1):
        raise ValueError("iperf JSON direction flags must be zero or one")
    bidirectional = bool(bidirectional)
    reverse = bool(reverse)
    directions = (
        ["client_to_server"] if not bidirectional else ["client_to_server", "server_to_client"]
    )
    flows = [
        FlowStats(
            direction=direction,
            sender=(
                _sum(end.get("sum_sent_bidir_reverse"))
                if direction == "server_to_client"
                else _sum(end.get("sum_sent"))
            ),
            receiver=(
                _sum(end.get("sum_received_bidir_reverse"))
                if direction == "server_to_client"
                else _sum(end.get("sum_received"))
            ),
        )
        for direction in directions
    ]
    for flow in flows:
        if flow.sender is not None:
            flow.sender.direction = flow.direction
            flow.sender.observation = "sender"
        if flow.receiver is not None:
            flow.receiver.direction = flow.direction
            flow.receiver.observation = "receiver"
    # iperf3 reverse mode swaps endpoint roles for this single flow.
    if reverse and not bidirectional:
        flows[0].direction = "server_to_client"

    intervals: list[IntervalStats] = []
    native_intervals = raw.get("intervals")
    native_intervals = [] if native_intervals is None else native_intervals
    if not isinstance(native_intervals, list):
        raise ValueError("iperf JSON intervals value must be an array")
    for interval in native_intervals:
        if not isinstance(interval, dict):
            raise ValueError("iperf JSON interval values must be objects")
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
            observation = (
                "sender"
                if summary_key.startswith("sum_sent")
                else "receiver"
                if summary_key.startswith("sum_received")
                else "sender"
                if sums.get("sender", True)
                else "receiver"
            )
            intervals.append(
                IntervalStats(
                    start_seconds=_optional_number(sums, "start") or 0.0,
                    end_seconds=_optional_number(sums, "end") or 0.0,
                    bits_per_second=_optional_number(sums, "bits_per_second"),
                    direction="server_to_client" if reverse_flow else flows[0].direction,
                    observation=observation,
                )
            )
        native_streams = interval.get("streams")
        native_streams = [] if native_streams is None else native_streams
        if not isinstance(native_streams, list):
            raise ValueError("iperf JSON interval streams value must be an array")
        for stream in native_streams:
            if not isinstance(stream, dict):
                raise ValueError("iperf JSON stream interval values must be objects")
            stream_id = stream.get("socket")
            if stream_id is not None and (
                isinstance(stream_id, bool) or not isinstance(stream_id, int)
            ):
                raise ValueError("iperf JSON stream socket must be an integer")
            intervals.append(
                IntervalStats(
                    start_seconds=_optional_number(stream, "start") or 0.0,
                    end_seconds=_optional_number(stream, "end") or 0.0,
                    bits_per_second=_optional_number(stream, "bits_per_second"),
                    direction=flows[0].direction,
                    observation="sender" if stream.get("sender", True) else "receiver",
                    stream_id=stream_id,
                )
            )

    protocol = test_start.get("protocol")
    if isinstance(protocol, str):
        protocol = protocol.lower()
    elif protocol is not None:
        raise ValueError("iperf JSON protocol must be a string")
    started_at = _optional_number(timestamp, "timesecs")
    duration = _optional_number(test_start, "duration")
    return Result(
        ok=True,
        raw=raw,
        end=end_stats,
        protocol=protocol,
        bidirectional=bidirectional,
        flows=flows,
        intervals=intervals,
        started_at_seconds=started_at,
        duration_seconds=duration,
        completed_at_seconds=(
            started_at + duration if started_at is not None and duration is not None else None
        ),
    )
