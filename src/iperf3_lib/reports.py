"""Strict assessment-report v1 JSON, concise text, and JUnit XML output."""

from __future__ import annotations

import dataclasses
import json
import math
import types
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any, Literal, Never, TypeAliasType, get_args, get_origin, get_type_hints

from ._evidence import has_observed_evidence
from .analysis import (
    AnalysisDiagnostic,
    ComparisonPolicy,
    CompatibilityAnalysis,
    EvidenceRef,
    ThroughputAnalysis,
    _expanded_comparison_fields_v1,
    _expanded_effective_v1,
)
from .artifacts import ResultArtifact, artifact_from_dict, artifact_to_dict, dumps_artifact
from .assessments import (
    ASSESSMENT_ALGORITHM,
    AssessedMeasurement,
    Assessment,
    AssessmentPolicy,
    AssessmentReport,
    ExcludedTrial,
    _decision_v1,
    ci_exit_code,
)
from .config import LEGACY_CONFIG_FIELDS, ClientConfig, Protocol
from .intent import PlanEstimate, RateIntent, ResolvedRate, RunEstimate
from .result import JSONValue
from .trials import (
    PlanBudget,
    PlanResult,
    PreparedPlan,
    TrialException,
    TrialPolicy,
    TrialRecord,
    TrialSpec,
    _json_evidence,
    prepare_plan,
)


class ReportValidationError(ValueError):
    """A report field violates its explicit versioned schema or internal contract."""

    def __init__(self, path: str, message: str):
        """Retain a JSON-style path alongside the validation failure."""
        self.path = path
        super().__init__(f"{path or '/'}: {message}")


class UnsupportedReportVersionError(ReportValidationError):
    """The reader does not implement this report schema or assessment algorithm."""


_MODELS = {
    AssessmentReport,
    Assessment,
    AssessmentPolicy,
    AssessedMeasurement,
    ExcludedTrial,
    ComparisonPolicy,
    CompatibilityAnalysis,
    AnalysisDiagnostic,
    EvidenceRef,
    ThroughputAnalysis,
    PlanResult,
    PreparedPlan,
    TrialSpec,
    TrialRecord,
    TrialException,
    TrialPolicy,
    PlanBudget,
    PlanEstimate,
    RunEstimate,
    RateIntent,
    ResolvedRate,
    ClientConfig,
}


def _fail(path, message) -> Never:
    raise ReportValidationError(path, message)


def _child(path, key):
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _object(value, path):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(path, "expected an object with string keys")
    return value


