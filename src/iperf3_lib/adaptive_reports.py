"""Strict adaptive-UDP v1 archives and human-readable tested-point evidence."""

from __future__ import annotations

import json
import math
import re
import types
from dataclasses import dataclass, field, fields
from importlib.metadata import version
from typing import Any, Literal, Never, Union, get_args, get_origin, get_type_hints

from . import adaptive
from .artifacts import ArtifactProducer
from .reports import (
    ReportValidationError,
    UnsupportedReportVersionError,
    _convert,
    plan_result_from_dict,
    plan_result_to_dict,
)
from .result import JSONValue
from .sweeps import SettingCheck
from .trials import PlanResult

ADAPTIVE_UDP_ALGORITHM: Literal["conservative-tested-rates-v1"] = "conservative-tested-rates-v1"
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+")


@dataclass(frozen=True)
class AdaptiveUDPReport:
    """Retained finite experiment evidence with its original writer and interpretation."""

    result: adaptive.AdaptiveResult
    producer: ArtifactProducer
    kind: Literal["iperf3-lib.adaptive-udp"] = "iperf3-lib.adaptive-udp"
    schema_version: int = 1
    algorithm_revision: Literal["conservative-tested-rates-v1"] = ADAPTIVE_UDP_ALGORITHM
    extensions: dict[str, JSONValue] = field(default_factory=dict)


_MODELS = {
    AdaptiveUDPReport,
    ArtifactProducer,
    adaptive.AdaptiveUDPPolicy,
    adaptive.PreparedAdaptiveUDP,
    adaptive.AdaptiveBatch,
    adaptive.AdaptiveBatchResult,
    adaptive.AdaptiveDecision,
    adaptive.AdaptiveObservation,
    adaptive.AdaptiveRateSummary,
    adaptive.AdaptiveResult,
    SettingCheck,
}


def _fail(path: str, message: str) -> Never:
    raise ReportValidationError(path, message)


def _object(value, path):
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(path, "expected an object with string keys")
    return value


def _strict_json(value, path=""):
    kind = type(value)
    if value is None or kind in {str, bool, int}:
        return
    if kind is float:
        if not math.isfinite(value):
            _fail(path, "expected a finite number")
        return
    if kind is list:
        for index, child in enumerate(value):
            _strict_json(child, f"{path}/{index}")
        return
    if kind is dict:
        _object(value, path)
        for key, child in value.items():
            _strict_json(child, f"{path}/{key}")
        return
    _fail(path, "expected a strict JSON value")


def _json_copy(value):
    try:
        _strict_json(value)
        return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ReportValidationError):
            raise
        _fail("", f"invalid strict JSON or excessive nesting: {exc}")


def _field(spec, value, path, *, encoding):
    if spec is PlanResult:
        try:
            return plan_result_to_dict(value) if encoding else plan_result_from_dict(value)
        except (ValueError, TypeError) as exc:
            _fail(path, f"invalid retained trial execution: {exc}")
    if spec in _MODELS:
        return _model(spec, value, path, encoding=encoding)
    args = get_args(spec)
    origin = get_origin(spec)
    if origin in (types.UnionType, Union):
        if value is None and type(None) in args:
            return None
        for option in args:
            if option is not type(None):
                return _field(option, value, path, encoding=encoding)
    if origin is tuple:
        if type(value) is not (tuple if encoding else list):
            _fail(path, "expected an array")
        if len(args) == 2 and args[1] is Ellipsis:
            specs = [args[0]] * len(value)
        else:
            if len(value) != len(args):
                _fail(path, "array has the wrong length")
            specs = args
        converted = [
            _field(item_spec, item, f"{path}/{index}", encoding=encoding)
            for index, (item_spec, item) in enumerate(zip(specs, value, strict=True))
        ]
        return converted if encoding else tuple(converted)
    # Shared frozen trial/configuration/artifact scalar contracts remain authoritative.
    return _convert(spec, value, path, encoding=encoding)


def _model(model, value, path, *, encoding):
    declared = fields(model)
    if encoding:
        if not isinstance(value, model):
            _fail(path, f"expected {model.__name__}")
        data = {item.name: getattr(value, item.name) for item in declared}
    else:
        data = _object(value, path)
        if set(data) != {item.name for item in declared}:
            _fail(path, "object must contain exactly the declared adaptive-UDP v1 fields")
    hints = get_type_hints(model)
    values = {
        item.name: _field(
            hints[item.name], data[item.name], f"{path}/{item.name}", encoding=encoding
        )
        for item in declared
    }
    if encoding:
        return values
    try:
        return model(**values)
    except (ValueError, TypeError, OverflowError) as exc:
        _fail(path, str(exc))


def _validate_result(result):
    try:
        adaptive._validate_result_v1(result)
    except (ValueError, TypeError, OverflowError, RuntimeError) as exc:
        _fail("/result", f"retained evidence or decisions disagree with frozen v1 rules: {exc}")


def adaptive_udp_report_from_dict(mapping: dict[str, Any]) -> AdaptiveUDPReport:
    """Load frozen v1 evidence without current planning, native work, or raw reparsing."""
    data = _json_copy(mapping)
    _object(data, "")
    if set(data) != {item.name for item in fields(AdaptiveUDPReport)}:
        _fail("", "object must contain exactly the declared adaptive-UDP v1 fields")
    if type(data["schema_version"]) is not int:
        _fail("/schema_version", "expected an integer schema version")
    if data["schema_version"] != 1 or data["algorithm_revision"] != ADAPTIVE_UDP_ALGORITHM:
        raise UnsupportedReportVersionError(
            "/schema_version", "unsupported adaptive-UDP schema or algorithm; upgrade the reader"
        )
    report = _model(AdaptiveUDPReport, data, "", encoding=False)
    if not report.producer.name.strip() or not report.producer.version.strip():
        _fail("/producer", "producer name and version must be nonempty strings")
    if any(not _NAMESPACE.fullmatch(key) for key in report.extensions):
        _fail("/extensions", "extension keys must be namespaced")
    _validate_result(report.result)
    return report


