"""Strict, versioned result artifacts independent of native library availability.

Version 1 stores normalized values as recorded. Loading never reparses ``raw``.
Unknown canonical fields are rejected; namespaced extensions and native raw JSON
are the explicit escape hatches. ``Result.to_dict()`` remains an unversioned view.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from importlib.metadata import version
from typing import Any, Never

from ._evidence import has_observed_evidence
from .config import LEGACY_CONFIG_FIELDS, ClientConfig, config_from_dict
from .result import (
    ConfigurationSnapshot,
    Diagnostic,
    EndpointCpuEvidence,
    EndStats,
    ExecutionMetadata,
    FieldAvailability,
    FlowStats,
    IntervalStats,
    JSONValue,
    Result,
    RunTiming,
    StreamStats,
    SumStats,
    TcpIntervalEvidence,
    TcpSummaryEvidence,
    VerifiedSetting,
)


class ArtifactValidationError(ValueError):
    """An artifact contains invalid shape, values, or contradictory metadata."""

    def __init__(self, path: str, message: str) -> None:
        """Keep the JSON Pointer separately from the readable error message."""
        self.path = path
        super().__init__(f"{path or '/'}: {message}")


class UnsupportedArtifactVersion(ArtifactValidationError):  # noqa: N818 - public schema API name
    """The artifact requires a schema version this reader does not understand."""


@dataclass
class ArtifactProducer:
    """Identity of the writer that originally produced an artifact."""

    name: str
    version: str


@dataclass
class ResultArtifact:
    """A result and its versioned serialization envelope."""

    result: Result
    producer: ArtifactProducer
    schema_version: int = 1
    kind: str = "iperf3-lib.result"
    extensions: dict[str, JSONValue] = field(default_factory=dict)


_DIRECTIONS = ("client_to_server", "server_to_client", "unknown")
_CONFIG_NAMES = frozenset(item.name for item in fields(ClientConfig))
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+")
_CONFIG_BOUNDS = {
    "port": (1, 65535),
    "duration": (0, 86400),
    "parallel": (1, 128),
    "omit": (0, 600),
    "blksize": (1, 1024 * 1024),
    "rate": (0, 2**64 - 1),
    "tos": (0, 255),
}


def _optional(spec: Any) -> tuple[str, Any]:
    return ("optional", spec)


def _array(spec: Any) -> tuple[str, Any]:
    return ("array", spec)


def _mapping(spec: Any) -> tuple[str, Any]:
    return ("mapping", spec)


def _enum(*values: str) -> tuple[str, tuple[str, ...]]:
    return ("enum", values)


_MEASUREMENTS = {
    "bits_per_second": _optional("nonnegative_number"),
    "retransmits": _optional("count"),
    "lost_percent": _optional("percent"),
    "jitter_ms": _optional("nonnegative_number"),
    "bytes": _optional("count"),
    "duration_seconds": _optional("nonnegative_number"),
    "start_seconds": _optional("number"),
    "end_seconds": _optional("number"),
    "packets": _optional("count"),
    "lost_packets": _optional("count"),
    "omitted": _optional("boolean"),
}
_SCHEMAS: dict[type, dict[str, Any]] = {
    ArtifactProducer: {"name": "nonempty_string", "version": "nonempty_string"},
    TcpIntervalEvidence: {
        "smoothed_rtt_seconds": _optional("nonnegative_number"),
        "rtt_variation_seconds": _optional("nonnegative_number"),
        "send_congestion_window_bytes": _optional("count"),
        "advertised_send_window_bytes": _optional("count"),
        "path_mtu_bytes": _optional("count"),
        "evidence_paths": _mapping("pointer"),
    },
    TcpSummaryEvidence: {
        "minimum_sampled_rtt_seconds": _optional("nonnegative_number"),
        "maximum_sampled_rtt_seconds": _optional("nonnegative_number"),
        "native_mean_sampled_rtt_seconds": _optional("nonnegative_number"),
        "maximum_send_congestion_window_bytes": _optional("count"),
        "maximum_advertised_send_window_bytes": _optional("count"),
        "evidence_paths": _mapping("pointer"),
    },
    EndpointCpuEvidence: {
        "endpoint": _enum("client", "server", "unknown"),
        "locality": _enum("local", "remote"),
        "scope": _enum("iperf_process"),
        "total_percent": _optional("nonnegative_number"),
        "user_percent": _optional("nonnegative_number"),
        "system_percent": _optional("nonnegative_number"),
        "evidence_paths": _mapping("pointer"),
    },
    RunTiming: {
        "started_at_seconds": _optional("number"),
        "completed_at_seconds": _optional("number"),
        "elapsed_seconds": _optional("nonnegative_number"),
        "native_started_at_seconds": _optional("number"),
        "requested_duration_seconds": _optional("nonnegative_number"),
        "estimated_completed_at_seconds": _optional("number"),
    },
    VerifiedSetting: {
        "value": "json",
        "state": _enum("verified", "unavailable", "unsupported"),
        "evidence_paths": _array("pointer"),
    },
    ConfigurationSnapshot: {
        "requested": _optional("json_object"),
        "effective": _mapping(VerifiedSetting),
    },
    ExecutionMetadata: {
        "status": _enum("completed", "failed", "incomplete"),
        "method": _enum("forward", "reverse", "bidirectional", "unknown"),
        "timing": RunTiming,
        "configuration": ConfigurationSnapshot,
        "native_version": _optional("nonempty_string"),
        "native_system_info": _optional("string"),
        "python_version": _optional("nonempty_string"),
        "platform": _optional("string"),
    },
    FieldAvailability: {
        "state": _enum("absent", "unsupported", "malformed", "unknown"),
        "evidence_paths": _array("pointer"),
    },
    SumStats: {
        **_MEASUREMENTS,
        "direction": _optional(_enum(*_DIRECTIONS)),
        "observation": _optional(_enum("sender", "receiver")),
        "tcp": _optional(TcpSummaryEvidence),
    },
    FlowStats: {
        "direction": _enum(*_DIRECTIONS),
        "sender": _optional(SumStats),
        "receiver": _optional(SumStats),
    },
    StreamStats: {
        "direction": _enum(*_DIRECTIONS),
        "stream_id": _optional("count"),
        "sender": _optional(SumStats),
        "receiver": _optional(SumStats),
        "unattributed": _optional(SumStats),
    },
    IntervalStats: {
        **_MEASUREMENTS,
        "direction": _enum(*_DIRECTIONS),
        "observation": _optional(_enum("sender", "receiver")),
        "stream_id": _optional("count"),
        "scope": _enum("aggregate", "stream", "unknown"),
        "tcp": _optional(TcpIntervalEvidence),
    },
    EndStats: {"sum_sent": _optional(SumStats), "sum_received": _optional(SumStats)},
    Diagnostic: {
        "message": "string",
        "severity": _enum("info", "warning", "error"),
        "code": "nonempty_string",
        "path": _optional("pointer"),
        "evidence_paths": _array("pointer"),
    },
    Result: {
        "ok": "boolean",
        "error": _optional("string"),
        "raw": "json_object",
        "end": _optional(EndStats),
        "protocol": _optional(_enum("tcp", "udp", "sctp")),
        "bidirectional": "boolean",
        "flows": _array(FlowStats),
        "intervals": _array(IntervalStats),
        "diagnostics": _array(Diagnostic),
        "started_at_seconds": _optional("number"),
        "duration_seconds": _optional("nonnegative_number"),
        "completed_at_seconds": _optional("number"),
        "reporting_role": _optional(_enum("client", "server")),
        "execution": _optional(ExecutionMetadata),
        "streams": _array(StreamStats),
        "availability": _mapping(FieldAvailability),
        "extensions": "extensions",
        "cpu": _array(EndpointCpuEvidence),
    },
    ResultArtifact: {
        "result": Result,
        "producer": ArtifactProducer,
        "schema_version": "integer",
        "kind": "string",
        "extensions": "extensions",
    },
}


def _child(path: str, key: str | int) -> str:
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _fail(path: str, message: str) -> Never:
    raise ArtifactValidationError(path, message)


def _pointer(value: str, path: str) -> list[str]:
    if value and not value.startswith("/"):
        _fail(path, "expected a JSON Pointer starting with '/' or the empty root pointer")
    if re.search(r"~(?:[^01]|$)", value):
        _fail(path, "invalid JSON Pointer escape; use ~0 for '~' and ~1 for '/'")
    return [part.replace("~1", "/").replace("~0", "~") for part in value.split("/")[1:]]


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return _fail(path, "expected an object")
    if any(not isinstance(key, str) for key in value):
        return _fail(path, "object keys must be strings")
    return value


def _json_value(value: Any, path: str, active: frozenset[int] = frozenset()) -> JSONValue:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            return _fail(path, "JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if id(value) in active:
            _fail(path, "cyclic data is not JSON")
        return {
            key: _json_value(item, _child(path, key), active | {id(value)})
            for key, item in _object(value, path).items()
        }
    if type(value) is list:
        if id(value) in active:
            _fail(path, "cyclic data is not JSON")
        return [
            _json_value(item, _child(path, index), active | {id(value)})
            for index, item in enumerate(value)
        ]
    return _fail(path, "expected a strict JSON value")


def _convert(spec: Any, value: Any, path: str, *, models: bool = False) -> Any:
    if isinstance(spec, tuple):
        tag, item_spec = spec
        if tag == "optional":
            return None if value is None else _convert(item_spec, value, path, models=models)
        if tag == "enum":
            if type(value) is not str or value not in item_spec:
                return _fail(path, f"expected one of {', '.join(item_spec)}")
            return value
        if tag == "array":
            if type(value) is not list:
                return _fail(path, "expected an array")
            return [
                _convert(item_spec, item, _child(path, index), models=models)
                for index, item in enumerate(value)
            ]
        return {
            key: _convert(item_spec, item, _child(path, key), models=models)
            for key, item in _object(value, path).items()
        }
    if isinstance(spec, type):
        schema = _SCHEMAS[spec]
        if models:
            if type(value) is not spec:
                return _fail(path, f"expected a {spec.__name__} dataclass")
            data = {item.name: getattr(value, item.name) for item in fields(spec)}
        else:
            data = _object(value, path)
        unknown = data.keys() - schema.keys()
        missing = schema.keys() - data.keys()
        if unknown:
            return _fail(_child(path, sorted(unknown)[0]), "unknown canonical field")
        if missing:
            return _fail(_child(path, sorted(missing)[0]), "required canonical field is missing")
        converted = {
            name: _convert(item_spec, data[name], _child(path, name), models=models)
            for name, item_spec in schema.items()
        }
        return converted if models else spec(**converted)
    if spec in ("json", "json_object", "extensions"):
        if spec != "json":
            _object(value, path)
        if spec == "extensions":
            for key in value:
                if not _NAMESPACE.fullmatch(key):
                    _fail(_child(path, key), "extension keys must have a dotted namespace")
        return _json_value(value, path)
    if spec in ("string", "nonempty_string", "pointer"):
        if type(value) is not str:
            return _fail(path, "expected a string")
        if spec == "nonempty_string" and not value.strip():
            return _fail(path, "expected a nonempty string")
        if spec == "pointer":
            _pointer(value, path)
        return value
    if spec == "boolean":
        if type(value) is not bool:
            return _fail(path, "expected a boolean")
        return value
    if type(value) not in (int, float) or (spec in {"count", "integer"} and type(value) is not int):
        return _fail(
            path, "expected an integer" if spec in {"count", "integer"} else "expected a number"
        )
    # Python integers are arbitrary precision and already finite; float conversion can overflow.
    if type(value) is float and not math.isfinite(value):
        return _fail(path, "number must be finite")
    if spec not in {"number", "integer"} and value < 0:
        return _fail(path, "number must be nonnegative")
    if spec == "percent" and value > 100:
        return _fail(path, "percentage must not exceed 100")
    return value


def _related_diagnostic(result: Result, path: str, evidence: list[str]) -> bool:
    return any(
        diagnostic.path == path or bool(set(diagnostic.evidence_paths).intersection(evidence))
        for diagnostic in result.diagnostics
    )


def _available_target(result: Result, pointer: str, path: str) -> Any:
    tokens = _pointer(pointer, path)
    value: Any = result
    if not tokens:
        return _fail(path, "availability must identify a canonical field")
    for token in tokens:
        if type(value) in _SCHEMAS:
            if token not in _SCHEMAS[type(value)] or token in {"raw", "extensions", "requested"}:
                return _fail(path, "availability must identify a canonical field")
            value = getattr(value, token)
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            if index >= len(value):
                return _fail(path, "availability array index is out of range")
            value = value[index]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            return _fail(path, "availability must identify an existing canonical field")
    return value


def _validate_measurements(value: SumStats | IntervalStats, path: str) -> None:
    if (
        value.start_seconds is not None
        and value.end_seconds is not None
        and value.end_seconds < value.start_seconds
    ):
        _fail(_child(path, "end_seconds"), "relative end precedes relative start")


def _has_observed_evidence(result: Result, pointers: list[str]) -> bool:
    try:
        return has_observed_evidence(result, pointers)
    except ValueError as exc:
        return _fail("", str(exc))


def _validate_observations(value: FlowStats | StreamStats, path: str) -> None:
    names = (
        ("sender", "receiver", "unattributed")
        if isinstance(value, StreamStats)
        else ("sender", "receiver")
    )
    for name in names:
        stats = getattr(value, name)
        if stats is None:
            continue
        stats_path = _child(path, name)
        _validate_measurements(stats, stats_path)
        if stats.direction not in (None, value.direction):
            _fail(_child(stats_path, "direction"), "summary direction conflicts with its container")
        expected = None if name == "unattributed" else name
        if stats.observation is not None and stats.observation != expected:
            _fail(
                _child(stats_path, "observation"),
                "summary observation conflicts with its container",
            )


def _validate_configuration(result: Result, execution: ExecutionMetadata) -> None:
    config = execution.configuration
    path = "/result/execution/configuration"
    if config.requested is not None:
        unknown = config.requested.keys() - _CONFIG_NAMES
        missing = LEGACY_CONFIG_FIELDS - config.requested.keys()
        if unknown or missing:
            key = sorted(unknown or missing)[0]
            _fail(
                _child(f"{path}/requested", key),
                "requested configuration must contain all v1 ClientConfig fields",
            )
        for key, value in config.requested.items():
            setting_path = _child(f"{path}/requested", key)
            if key not in LEGACY_CONFIG_FIELDS:
                continue
            if value is None and key in {"blksize", "rate", "tos", "duration"}:
                continue
            if key == "server":
                _convert("string", value, setting_path)
            elif key == "protocol":
                _convert(_enum("tcp", "udp", "sctp"), value, setting_path)
            elif key in {"reverse", "bidirectional", "mptcp", "json_stream"}:
                _convert("boolean", value, setting_path)
            else:
                count = _convert("count", value, setting_path)
                minimum, maximum = _CONFIG_BOUNDS[key]
                if not minimum <= count <= maximum:
                    _fail(setting_path, f"expected a value between {minimum} and {maximum}")
        if config.requested["reverse"] and config.requested["bidirectional"]:
            _fail(f"{path}/requested", "reverse and bidirectional cannot both be enabled")
        block_size = config.requested["blksize"]
        if config.requested["protocol"] == "udp" and block_size is not None:
            block_count = _convert("count", block_size, f"{path}/requested/blksize")
            if not 16 <= block_count <= 65507:
                _fail(f"{path}/requested/blksize", "UDP block size must be between 16 and 65507")
        try:
            config_from_dict(config.requested)
        except (ValueError, TypeError) as exc:
            _fail(f"{path}/requested", str(exc))
    for key, setting in config.effective.items():
        setting_path = _child(f"{path}/effective", key)
        if key not in _CONFIG_NAMES:
            _fail(setting_path, "unknown v1 configuration field")
        if setting.state != "verified" and setting.value is not None:
            _fail(
                f"{setting_path}/value", "unavailable or unsupported settings must have null value"
            )
        if setting.state in {"verified", "unsupported"} and not setting.evidence_paths:
            _fail(
                f"{setting_path}/evidence_paths",
                "verified and unsupported settings require evidence",
            )
        if setting.state == "verified" and setting.value is None:
            _fail(f"{setting_path}/value", "verified settings must contain an observed value")
        if setting.state == "verified":
            spec = (
                "json"
                if key not in LEGACY_CONFIG_FIELDS
                else "string"
                if key == "server"
                else _enum("tcp", "udp", "sctp")
                if key == "protocol"
                else "boolean"
                if key in {"reverse", "bidirectional", "mptcp", "json_stream"}
                else "count"
            )
            if spec == "json":
                _extended_setting(key, setting.value, f"{setting_path}/value")
            else:
                _convert(spec, setting.value, f"{setting_path}/value")
            if not _has_observed_evidence(result, setting.evidence_paths):
                _fail(
                    f"{setting_path}/evidence_paths",
                    "verified settings require existing native raw or extension receipt evidence",
                )
        if (
            setting.state == "verified"
            and config.requested is not None
            and config.requested.get(key) is not None
            and setting.value != config.requested.get(key)
            and not _related_diagnostic(
                result, setting_path.removeprefix("/result"), setting.evidence_paths
            )
        ):
            _fail(setting_path, "effective/requested disagreement requires a diagnostic")


def _extended_setting(name: str, value: JSONValue, path: str) -> None:
    """Validate observed setting types without imposing request-only bounds."""
    if name in {
        "no_delay",
        "zerocopy",
        "skip_rx_copy",
        "udp_counters_64bit",
        "dont_fragment",
        "repeating_payload",
        "get_server_output",
        "gsro",
        "use_pkcs1_padding",
    }:
        _convert("boolean", value, path)
    elif name in {
        "bind_address",
        "bind_device",
        "congestion_control",
        "payload_file",
        "title",
        "extra_data",
        "username",
        "rsa_public_key_path",
    }:
        _convert("string", value, path)
    elif name == "address_family":
        _convert(_enum("auto", "ipv4", "ipv6"), value, path)
    elif name == "interval_seconds":
        _convert("number", value, path)
    elif name == "sctp_bind_addresses":
        _convert(_array("string"), value, path)
    elif name == "control_keepalive":
        items = _convert(_array("integer"), value, path)
        if len(items) != 3:
            _fail(path, "expected three keepalive values")
    else:
        _convert("integer", value, path)


def _validate_result(result: Result) -> None:
    execution = result.execution
    if execution is None:
        _fail(
            "/result/execution",
            "canonical artifacts require execution metadata; use artifact_from_result",
        )
    if result.ok != (execution.status == "completed"):
        _fail("/result/ok", "ok conflicts with execution status")
    if execution.status == "completed" and result.error is not None:
        _fail("/result/error", "completed execution cannot have an error")
    if execution.status == "failed" and result.error is None:
        _fail("/result/error", "failed execution requires an explicit error")
    if execution.method != "unknown" and result.bidirectional != (
        execution.method == "bidirectional"
    ):
        _fail("/result/bidirectional", "bidirectional flag conflicts with execution method")
    if result.completed_at_seconds != execution.timing.completed_at_seconds:
        _fail(
            "/result/completed_at_seconds",
            "compatibility completion must match observed operation completion",
        )
    if result.end is not None:
        if not result.flows or result.end != EndStats(
            result.flows[0].sender, result.flows[0].receiver
        ):
            _fail("/result/end", "end must equal the primary flow compatibility projection")
    directions: set[str] = set()
    expected_direction = {"forward": "client_to_server", "reverse": "server_to_client"}.get(
        execution.method
    )
    for index, flow in enumerate(result.flows):
        path = f"/result/flows/{index}"
        if flow.direction in directions:
            _fail(f"{path}/direction", "duplicate flow direction")
        directions.add(flow.direction)
        _validate_observations(flow, path)
    sockets: set[int] = set()
    for index, stream in enumerate(result.streams):
        path = f"/result/streams/{index}"
        if stream.stream_id is not None:
            if stream.stream_id in sockets:
                _fail(f"{path}/stream_id", "duplicate local stream identifier")
            sockets.add(stream.stream_id)
        _validate_observations(stream, path)
        if stream.unattributed is not None and not any(
            diagnostic.path is not None and diagnostic.path.startswith(f"/streams/{index}")
            for diagnostic in result.diagnostics
        ):
            _fail(f"{path}/unattributed", "unattributed stream data requires a diagnostic")
    for index, interval in enumerate(result.intervals):
        path = f"/result/intervals/{index}"
        _validate_measurements(interval, path)
        if interval.scope == "aggregate" and interval.stream_id is not None:
            _fail(f"{path}/stream_id", "aggregate intervals cannot identify a component stream")
    if expected_direction is not None:
        for name in ("flows", "streams", "intervals"):
            for index, value in enumerate(getattr(result, name)):
                if value.direction not in ("unknown", expected_direction):
                    _fail(
                        f"/result/{name}/{index}/direction",
                        "direction conflicts with execution method",
                    )
    observed_directions = {
        value.direction
        for value in [*result.flows, *result.streams, *result.intervals]
        if value.direction != "unknown"
    }
    if not result.bidirectional and len(observed_directions) > 1:
        _fail(
            "/result/bidirectional",
            "measurements contain both directions but bidirectional is false",
        )
    stream_directions = {
        stream.stream_id: stream.direction
        for stream in result.streams
        if stream.stream_id is not None
    }
    for index, interval in enumerate(result.intervals):
        direction = stream_directions.get(interval.stream_id, "unknown")
        if (
            interval.scope == "stream"
            and direction != "unknown"
            and interval.direction not in ("unknown", direction)
        ):
            _fail(
                f"/result/intervals/{index}/direction",
                "interval direction conflicts with its local stream",
            )

    def validate_evidence(item, path):
        names = _SCHEMAS[type(item)].keys() - {"evidence_paths", "endpoint", "locality", "scope"}
        if item.evidence_paths.keys() - names:
            _fail(f"{path}/evidence_paths", "evidence map contains an unknown measurement name")
        for name in names:
            value = getattr(item, name)
            pointer = item.evidence_paths.get(name)
            if value is not None and (
                pointer is None or not _has_observed_evidence(result, [pointer])
            ):
                _fail(
                    f"{path}/{name}",
                    "present native evidence requires an existing raw or extension receipt",
                )
            if value is None and pointer is not None:
                _fail(f"{path}/evidence_paths/{name}", "absent evidence belongs in availability")

    for group in ("flows", "streams"):
        for index, item in enumerate(getattr(result, group)):
            for observer in ("sender", "receiver", "unattributed"):
                stats = getattr(item, observer, None)
                if stats is None or stats.tcp is None:
                    continue
                path = f"/result/{group}/{index}/{observer}/tcp"
                if result.protocol != "tcp" or group != "streams" or observer != "sender":
                    _fail(path, "TCP summary evidence belongs to a TCP stream sender")
                validate_evidence(stats.tcp, path)
    for index, interval in enumerate(result.intervals):
        if interval.tcp is not None:
            path = f"/result/intervals/{index}/tcp"
            if (
                result.protocol != "tcp"
                or interval.scope != "stream"
                or interval.observation != "sender"
            ):
                _fail(path, "TCP interval evidence belongs to a TCP stream sender")
            validate_evidence(interval.tcp, path)
    localities = set()
    for index, cpu in enumerate(result.cpu):
        path = f"/result/cpu/{index}"
        if cpu.locality in localities:
            _fail(path, "duplicate CPU locality")
        localities.add(cpu.locality)
        expected = (
            result.reporting_role
            if cpu.locality == "local"
            else "server"
            if result.reporting_role == "client"
            else "client"
            if result.reporting_role == "server"
            else None
        )
        if cpu.endpoint != "unknown" and cpu.endpoint != expected:
            _fail(f"{path}/endpoint", "CPU endpoint conflicts with reporting role and locality")
        validate_evidence(cpu, path)
    _validate_configuration(result, execution)
    for pointer, availability in result.availability.items():
        path = _child("/result/availability", pointer)
        if _available_target(result, pointer, path) is not None:
            _fail(path, "non-present availability cannot describe a present value")
        if availability.state != "absent" and (
            not availability.evidence_paths
            or not _related_diagnostic(result, pointer, availability.evidence_paths)
        ):
            _fail(path, "non-absent availability requires evidence and a related diagnostic")


def _validate_envelope(artifact: ResultArtifact) -> None:
    if artifact.schema_version != 1:
        raise UnsupportedArtifactVersion(
            "/schema_version",
            f"unsupported artifact version {artifact.schema_version}; upgrade the reader",
        )
    if artifact.kind != "iperf3-lib.result":
        _fail("/kind", "expected 'iperf3-lib.result'")
    _validate_result(artifact.result)


def _add_compatibility(result: Result) -> None:
    if result.execution is None:
        status = (
            "completed" if result.ok else "failed" if result.error is not None else "incomplete"
        )
        result.execution = ExecutionMetadata(
            status=status,
            timing=RunTiming(
                native_started_at_seconds=result.started_at_seconds,
                requested_duration_seconds=result.duration_seconds,
                estimated_completed_at_seconds=result.completed_at_seconds,
            ),
        )
        result.diagnostics.append(
            Diagnostic(
                "Execution status was inferred from an unversioned result; operation timing and configuration were not recorded.",
                "warning",
                "compatibility.inferred_status",
                "/execution/status",
                ["/ok", "/error"],
            )
        )
        if result.completed_at_seconds is not None:
            result.diagnostics.append(
                Diagnostic(
                    "Legacy completion has no observation evidence and is retained only as an estimate.",
                    "warning",
                    "compatibility.unverified_completion",
                    "/execution/timing/estimated_completed_at_seconds",
                    ["/completed_at_seconds"],
                )
            )
            result.completed_at_seconds = None
    if result.end is not None and not result.flows:
        result.flows.append(FlowStats("unknown", result.end.sum_sent, result.end.sum_received))
        # Existing endpoint directions are evidence and must not be erased or contradicted.
        directions = {
            part.direction
            for part in (result.end.sum_sent, result.end.sum_received)
            if part is not None and part.direction is not None
        }
        if len(directions) == 1:
            result.flows[0].direction = directions.pop()
        result.diagnostics.append(
            Diagnostic(
                "The primary flow was reconstructed from the legacy end projection; absent direction remains unknown.",
                "warning",
                "compatibility.inferred_flow",
                "/flows/0",
                ["/end"],
            )
        )


def artifact_from_result(result: Result) -> ResultArtifact:
    """Snapshot a result, adding explicit compatibility metadata when necessary.

    The returned artifact is detached from caller-owned mutable data. Legacy
    completion values without execution metadata become estimates, never observed
    operation completion. No native library is loaded or queried.
    """
    snapshot = _convert(Result, _convert(Result, result, "/result", models=True), "/result")
    _add_compatibility(snapshot)
    artifact = ResultArtifact(snapshot, ArtifactProducer("iperf3-lib", version("iperf3-lib")))
    _validate_envelope(artifact)
    return artifact


def artifact_to_dict(artifact: ResultArtifact) -> dict[str, Any]:
    """Validate mutable model instances and return a detached v1 JSON mapping."""
    data = _convert(ResultArtifact, artifact, "", models=True)
    _validate_envelope(_convert(ResultArtifact, data, ""))
    return data


def artifact_from_dict(mapping: Mapping[str, Any]) -> ResultArtifact:
    """Load a complete v1 mapping without native loading or raw JSON reparsing."""
    data = _object(mapping, "")
    if "schema_version" not in data:
        _fail(
            "/schema_version",
            "unversioned snapshot; use artifact_from_legacy_dict for explicit migration",
        )
    schema_version = _convert("integer", data["schema_version"], "/schema_version")
    if schema_version != 1:
        raise UnsupportedArtifactVersion(
            "/schema_version", f"unsupported artifact version {schema_version}; upgrade the reader"
        )
    artifact = _convert(ResultArtifact, data, "")
    _validate_envelope(artifact)
    return artifact


def dumps_artifact(artifact: ResultArtifact, *, indent: int | None = None) -> str:
    """Serialize a validated artifact using deterministic keys and strict JSON."""
    return json.dumps(artifact_to_dict(artifact), indent=indent, sort_keys=True, allow_nan=False)


@dataclass
class _JSONPairs:
    pairs: list[tuple[str, Any]]


def _unpack_json(value: Any, path: str = "") -> Any:
    if isinstance(value, _JSONPairs):
        data: dict[str, Any] = {}
        for key, item in value.pairs:
            item_path = _child(path, key)
            if key in data:
                _fail(item_path, "duplicate JSON object key")
            data[key] = _unpack_json(item, item_path)
        return data
    if isinstance(value, list):
        return [_unpack_json(item, _child(path, index)) for index, item in enumerate(value)]
    return value


def loads_artifact(text: str | bytes) -> ResultArtifact:
    """Decode strict JSON, rejecting duplicate keys and unknown schema versions."""
    if not isinstance(text, (str, bytes)):
        _fail("", "expected JSON text or bytes")

    def invalid_constant(value: str) -> Any:
        return _fail("", f"nonfinite JSON constant {value} is forbidden")

    try:
        data = _unpack_json(
            json.loads(text, object_pairs_hook=_JSONPairs, parse_constant=invalid_constant)
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ArtifactValidationError("", f"invalid JSON: {exc}") from exc
    return artifact_from_dict(data)


_LEGACY_FIELDS: dict[type, set[str]] = {
    Result: {
        "ok",
        "error",
        "raw",
        "end",
        "protocol",
        "bidirectional",
        "flows",
        "intervals",
        "diagnostics",
        "started_at_seconds",
        "duration_seconds",
        "completed_at_seconds",
        "reporting_role",
    },
    EndStats: {"sum_sent", "sum_received"},
    SumStats: {
        "bits_per_second",
        "retransmits",
        "lost_percent",
        "jitter_ms",
        "direction",
        "observation",
    },
    FlowStats: {"direction", "sender", "receiver"},
    IntervalStats: {
        "start_seconds",
        "end_seconds",
        "bits_per_second",
        "direction",
        "observation",
        "stream_id",
    },
    Diagnostic: {"message", "severity"},
}


def _legacy_model(model: type, value: Any, path: str) -> Any:
    data = _object(value, path)
    unknown = data.keys() - _LEGACY_FIELDS[model]
    if unknown:
        _fail(_child(path, sorted(unknown)[0]), "unknown field in the supported legacy snapshot")
    converted: dict[str, Any] = {}
    for name, item in data.items():
        spec = _SCHEMAS[model][name]
        if isinstance(spec, tuple) and spec[0] == "optional" and isinstance(spec[1], type):
            converted[name] = (
                None if item is None else _legacy_model(spec[1], item, _child(path, name))
            )
        elif isinstance(spec, tuple) and spec[0] == "array":
            if type(item) is not list:
                _fail(_child(path, name), "expected an array")
            converted[name] = [
                _legacy_model(spec[1], entry, _child(_child(path, name), index))
                for index, entry in enumerate(item)
            ]
        else:
            converted[name] = _convert(spec, item, _child(path, name))
    try:
        return model(**converted)
    except TypeError as exc:
        raise ArtifactValidationError(path, f"missing required legacy field: {exc}") from exc


def artifact_from_legacy_dict(mapping: Mapping[str, Any]) -> ResultArtifact:
    """Explicitly migrate the known unversioned 0.2 development snapshot shape.

    New fields have unknown/absent defaults. Old intervals keep unknown scope,
    even when a socket is absent. Unverified completion is retained as an estimate.
    Unknown legacy keys are rejected rather than silently discarded.
    """
    result = _legacy_model(Result, mapping, "/result")
    result.diagnostics.append(
        Diagnostic(
            "Imported the known unversioned 0.2 development snapshot; new measurements and interval scope were not recorded.",
            "warning",
            "compatibility.legacy_snapshot",
            "",
            [],
        )
    )
    return artifact_from_result(result)