def _convert(spec, value, path, *, encoding=False):
    if spec is ResultArtifact:
        try:
            return artifact_to_dict(value) if encoding else artifact_from_dict(value)
        except (ValueError, TypeError) as exc:
            _fail(path, f"invalid retained result artifact: {exc}")
    if spec in _MODELS:
        return _model(spec, value, path, encoding=encoding)
    if spec is JSONValue or spec is object:
        try:
            return _json_evidence(value)
        except (ValueError, TypeError, RecursionError) as exc:
            _fail(path, str(exc))
    if isinstance(spec, TypeAliasType):
        return _convert(spec.__value__, value, path, encoding=encoding)
    if spec is Protocol:
        if encoding and isinstance(value, Protocol):
            return value.value
        if not isinstance(value, str):
            _fail(path, "expected a protocol string")
        try:
            return Protocol(value)
        except ValueError:
            _fail(path, "invalid protocol")
    origin, args = get_origin(spec), get_args(spec)
    if origin is types.UnionType:
        if value is None and type(None) in args:
            return None
        for option in args:
            if option is not type(None):
                return _convert(option, value, path, encoding=encoding)
    if origin is Literal:
        if not any(type(value) is type(option) and value == option for option in args):
            _fail(path, f"expected one of {args}")
        return value
    if origin is tuple:
        if not isinstance(value, tuple if encoding else list):
            _fail(path, "expected an array")
        if len(args) == 2 and args[1] is Ellipsis:
            converted = [
                _convert(args[0], item, _child(path, index), encoding=encoding)
                for index, item in enumerate(value)
            ]
        else:
            if len(value) != len(args):
                _fail(path, "array has the wrong length")
            converted = [
                _convert(item_type, item, _child(path, index), encoding=encoding)
                for index, (item_type, item) in enumerate(zip(args, value, strict=True))
            ]
        return converted if encoding else tuple(converted)
    if origin is dict:
        data = _object(value, path)
        return {
            key: _convert(args[1], item, _child(path, key), encoding=encoding)
            for key, item in data.items()
        }
    if spec is bool:
        if type(value) is not bool:
            _fail(path, "expected a boolean")
    elif spec is int:
        if type(value) is not int:
            _fail(path, "expected an integer")
    elif spec is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(path, "expected a finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            _fail(path, "expected a finite number")
    elif spec is str:
        if not isinstance(value, str):
            _fail(path, "expected a string")
    else:
        _fail(path, f"unsupported report field type {spec}")
    return value


def _model(model, value, path, *, encoding=False):
    fields = dataclasses.fields(model)
    hints = get_type_hints(model)
    extras = {
        TrialSpec: {"resolved_config"},
        PlanResult: {"execution_success"},
        AssessmentReport: {"ci_exit_code"},
    }.get(model, set())
    if encoding:
        if not isinstance(value, model):
            _fail(path, f"expected {model.__name__}")
        data = {field.name: getattr(value, field.name) for field in fields}
        if model is ClientConfig:
            data["server"] = str(data["server"])
    else:
        data = _object(value, path)
        if model is ClientConfig and LEGACY_CONFIG_FIELDS <= set(data):
            # Earlier development artifacts retain their original field set.
            # New options are additive with the documented dataclass defaults.
            data = dict(data)
            for field in fields:
                if field.name not in data:
                    default = field.default
                    data[field.name] = list(default) if isinstance(default, tuple) else default
        expected = {field.name for field in fields} | extras
        if set(data) != expected:
            _fail(
                path,
                f"field set differs; missing={sorted(expected - set(data))}, unknown={sorted(set(data) - expected)}",
            )
        if model is PreparedPlan:
            policy = _convert(TrialPolicy, data["policy"], _child(path, "policy"))
            if (
                not isinstance(data["trials"], list)
                or not 1 <= len(data["trials"]) <= policy.max_trials
            ):
                _fail(_child(path, "trials"), "trial count exceeds admission policy or is empty")
    values = {
        field.name: _convert(
            hints[field.name], data[field.name], _child(path, field.name), encoding=encoding
        )
        for field in fields
    }
    if encoding:
        if model is ClientConfig:
            for field in fields:
                if (
                    field.name not in LEGACY_CONFIG_FIELDS
                    and getattr(value, field.name) == field.default
                ):
                    values.pop(field.name)
        if model is TrialSpec:
            values["resolved_config"] = _convert(
                ClientConfig, value.resolved_config, _child(path, "resolved_config"), encoding=True
            )
        elif model is PlanResult:
            values["execution_success"] = value.execution_success
        elif model is AssessmentReport:
            values["ci_exit_code"] = ci_exit_code(value)
        return values
    try:
        instance = model(**values)
    except (ValueError, TypeError) as exc:
        _fail(path, str(exc))
    if model is TrialSpec:
        resolved = _convert(ClientConfig, data["resolved_config"], _child(path, "resolved_config"))
        if resolved != instance.resolved_config:
            _fail(
                _child(path, "resolved_config"),
                "derived native configuration disagrees with retained intent",
            )
    elif model is PlanResult:
        if (
            _convert(bool, data["execution_success"], _child(path, "execution_success"))
            != instance.execution_success
        ):
            _fail(
                _child(path, "execution_success"),
                "execution outcome disagrees with retained trials",
            )
    elif model is AssessmentReport:
        if _convert(int, data["ci_exit_code"], _child(path, "ci_exit_code")) != ci_exit_code(
            instance
        ):
            _fail(_child(path, "ci_exit_code"), "CI status disagrees with execution/assessment")
    return instance


def _validate_history(execution):
    plan = execution.plan
    try:
        prepared = prepare_plan(
            plan.trials, policy=plan.policy, budget=plan.budget, order_seed=plan.order_seed
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        _fail("/execution/plan", str(exc))
    if prepared != plan:
        _fail(
            "/execution/plan",
            "stored admission estimates or settings disagree with the finite plan",
        )
    if len(execution.trials) != len(plan.trials):
        _fail("/execution/trials", "every planned trial must be retained")
    stopped = False
    execution_failed = False
    for index, (record, spec) in enumerate(zip(execution.trials, plan.trials, strict=True)):
        path = f"/execution/trials/{index}"
        if record.spec != spec:
            _fail(path, "trial settings/order disagree with the admitted plan")
        if record.status == "not_run":
            stopped = True
            if not record.reason or record.reason != execution.stop_reason:
                _fail(path, "unstarted trial requires the recorded plan stop reason")
            if any(
                item is not None
                for item in (
                    record.artifact,
                    record.exception,
                    record.started_at_seconds,
                    record.completed_at_seconds,
                    record.elapsed_seconds,
                    record.returned_result_evidence,
                )
            ):
                _fail(path, "unstarted trial cannot contain execution or measurement evidence")
            continue
        if stopped:
            _fail(path, "executed trial follows a stopped/unstarted trial")
        if execution_failed and plan.policy.stop_on_error:
            _fail(path, "executed trial follows failed/incomplete execution under stop_on_error")
        execution_failed |= record.status != "completed"
        if (
            any(
                item is None
                for item in (
                    record.started_at_seconds,
                    record.completed_at_seconds,
                    record.elapsed_seconds,
                )
            )
            or record.elapsed_seconds < 0
        ):
            _fail(path, "executed trial requires observed timing and nonnegative elapsed duration")
        if record.status == "exception":
            if (
                record.artifact is not None
                or record.exception is None
                or not record.exception.type_name
            ):
                _fail(path, "exception requires an exception record and no fabricated artifact")
            if record.returned_result_evidence is not None and not record.diagnostics:
                _fail(path, "partial returned evidence requires serialization diagnostics")
        else:
            if (
                record.artifact is None
                or record.exception is not None
                or record.returned_result_evidence is not None
            ):
                _fail(path, "native outcome requires exactly one retained artifact")
            if (
                record.artifact.result.execution is None
                or record.artifact.result.execution.status != record.status
            ):
                _fail(path, "trial status disagrees with artifact execution status")
    if execution.elapsed_seconds < 0 or execution.observed_pause_seconds < 0:
        _fail("/execution", "elapsed and pause durations must be nonnegative")
    duration = (
        math.fsum(record.elapsed_seconds or 0 for record in execution.trials)
        + execution.observed_pause_seconds
    )
    if duration > execution.elapsed_seconds + 1e-9:
        _fail(
            "/execution/elapsed_seconds",
            "recorded work and pauses exceed total monotonic elapsed time",
        )
    if execution.stop_reason not in (None, "stop_on_error", "elapsed_admission_limit"):
        _fail("/execution/stop_reason", "unknown execution stop reason")
    if (
        execution.stop_reason == "elapsed_admission_limit"
        and plan.budget.stop_after_elapsed_seconds is None
    ):
        _fail("/execution/stop_reason", "elapsed stop requires an explicit policy")
    if execution.stop_reason == "stop_on_error" and (
        not plan.policy.stop_on_error
        or all(record.status in ("completed", "not_run") for record in execution.trials)
    ):
        _fail(
            "/execution/stop_reason",
            "error stop requires policy and failed/incomplete execution evidence",
        )


def _summary_selection_v1(artifact, direction, observation, path):
    """Freeze eligibility and arithmetic for a validated canonical endpoint summary."""
    result = artifact.result
    if (
        not result.ok
        or result.error is not None
        or result.execution is None
        or result.execution.status != "completed"
    ):
        return None
    matches = [
        (index, flow) for index, flow in enumerate(result.flows) if flow.direction == direction
    ]
    if len(matches) != 1:
        return None
    index, flow = matches[0]
    stats = getattr(flow, observation)
    if (
        stats is None
        or stats.omitted is True
        or stats.bytes is None
        or stats.duration_seconds is None
        or stats.duration_seconds <= 0
    ):
        return None
    if stats.direction not in (None, direction) or stats.observation not in (None, observation):
        _fail(path, "summary provenance conflicts with its selected container")
    try:
        derived = stats.bytes * 8 / stats.duration_seconds
    except OverflowError:
        _fail(path, "measured throughput exceeds finite numeric range")
    if not math.isfinite(derived):
        _fail(path, "measured throughput exceeds finite numeric range")
    return index, stats, derived


def summary_eligible_v1(
    artifact: ResultArtifact,
    *,
    direction: Literal["client_to_server", "server_to_client"],
    observation: Literal["sender", "receiver"],
) -> bool:
    """Check frozen report-v1 summary eligibility without invoking current analysis.

    Other report envelopes can use this to require every eligible measurement
    and to validate insufficient-summary exclusions. The artifact is validated
    and detached; a completed, non-omitted zero-byte summary remains eligible.
    """
    if direction not in ("client_to_server", "server_to_client"):
        _fail("/selection/direction", "invalid summary direction")
    if observation not in ("sender", "receiver"):
        _fail("/selection/observation", "invalid summary observation")
    data = _convert(ResultArtifact, artifact, "/artifact", encoding=True)
    retained = _convert(ResultArtifact, data, "/artifact")
    return _summary_selection_v1(retained, direction, observation, "/selection") is not None


def _validate_measurement_v1(measurement, artifact, policy, path):
    """Check retained v1 summary arithmetic without rerunning the current analysis API."""
    value = measurement.throughput
    if (
        value.quality != "complete"
        or value.direction != policy.direction
        or value.observation != policy.observation
    ):
        _fail(path, "selected measurement does not satisfy the recorded policy")
    selection = _summary_selection_v1(artifact, policy.direction, policy.observation, path)
    if selection is None:
        _fail(path, "selected measurement lacks an eligible completed endpoint summary")
    index, stats, derived = selection
    if value.bytes != stats.bytes or value.measured_seconds != stats.duration_seconds:
        _fail(path, "selected byte/duration evidence disagrees with the retained artifact")
    if value.throughput_bps != derived:
        _fail(path, "throughput must equal measured bytes times eight divided by measured seconds")
    expected = (
        EvidenceRef(f"/flows/{index}/{policy.observation}/bytes"),
        EvidenceRef(f"/flows/{index}/{policy.observation}/duration_seconds"),
    )
    if value.evidence != expected:
        _fail(path, "summary measurement evidence pointers disagree with v1 selection")


def _fingerprint_v1(result, expanded=frozenset()):
    """Freeze the report-v1 projection of already canonical provenance and receipts."""
    metadata = result.execution
    if metadata is not None:
        for name in ("native_version", "native_system_info"):
            value = getattr(metadata, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                _fail("/assessment/compatibility", f"{name} must be a nonempty string or None")
    intent = {"basis": "native_per_stream"}
    extension = result.extensions.get("iperf3_lib.rate_intent")
    if extension is not None:
        if (
            not isinstance(extension, dict)
            or type(extension.get("schema_version")) is not int
            or extension["schema_version"] != 1
        ):
            _fail("/assessment/compatibility", "unsupported retained rate intent extension")
        resolution = extension.get("resolution")
        if not isinstance(resolution, dict) or resolution.get("source") not in {
            "legacy",
            "per_stream",
            "aggregate",
            "protocol_default",
        }:
            _fail("/assessment/compatibility", "malformed retained rate resolution")
        if resolution["source"] == "aggregate":
            requested = extension.get("intent")
            target = (
                requested.get("aggregate_bps_per_direction")
                if isinstance(requested, dict)
                else None
            )
            if type(target) is not int or not 1 <= target <= 2**64 - 1:
                _fail(
                    "/assessment/compatibility", "aggregate intent must be a positive uint64 value"
                )
            intent = {"basis": "aggregate_per_direction", "target_bps": target}
    fingerprint = {
        "protocol": result.protocol,
        "method": metadata.method if metadata else None,
        "native_version": metadata.native_version if metadata else None,
        "native_system_info": metadata.native_system_info if metadata else None,
        "rate_intent": intent,
    }
    for name in ("server", "port", "duration", "omit", "parallel", "rate", "blksize", "tos"):
        setting = metadata.configuration.effective.get(name) if metadata else None
        fingerprint[name] = (
            setting.value
            if setting is not None
            and setting.state == "verified"
            and has_observed_evidence(result, setting.evidence_paths)
            else None
        )
        value = fingerprint[name]
        if value is None:
            continue
        if name == "server":
            if not isinstance(value, str) or not value.strip():
                _fail("/assessment/compatibility", "verified server must be a nonempty string")
        else:
            minimum, maximum = {
                "port": (1, 65535),
                "duration": (0, 86400),
                "omit": (0, 600),
                "parallel": (1, 128),
                "rate": (0, 2**64 - 1),
                "blksize": (0, 1024 * 1024),
                "tos": (0, 255),
            }[name]
            if type(value) is not int or not minimum <= value <= maximum:
                _fail(
                    "/assessment/compatibility",
                    f"verified {name} must be an integer between {minimum} and {maximum}",
                )
    for name in sorted(expanded):
        fingerprint[name] = _expanded_effective_v1(result, name)
    return fingerprint


def _validate_compatibility_v1(compatibility, selected):
    """Validate frozen v1 compatibility conclusions without invoking current analysis."""
    try:
        expanded = _expanded_comparison_fields_v1(
            [artifact.result for artifact in selected.values()], compatibility.policy
        )
        fingerprints = {
            identifier: _fingerprint_v1(artifact.result, expanded)
            for identifier, artifact in selected.items()
        }
    except (ValueError, TypeError) as exc:
        _fail("/assessment/compatibility", str(exc))
    if json.dumps(fingerprints, sort_keys=True, allow_nan=False) != json.dumps(
        compatibility.fingerprints, sort_keys=True, allow_nan=False
    ):
        _fail(
            "/assessment/compatibility/fingerprints",
            "stored fingerprints disagree with canonical provenance and verified receipts",
        )
    varying = set(compatibility.policy.varying_fields)
    compatible = bool(fingerprints)
    partial = False
    intents = [item["rate_intent"] for item in fingerprints.values()]
    if intents and all(
        intent["basis"] == "aggregate_per_direction" and intent == intents[0] for intent in intents
    ):
        target = intents[0]["target_bps"]
        allocated = all(
            type(item["parallel"]) is int
            and item["parallel"] >= 1
            and target >= item["parallel"]
            and item["rate"] == target // item["parallel"]
            for item in fingerprints.values()
        )
        compatible &= allocated
        if allocated:
            varying.add("rate")
    fields = next(iter(fingerprints.values()), {})
    for name in fields:
        values = [item[name] for item in fingerprints.values()]
        missing = any(
            value is None or (name not in expanded and value == "unknown") for value in values
        )
        differs = any(value != values[0] for value in values[1:])
        if missing:
            compatible = False
        elif differs and name not in varying:
            if name in compatibility.policy.allowed_differences:
                partial = True
            else:
                compatible = False
    quality = "insufficient_data" if not compatible else "partial" if partial else "complete"
    if compatibility.compatible != compatible or compatibility.quality != quality:
        _fail(
            "/assessment/compatibility",
            "stored compatibility decision disagrees with frozen v1 evidence and recorded difference policy",
        )


def _validate_report(report):
    _validate_history(report.execution)
    assessment = report.assessment
    if assessment.compatibility.policy != report.comparison:
        _fail("/assessment/compatibility/policy", "comparison policy disagrees with report policy")
    if (
        assessment.compatibility.compatible
        and assessment.compatibility.quality == "insufficient_data"
    ):
        _fail(
            "/assessment/compatibility", "insufficient compatibility evidence cannot be compatible"
        )
    retained = {f"trial:{record.spec.trial_id}": record for record in report.execution.trials}
    baseline_ids = {
        f"baseline:{index}": artifact for index, artifact in enumerate(report.baselines or ())
    }
    seen = set()
    selected = {}
    for label, population in (
        ("measurements", assessment.measurements),
        ("baseline_measurements", assessment.baseline_measurements),
    ):
        for index, measurement in enumerate(population):
            identifier = measurement.trial_id
            path = f"/assessment/{label}/{index}"
            if identifier in seen:
                _fail(path, "trial is counted more than once")
            seen.add(identifier)
            if label == "measurements":
                record = retained.get(identifier)
                if (
                    record is None
                    or record.spec.phase != "measured"
                    or record.status != "completed"
                    or record.artifact is None
                ):
                    _fail(path, "measurement does not identify a completed measured trial")
                artifact = record.artifact
            else:
                artifact = baseline_ids.get(identifier)
                if artifact is None:
                    _fail(path, "measurement does not identify a retained baseline")
            _validate_measurement_v1(measurement, artifact, report.policy, path)
            selected[identifier] = artifact
    if set(assessment.compatibility.fingerprints) != seen:
        _fail(
            "/assessment/compatibility/fingerprints",
            "fingerprints must identify every selected comparison trial",
        )
    _validate_compatibility_v1(assessment.compatibility, selected)
    for index, exclusion in enumerate(assessment.exclusions):
        path = f"/assessment/exclusions/{index}"
        if exclusion.trial_id in seen or not exclusion.reason:
            _fail(path, "duplicate or unexplained exclusion")
        record = retained.get(exclusion.trial_id)
        if record is not None and record.spec.phase == "warmup":
            expected_reason = "warmup_run"
        elif record is not None and record.status != "completed":
            expected_reason = f"execution_{record.status}"
        else:
            artifact = (
                record.artifact if record is not None else baseline_ids.get(exclusion.trial_id)
            )
            if artifact is None:
                _fail(path, "exclusion does not identify retained execution or baseline evidence")
            if (
                _summary_selection_v1(
                    artifact, report.policy.direction, report.policy.observation, path
                )
                is not None
            ):
                _fail(path, "eligible summaries must be retained as measurements, including zero")
            expected_reason = "insufficient_summary_measurement"
        if exclusion.reason != expected_reason:
            _fail(
                path,
                "exclusion reason disagrees with retained phase, execution, or summary evidence",
            )
        seen.add(exclusion.trial_id)
    if seen != set(retained) | set(baseline_ids):
        _fail("/assessment", "every trial and baseline needs a measurement or exclusion")
    expected = _decision_v1(
        report.policy,
        assessment.measurements,
        assessment.baseline_measurements,
        report.baselines is not None,
        assessment.compatibility,
    )
    actual = (
        assessment.outcome,
        assessment.median_throughput_bps,
        assessment.baseline_median_bps,
        assessment.effective_minimum_bps,
        assessment.relative_change,
        assessment.reasons,
    )
    if actual != expected:
        _fail(
            "/assessment",
            "decision disagrees with retained v1 analysis, counts, thresholds or tolerance",
        )


def report_to_dict(report: AssessmentReport) -> dict[str, Any]:
    """Validate mutable nested evidence and return detached strict report-v1 data."""
    data = _convert(AssessmentReport, report, "", encoding=True)
    report_from_dict(data)
    return data


def plan_result_to_dict(execution: PlanResult) -> dict[str, Any]:
    """Encode the strict report-v1 execution payload for other versioned reports.

    The containing report owns its version envelope. This payload preserves all
    admitted configurations, resolved intent, artifacts, and unstarted records.
    """
    data = _convert(PlanResult, execution, "/execution", encoding=True)
    plan_result_from_dict(data)
    return data


def plan_result_from_dict(mapping: dict[str, Any]) -> PlanResult:
    """Decode a report-v1 execution payload without native loading or execution."""
    execution = _convert(PlanResult, mapping, "/execution")
    _validate_history(execution)
    return execution


def compatibility_from_dict(
    mapping: dict[str, Any], *, artifacts: Mapping[str, ResultArtifact]
) -> CompatibilityAnalysis:
    """Read frozen report-v1 compatibility against its identified retained artifacts.

    This seam is shared with other versioned experiment reports. It validates
    stored fingerprints and conclusions using v1 rules, never current analysis.
    """
    if not isinstance(artifacts, Mapping) or any(
        not isinstance(key, str) or not key.strip() for key in artifacts
    ):
        _fail("/assessment/compatibility", "artifacts require nonempty string trial identities")
    retained = {
        key: _convert(ResultArtifact, value, f"/artifacts/{key}", encoding=True)
        for key, value in artifacts.items()
    }
    selected = {
        key: _convert(ResultArtifact, value, f"/artifacts/{key}") for key, value in retained.items()
    }
    comparison = _convert(CompatibilityAnalysis, mapping, "/assessment/compatibility")
    _validate_compatibility_v1(comparison, selected)
    return comparison


def compatibility_to_dict(
    comparison: CompatibilityAnalysis, *, artifacts: Mapping[str, ResultArtifact]
) -> dict[str, Any]:
    """Encode strict report-v1 compatibility with canonical evidence verification."""
    data = _convert(CompatibilityAnalysis, comparison, "/assessment/compatibility", encoding=True)
    compatibility_from_dict(data, artifacts=artifacts)
    return data


def report_from_dict(mapping: dict[str, Any]) -> AssessmentReport:
    """Read v1 evidence without native loading or invoking current analysis functions.

    Stored comparison fingerprints/decisions are retained as archive evidence.
    Frozen median-summary-v1 arithmetic and internal references are validated;
    this is not a fresh compatibility analysis or an authenticity signature.
    """
    data = _object(mapping, "")
    if type(data.get("schema_version")) is not int:
        _fail("/schema_version", "expected an explicit integer schema version")
    if data["schema_version"] != 1:
        raise UnsupportedReportVersionError(
            "/schema_version", "unsupported report schema; upgrade the reader"
        )
    if (
        isinstance(data.get("assessment"), dict)
        and data["assessment"].get("algorithm_revision") != ASSESSMENT_ALGORITHM
    ):
        raise UnsupportedReportVersionError(
            "/assessment/algorithm_revision", "unsupported assessment algorithm; upgrade the reader"
        )
    report = _convert(AssessmentReport, data, "")
    _validate_report(report)
    return report


def dumps_report(report: AssessmentReport, *, indent: int | None = None) -> str:
    """Serialize a strict, deterministic assessment report with retained artifacts."""
    return json.dumps(report_to_dict(report), sort_keys=True, indent=indent, allow_nan=False)


def loads_report(text: str | bytes) -> AssessmentReport:
    """Reject duplicate keys and nonfinite JSON before applying the v1 schema."""
    if not isinstance(text, (str, bytes)):
        _fail("", "expected JSON text or bytes")

    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                _fail("", f"duplicate JSON key: {key}")
            data[key] = value
        return data

    def invalid_constant(value):
        _fail("", f"nonfinite JSON constant: {value}")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ReportValidationError("", f"invalid JSON: {exc}") from exc
    return report_from_dict(data)


def render_text(report: AssessmentReport) -> str:
    """Render a concise deterministic summary while JSON retains the full evidence."""
    report_to_dict(report)
    assessment = report.assessment
    counts = {
        status: sum(record.status == status for record in report.execution.trials)
        for status in ("completed", "failed", "incomplete", "exception", "not_run")
    }
    return (
        "\n".join(
            [
                f"Performance: {assessment.outcome}; execution_success={report.execution.execution_success}; ci_exit_code={ci_exit_code(report)}",
                f"Selection: {report.policy.direction}/{report.policy.observation}; aggregation=median; valid={len(assessment.measurements)}/{report.policy.minimum_valid_trials}",
                f"Median bps: {assessment.median_throughput_bps}; baseline bps: {assessment.baseline_median_bps}; required bps: {assessment.effective_minimum_bps}",
                "Trials: " + ", ".join(f"{key}={value}" for key, value in counts.items()),
                "Reasons: " + ", ".join(assessment.reasons),
                "Excluded: "
                + ", ".join(f"{item.trial_id} ({item.reason})" for item in assessment.exclusions),
            ]
        )
        + "\n"
    )


def _xml_text(value: str) -> str:
    return "".join(
        char
        if char in "\t\n\r"
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
        else "\ufffd"
        for char in value
    )


def render_junit(
    report: AssessmentReport, *, inconclusive: Literal["failure", "skipped"] = "failure"
) -> str:
    """Retain execution errors and separate assessment failure in standard JUnit XML."""
    if inconclusive not in ("failure", "skipped"):
        raise ValueError("inconclusive must be failure or skipped")
    report_to_dict(report)
    root = ET.Element(
        "testsuite", name="iperf3-lib assessment", tests=str(len(report.execution.trials) + 1)
    )
    for record in report.execution.trials:
        case = ET.SubElement(
            root,
            "testcase",
            name=_xml_text(record.spec.trial_id),
            classname=f"iperf3_lib.{record.spec.phase}",
            time=str(record.elapsed_seconds or 0),
        )
        if record.status in ("failed", "exception"):
            message = (
                record.exception.message
                if record.exception
                else (record.artifact.result.error if record.artifact else None) or "native failure"
            )
            ET.SubElement(case, "error", type=record.status, message=_xml_text(message))
        elif record.status in ("incomplete", "not_run"):
            ET.SubElement(
                case,
                inconclusive,
                type="inconclusive",
                message=_xml_text(record.reason or record.status),
            )
        if record.artifact is not None:
            ET.SubElement(case, "system-out").text = _xml_text(dumps_artifact(record.artifact))
        elif record.returned_result_evidence is not None:
            ET.SubElement(case, "system-out").text = _xml_text(
                json.dumps(record.returned_result_evidence, sort_keys=True, allow_nan=False)
            )
    case = ET.SubElement(
        root, "testcase", name="performance_assessment", classname="iperf3_lib.assessment"
    )
    if report.assessment.outcome != "pass":
        tag = "failure" if report.assessment.outcome == "fail" else inconclusive
        ET.SubElement(
            case, tag, type=report.assessment.outcome, message=", ".join(report.assessment.reasons)
        )
    ET.SubElement(case, "system-out").text = _xml_text(render_text(report))
    for attribute, tag in (("errors", "error"), ("failures", "failure"), ("skipped", "skipped")):
        root.set(attribute, str(len(root.findall(f"testcase/{tag}"))))
    root.set("time", str(report.execution.elapsed_seconds))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