def adaptive_udp_report_to_dict(report: AdaptiveUDPReport) -> dict[str, Any]:
    """Validate and detach retained evidence, decisions, ordering, and producer identity."""
    if not isinstance(report, AdaptiveUDPReport):
        _fail("", "expected an AdaptiveUDPReport")
    # Reject tuple coercion and cycles before the shared JSON-value converter.
    _json_copy(report.extensions)
    data = _json_copy(_model(AdaptiveUDPReport, report, "", encoding=True))
    adaptive_udp_report_from_dict(data)
    return data


def report_from_adaptive_udp(result: adaptive.AdaptiveResult) -> AdaptiveUDPReport:
    """Snapshot an experiment with this writer's identity without running measurements."""
    return adaptive_udp_report_from_dict(
        adaptive_udp_report_to_dict(
            AdaptiveUDPReport(result, ArtifactProducer("iperf3-lib", version("iperf3-lib")))
        )
    )


def dumps_adaptive_udp_report(report: AdaptiveUDPReport, *, indent: int | None = None) -> str:
    """Write deterministic strict JSON retaining all tested and excluded observations."""
    return json.dumps(
        adaptive_udp_report_to_dict(report), indent=indent, sort_keys=True, allow_nan=False
    )


def loads_adaptive_udp_report(text: str | bytes) -> AdaptiveUDPReport:
    """Read JSON while rejecting duplicate keys, nonfinite numbers, and future versions."""
    if not isinstance(text, (str, bytes)):
        _fail("", "expected JSON text or bytes")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _fail("", f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def constant(value):
        _fail("", f"nonfinite JSON constant: {value}")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        if isinstance(exc, ReportValidationError):
            raise
        _fail("", f"invalid JSON: {exc}")
    return adaptive_udp_report_from_dict(data)


def _range_text(values, unit):
    present = [item for item in values if item is not None]
    if not present:
        return "unavailable"
    low, high = min(present), max(present)
    span = f"{low:g}" if low == high else f"{low:g}..{high:g}"
    return f"{span} {unit}"


def render_adaptive_udp_text(report: AdaptiveUDPReport) -> str:
    """Describe discrete tested points, decision criteria, and unavailable evidence."""
    retained = adaptive_udp_report_from_dict(adaptive_udp_report_to_dict(report))
    result = retained.result
    policy = result.prepared.adaptive_policy
    highest = (
        "unavailable"
        if result.highest_eligible_bps is None
        else f"{result.highest_eligible_bps} bit/s"
    )
    points = ", ".join(str(rate) for rate in result.acceptable_rates_bps) or "none"
    decision_state = (
        "terminal" if result.decision.batch is None else "next admitted batch; not yet executed"
    )
    lines = [
        f"Adaptive UDP: {result.outcome}",
        f"Decision: {result.decision.reason} ({decision_state})",
        (
            f"Criteria: every measured repetition has receiver count-derived loss <= "
            f"{policy.receiver_loss_percent:g}%, sender/requested load >= "
            f"{policy.minimum_sender_fraction:g}; at least {policy.minimum_valid_trials} "
            f"valid trials and {policy.confirmation_batches} confirmation batches."
        ),
        f"Highest tested eligible offered load: {highest}",
        f"Acceptable tested offered loads (bit/s): {points}",
        f"Configured ceiling censors upper boundary: {str(result.ceiling_censored).lower()}",
        f"Observed cooldown: {result.observed_pause_seconds:g} seconds",
        "Untested gaps are unknown; tested points do not establish physical network capacity.",
    ]
    if result.decision.batch is not None:
        proposed = ", ".join(str(rate) for rate in result.decision.batch.rates)
        lines.append(
            f"Proposed next offered loads (bit/s), still untested in this batch: {proposed}"
        )
    for summary in result.summaries:
        observations = [item for item in summary.observations if item.phase != "warmup"]
        lines.append(
            f"Requested {summary.rate_bps} bit/s: {summary.status}; "
            f"{summary.valid_trials} valid trials; "
            f"{summary.confirmation_batches} confirmation batches."
        )
        for label, name, unit in (
            ("Allocated aggregate", "allocated_rate_bps", "bit/s"),
            ("Native allocation per stream", "native_per_stream_bps", "bit/s"),
            ("Achieved sender", "sender_bps", "bit/s"),
            ("Sender/requested fraction", "sender_fraction", "fraction"),
            ("Achieved receiver", "receiver_bps", "bit/s"),
            ("Count-derived receiver loss", "count_loss_percent", "%"),
            ("Native-reported receiver loss", "native_loss_percent", "%"),
        ):
            lines.append(
                f"  {label}: {_range_text([getattr(item, name) for item in observations], unit)}"
            )
        for observation in summary.observations:
            if observation.reasons:
                lines.append(
                    f"  {observation.trial_id} ({observation.phase}, "
                    f"{observation.execution_status}): {', '.join(observation.reasons)}"
                )
        if summary.reasons:
            lines.append(f"  Rate decision: {', '.join(summary.reasons)}")
    return "\n".join(lines)
