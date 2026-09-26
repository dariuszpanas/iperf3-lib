"""Result and statistics models for iperf3 test runs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Literal

ObservationPoint = Literal["sender", "receiver"]
ReportingRole = Literal["client", "server"]
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


@dataclass
class RunTiming:
    """Observed operation timing kept separate from native and inferred timestamps."""

    started_at_seconds: float | None = None
    completed_at_seconds: float | None = None
    elapsed_seconds: float | None = None
    native_started_at_seconds: float | None = None
    requested_duration_seconds: float | None = None
    estimated_completed_at_seconds: float | None = None


@dataclass
class VerifiedSetting:
    """A native setting observation with explicit verification evidence."""

    value: JSONValue = None
    state: Literal["verified", "unavailable", "unsupported"] = "unavailable"
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class ConfigurationSnapshot:
    """Detached requested settings and independently observed effective settings."""

    requested: dict[str, JSONValue] | None = None
    effective: dict[str, VerifiedSetting] = field(default_factory=dict)


@dataclass
class ExecutionMetadata:
    """Execution outcome, method, environment, configuration, and operation timing."""

    status: Literal["completed", "failed", "incomplete"]
    method: Literal["forward", "reverse", "bidirectional", "unknown"] = "unknown"
    timing: RunTiming = field(default_factory=RunTiming)
    configuration: ConfigurationSnapshot = field(default_factory=ConfigurationSnapshot)
    native_version: str | None = None
    native_system_info: str | None = None
    python_version: str | None = None
    platform: str | None = None


@dataclass
class FieldAvailability:
    """Evidence explaining why a canonical measurement is not available."""

    state: Literal["absent", "unsupported", "malformed", "unknown"]
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class StreamStats:
    """Endpoint observations for one local native stream identifier."""

    direction: str = "unknown"
    stream_id: int | None = None
    sender: SumStats | None = None
    receiver: SumStats | None = None
    unattributed: SumStats | None = None


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
    scope: Literal["aggregate", "stream", "unknown"] = "unknown"
    bytes: int | None = None
    duration_seconds: float | None = None
    omitted: bool | None = None
    packets: int | None = None
    lost_packets: int | None = None
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None


@dataclass
class Diagnostic:
    """A native or normalized data quality diagnostic."""

    message: str
    severity: Literal["info", "warning", "error"] = "info"
    code: str = "unspecified"
    path: str | None = None
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class SumStats:
    """Summary measurements from one endpoint observation of a traffic flow."""

    bits_per_second: float | None = None
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None
    direction: str | None = None
    observation: ObservationPoint | None = None
    bytes: int | None = None
    duration_seconds: float | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    packets: int | None = None
    lost_packets: int | None = None
    omitted: bool | None = None


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
    execution: ExecutionMetadata | None = None
    streams: list[StreamStats] = field(default_factory=list)
    availability: dict[str, FieldAvailability] = field(default_factory=dict)
    extensions: dict[str, JSONValue] = field(default_factory=dict)

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
    availability: dict[str, FieldAvailability] = {}

    def _diagnose(
        message: str,
        severity: Literal["info", "warning", "error"] = "warning",
        *,
        code: str = "measurement.absent",
        path: str | None = None,
        evidence_paths: list[str] | None = None,
    ):
        diagnostic = Diagnostic(message, severity, code, path, list(evidence_paths or []))
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

    protocol = test_start.get("protocol")
    if isinstance(protocol, str):
        protocol = protocol.lower()
        if protocol not in ("tcp", "udp", "sctp"):
            raise ValueError("iperf JSON protocol must be tcp, udp, or sctp")
    elif protocol is not None:
        raise ValueError("iperf JSON protocol must be a string")
    for name in ("version", "system_info"):
        if start.get(name) is not None and not isinstance(start[name], str):
            raise ValueError(f"iperf JSON {name} must be a string")

    def _optional_number(data: dict[str, Any], name: str, *, integer: bool = False):
        value = data.get(name)
        if value is None:
            return None
        valid_type = isinstance(value, int) if integer else isinstance(value, Real)
        if isinstance(value, bool) or not valid_type:
            raise ValueError(f"iperf JSON {name} must be numeric")
        if not math.isfinite(value):
            raise ValueError(f"iperf JSON {name} must be finite")
        if name not in {"start", "end"} and value < 0:
            raise ValueError(f"iperf JSON {name} must be non-negative")
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

    def _retransmits(data: dict[str, Any], canonical_path: str, source_path: str):
        value = data.get("retransmits")
        if value is not None and protocol == "sctp":
            # These producers collect retransmissions only for TCP but emit SCTP
            # placeholders, exchanged -1 sentinels, and uninitialized interval data.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("iperf JSON SCTP retransmits must be an integer")
            known_unsupported = start.get("version") in ("iperf 3.19.1", "iperf 3.21")
            state = "unsupported" if known_unsupported else "unknown"
            path = f"{canonical_path}/retransmits"
            evidence = [
                f"{source_path}/retransmits",
                "/raw/start/test_start/protocol",
                "/raw/start/version",
            ]
            availability[path] = FieldAvailability(state, evidence)
            _diagnose(
                "This native producer does not collect SCTP retransmissions; emitted values are unsupported."
                if known_unsupported
                else "SCTP retransmission measurement support is unknown for this native producer.",
                code=f"measurement.{state}",
                path=path,
                evidence_paths=evidence,
            )
            return None
        return _optional_number(data, "retransmits", integer=True)

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
        _diagnose(
            "Conflicting reporting-role provenance; reporting endpoint is unknown.",
            code="provenance.conflict",
            path="/reporting_role",
            evidence_paths=[
                f"/raw/start/{key}"
                for key in ("connecting_to", "accepted_connection")
                if key in start
            ],
        )
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
            _diagnose(
                "Bidirectional mode was inferred from native reverse-flow end summaries.",
                code="provenance.inferred",
                path="/execution/method",
                evidence_paths=["/raw/end"],
            )
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
        _diagnose(
            "Traffic direction is unknown because native direction flags are missing.",
            code="provenance.unknown",
            path="/execution/method",
            evidence_paths=["/raw/start/test_start"],
        )

    has_end_measurements = False

    def _sum(
        data: Any,
        name: str,
        direction: str,
        observation: ObservationPoint | None,
        canonical_path: str,
    ) -> SumStats | None:
        nonlocal has_end_measurements
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("iperf JSON summary values must be objects")
        values = {
            key: _optional_number(data, key, integer=key in {"bytes", "packets", "lost_packets"})
            for key in (
                "bits_per_second",
                "lost_percent",
                "jitter_ms",
                "bytes",
                "seconds",
                "packets",
                "lost_packets",
            )
        }
        values["retransmits"] = _retransmits(data, canonical_path, f"/raw/{name.replace('.', '/')}")
        if any(value is not None for key, value in values.items() if key != "seconds"):
            has_end_measurements = True
        if values["bits_per_second"] is None:
            _diagnose(
                f"Native {name}.bits_per_second is missing.",
                path=f"{canonical_path}/bits_per_second",
                evidence_paths=[f"/raw/{name.replace('.', '/')}/bits_per_second"],
            )
        return SumStats(
            bits_per_second=values["bits_per_second"],
            retransmits=values["retransmits"],
            lost_percent=values["lost_percent"],
            jitter_ms=values["jitter_ms"],
            direction=direction,
            observation=observation,
            bytes=values["bytes"],
            duration_seconds=values["seconds"],
            start_seconds=_optional_number(data, "start"),
            end_seconds=_optional_number(data, "end"),
            packets=values["packets"],
            lost_packets=values["lost_packets"],
            omitted=_optional_flag(data, "omitted"),
        )

    flow_sources = [(primary_direction, "")]
    if bidirectional:
        flow_sources.append(("server_to_client", "_bidir_reverse"))
    flows = []
    for flow_index, (direction, suffix) in enumerate(flow_sources):
        flow = FlowStats(
            direction=direction,
            sender=_sum(
                end.get(f"sum_sent{suffix}"),
                f"end.sum_sent{suffix}",
                direction,
                "sender",
                f"/flows/{flow_index}/sender",
            ),
            receiver=_sum(
                end.get(f"sum_received{suffix}"),
                f"end.sum_received{suffix}",
                direction,
                "receiver",
                f"/flows/{flow_index}/receiver",
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

    def _stream_provenance(data: dict[str, Any], source_path: str):
        stream_id = _optional_number(data, "socket", integer=True)
        sender = _optional_flag(data, "sender")
        senders = socket_senders.get(stream_id, set())
        direction = primary_direction
        if len(senders) > 1:
            sender = None
            direction = "unknown"
            _diagnose(
                f"Conflicting native sender provenance for socket {stream_id}.",
                code="provenance.conflict",
                evidence_paths=[source_path],
            )
        else:
            if sender is None and senders:
                sender = next(iter(senders))
            if bidirectional:
                if reporting_role is None or sender is None:
                    direction = "unknown"
                    _diagnose(
                        "Bidirectional stream direction is unknown without reporting role and local sender provenance.",
                        code="provenance.unknown",
                        evidence_paths=[source_path],
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
                and sender != ((reporting_role == "client") == (direction == "client_to_server"))
            ):
                direction = "unknown"
                _diagnose(
                    f"Native sender provenance for socket {stream_id} conflicts with test direction.",
                    code="provenance.conflict",
                    evidence_paths=[source_path],
                )
        return stream_id, sender, direction

    def _interval(
        data: dict[str, Any],
        direction: str,
        observation,
        stream_id=None,
        *,
        scope: Literal["aggregate", "stream"],
        source_path: str,
    ) -> IntervalStats:
        canonical_path = f"/intervals/{len(intervals)}"
        start_seconds = _optional_number(data, "start")
        end_seconds = _optional_number(data, "end")
        bitrate = _optional_number(data, "bits_per_second")
        if start_seconds is None or end_seconds is None:
            _diagnose(
                "Native interval boundaries are missing; elapsed time is unknown.",
                path=canonical_path,
                evidence_paths=[source_path],
            )
        if bitrate is None:
            _diagnose(
                "Native interval throughput is missing.",
                path=f"{canonical_path}/bits_per_second",
                evidence_paths=[f"{source_path}/bits_per_second"],
            )
        if observation is None:
            _diagnose(
                "Native interval observation is unknown because sender provenance is missing.",
                code="provenance.unknown",
                path=f"{canonical_path}/observation",
                evidence_paths=[source_path],
            )
            availability[f"{canonical_path}/observation"] = FieldAvailability(
                "unknown", [source_path]
            )
        return IntervalStats(
            start_seconds,
            end_seconds,
            bitrate,
            direction,
            observation,
            stream_id,
            scope=scope,
            bytes=_optional_number(data, "bytes", integer=True),
            duration_seconds=_optional_number(data, "seconds"),
            omitted=_optional_flag(data, "omitted"),
            packets=_optional_number(data, "packets", integer=True),
            lost_packets=_optional_number(data, "lost_packets", integer=True),
            retransmits=_retransmits(data, canonical_path, source_path),
            lost_percent=_optional_number(data, "lost_percent"),
            jitter_ms=_optional_number(data, "jitter_ms"),
        )

    for interval_index, interval in enumerate(native_intervals):
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
                    "Native reverse-flow interval exists without bidirectional mode metadata.",
                    code="provenance.unknown",
                    evidence_paths=[f"/raw/intervals/{interval_index}/{summary_key}"],
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
                    scope="aggregate",
                    source_path=f"/raw/intervals/{interval_index}/{summary_key}",
                )
            )
        native_streams = interval.get("streams")
        native_streams = [] if native_streams is None else native_streams
        for stream_index, stream in enumerate(native_streams):
            source_path = f"/raw/intervals/{interval_index}/streams/{stream_index}"
            stream_id, sender, direction = _stream_provenance(stream, source_path)
            observation = None if sender is None else "sender" if sender else "receiver"
            intervals.append(
                _interval(
                    stream,
                    direction,
                    observation,
                    stream_id,
                    scope="stream",
                    source_path=source_path,
                )
            )

    streams: list[StreamStats] = []
    for stream_index, native_stream in enumerate(native_end_streams):
        source_path = f"/raw/end/streams/{stream_index}"
        canonical_path = f"/streams/{stream_index}"
        parts = {
            name: native_stream[name]
            for name in ("sender", "receiver", "udp")
            if native_stream.get(name) is not None
        }
        provenances = [
            _stream_provenance(data, f"{source_path}/{name}") for name, data in parts.items()
        ]
        sockets = {socket for socket, _, _ in provenances if socket is not None}
        directions = {direction for _, _, direction in provenances}
        stream_id = next(iter(sockets)) if len(sockets) == 1 else None
        direction = next(iter(directions)) if len(directions) == 1 else "unknown"
        if len(sockets) > 1 or len(directions) > 1:
            direction = "unknown"
            _diagnose(
                "Native endpoint summaries disagree about stream provenance.",
                code="provenance.conflict",
                path=canonical_path,
                evidence_paths=[source_path],
            )
        stream_result = StreamStats(direction, stream_id)
        for name, data in parts.items():
            observer: ObservationPoint | None = (
                "sender" if name == "sender" else "receiver" if name == "receiver" else None
            )
            field_name = "unattributed" if name == "udp" else name
            stats = _sum(
                data,
                f"end.streams.{stream_index}.{name}",
                direction,
                observer,
                f"{canonical_path}/{field_name}",
            )
            setattr(stream_result, field_name, stats)
            if name == "udp":
                evidence = [f"{source_path}/udp"]
                _diagnose(
                    "Native UDP stream summary combines sender and receiver fields; endpoint attribution is unknown.",
                    code="provenance.mixed_udp_summary",
                    path=f"{canonical_path}/unattributed",
                    evidence_paths=evidence,
                )
                availability[f"{canonical_path}/unattributed/observation"] = FieldAvailability(
                    "unknown", evidence
                )
        streams.append(stream_result)

    error = native_error
    if native_error is not None:
        _diagnose(
            native_error or "Native JSON contains an empty error message.",
            "error",
            code="native.error",
            path="/error",
            evidence_paths=["/raw/error"],
        )
    elif not has_end_measurements:
        error = "Incomplete native JSON: no end-of-test endpoint measurements."
        _diagnose(
            error,
            "error",
            code="native.incomplete",
            path="/execution/status",
            evidence_paths=["/raw/end"],
        )
    ok = error is None

    started_at = _optional_number(timestamp, "timesecs")
    duration = _optional_number(test_start, "duration")
    effective = {
        name: VerifiedSetting()
        for name in (
            "server",
            "port",
            "protocol",
            "duration",
            "parallel",
            "omit",
            "reverse",
            "bidirectional",
            "mptcp",
            "blksize",
            "rate",
            "tos",
            "json_stream",
        )
    }
    for field_name, native_name in (
        ("duration", "duration"),
        ("parallel", "num_streams"),
        ("omit", "omit"),
        ("blksize", "blksize"),
        ("tos", "tos"),
    ):
        value = _optional_number(test_start, native_name, integer=True)
        if value is not None:
            effective[field_name] = VerifiedSetting(
                value, "verified", [f"/raw/start/test_start/{native_name}"]
            )
    if protocol is not None:
        effective["protocol"] = VerifiedSetting(
            protocol, "verified", ["/raw/start/test_start/protocol"]
        )
    if reverse is not None:
        effective["reverse"] = VerifiedSetting(
            reverse, "verified", ["/raw/start/test_start/reverse"]
        )
    if native_bidir is not None or alias_bidir is not None:
        native_name = "bidir" if native_bidir is not None else "bidirectional"
        effective["bidirectional"] = VerifiedSetting(
            bidirectional, "verified", [f"/raw/start/test_start/{native_name}"]
        )
    test_rate = _optional_number(test_start, "target_bitrate", integer=True)
    root_rate = _optional_number(start, "target_bitrate", integer=True)
    if test_rate is not None and root_rate is not None and test_rate != root_rate:
        _diagnose(
            "Native target bitrate values disagree; effective rate is unavailable.",
            code="configuration.conflict",
            path="/execution/configuration/effective/rate",
            evidence_paths=["/raw/start/target_bitrate", "/raw/start/test_start/target_bitrate"],
        )
    elif test_rate is not None or root_rate is not None:
        effective["rate"] = VerifiedSetting(
            test_rate if test_rate is not None else root_rate,
            "verified",
            [
                "/raw/start/test_start/target_bitrate"
                if test_rate is not None
                else "/raw/start/target_bitrate"
            ],
        )
    connecting_to = start.get("connecting_to", {})
    if "host" in connecting_to:
        if not isinstance(connecting_to["host"], str):
            raise ValueError("iperf JSON connecting_to host must be a string")
        effective["server"] = VerifiedSetting(
            connecting_to["host"], "verified", ["/raw/start/connecting_to/host"]
        )
    native_port = _optional_number(connecting_to, "port", integer=True)
    if native_port is not None:
        effective["port"] = VerifiedSetting(
            native_port, "verified", ["/raw/start/connecting_to/port"]
        )
    execution = ExecutionMetadata(
        status="completed" if ok else "failed" if native_error is not None else "incomplete",
        method="bidirectional"
        if bidirectional
        else "reverse"
        if reverse is True
        else "forward"
        if reverse is False
        else "unknown",
        timing=RunTiming(
            native_started_at_seconds=started_at,
            requested_duration_seconds=duration,
            estimated_completed_at_seconds=started_at + duration
            if ok and started_at is not None and duration is not None
            else None,
        ),
        configuration=ConfigurationSnapshot(effective=effective),
        native_version=start.get("version"),
        native_system_info=start.get("system_info"),
    )
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
        completed_at_seconds=None,
        reporting_role=reporting_role,
        execution=execution,
        streams=streams,
        availability=availability,
    )
