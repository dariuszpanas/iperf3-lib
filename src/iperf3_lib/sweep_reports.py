"""Strict sweep-v1 reports embedding the shared trial and compatibility codecs."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from importlib.metadata import version
from typing import Any, Literal, Never

from .analysis import ComparisonPolicy
from .artifacts import ArtifactProducer
from .config import ClientConfig
from .intent import RateIntent
from .reports import (
    ReportValidationError,
    UnsupportedReportVersionError,
    compatibility_from_dict,
    compatibility_to_dict,
    plan_result_from_dict,
    plan_result_to_dict,
)
from .result import JSONValue
from .sweeps import (
    SweepAxis,
    SweepComparison,
    SweepResult,
    _cell_summaries_v1,
    _comparison_groups,
    _comparison_policy,
    _comparison_quality,
    _count,
    _exact_equal,
    _prepare_v1,
    _validated_prepared,
)

SWEEP_ALGORITHM: Literal["receiver-cell-median-v1"] = "receiver-cell-median-v1"


@dataclass(frozen=True)
class SweepReport:
    """Versioned sweep evidence with producer identity and namespaced extensions."""

    result: SweepResult
    producer: ArtifactProducer
    kind: Literal["iperf3-lib.sweep"] = "iperf3-lib.sweep"
    schema_version: int = 1
    algorithm_revision: Literal["receiver-cell-median-v1"] = SWEEP_ALGORITHM
    extensions: dict[str, JSONValue] = field(default_factory=dict)


def _fail(path, message) -> Never:
    raise ReportValidationError(path, message)


def _object(value, path, keys=None):
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(path, "expected an object with string keys")
    if keys is not None and set(value) != set(keys):
        _fail(path, "object must contain exactly the declared sweep-v1 fields")
    return value


def _string(value, path):
    if type(value) is not str or not value.strip():
        _fail(path, "expected a nonempty string")
    return value


def _array(value, path):
    if type(value) is not list:
        _fail(path, "expected an array")
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
    def check(item):
        if isinstance(item, dict):
            _object(item, "/extensions")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    try:
        check(value)
        return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, RecursionError) as exc:
        _fail("", f"report must contain strict finite JSON values: {exc}")


def _config_from_dict(data, path):
    data = _object(data, path, {item.name for item in fields(ClientConfig)})
    _string(data["server"], f"{path}/server")
    if type(data["protocol"]) is not str:
        _fail(f"{path}/protocol", "expected a protocol string")
    try:
        return ClientConfig(**data)
    except (ValueError, TypeError) as exc:
        _fail(path, str(exc))


def _intent_from_dict(data):
    if data is None:
        return None
    data = _object(data, "/result/prepared/rate_intent", {item.name for item in fields(RateIntent)})
    try:
        return RateIntent(**data)
    except (ValueError, TypeError) as exc:
        _fail("/result/prepared/rate_intent", str(exc))


def _policy_from_dict(data):
    if data is None:
        return None
    path = "/result/comparison_policy"
    data = _object(data, path, {item.name for item in fields(ComparisonPolicy)})
    endpoints = tuple(_array(data["endpoint_pair"], f"{path}/endpoint_pair"))
    varying = tuple(_array(data["varying_fields"], f"{path}/varying_fields"))
    allowed = _object(data["allowed_differences"], f"{path}/allowed_differences")
    try:
        return ComparisonPolicy(data["group_id"], endpoints, varying, dict(allowed))
    except (ValueError, TypeError) as exc:
        _fail(path, str(exc))


def _prepared_metadata(prepared):
    return {
        "base_config": asdict(prepared.base_config),
        "axes": [asdict(axis) for axis in prepared.axes],
        "cells": [asdict(cell) for cell in prepared.cells],
        "rate_intent": asdict(prepared.rate_intent) if prepared.rate_intent is not None else None,
        "order": prepared.order,
        "seed": prepared.seed,
    }


def _prepared_from_dict(data, plan):
    path = "/result/prepared"
    data = _object(data, path, {"base_config", "axes", "cells", "rate_intent", "order", "seed"})
    base = _config_from_dict(data["base_config"], f"{path}/base_config")
    axes = []
    for index, item in enumerate(_array(data["axes"], f"{path}/axes")):
        _object(item, f"{path}/axes/{index}", {"name", "values"})
        try:
            axes.append(
                SweepAxis(
                    item["name"], tuple(_array(item["values"], f"{path}/axes/{index}/values"))
                )
            )
        except (ValueError, TypeError) as exc:
            _fail(f"{path}/axes/{index}", str(exc))
    cells = _array(data["cells"], f"{path}/cells")
    ids = tuple(
        _string(_object(cell, f"{path}/cells/{index}")["cell_id"], f"{path}/cells/{index}/cell_id")
        for index, cell in enumerate(cells)
        if "cell_id" in _object(cell, f"{path}/cells/{index}")
    )
    if len(ids) != len(cells):
        _fail(f"{path}/cells", "every cell requires an identity")
    try:
        prepared = _prepare_v1(
            base,
            tuple(axes),
            policy=plan.policy,
            budget=plan.budget,
            rate_intent=_intent_from_dict(data["rate_intent"]),
            order=data["order"],
            seed=data["seed"],
            recorded_order=ids,
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        _fail(path, str(exc))
    if not _exact_equal(_prepared_metadata(prepared), data):
        _fail(path, "stored cells/settings do not match the finite axes and recorded order")
    if not _exact_equal(asdict(prepared.plan), asdict(plan)):
        _fail(
            path,
            "sweep trial membership, settings, estimates or order disagree with the execution plan",
        )
    return prepared


def _selected_artifacts(execution, group):
    artifacts = {
        record.spec.trial_id: record.artifact
        for record in execution.trials
        if record.artifact is not None
    }
    return {
        sample.trial_id: artifacts[sample.trial_id]
        for _, summary in group
        for sample in summary.samples
        if sample.eligible_for_cell
    }


def _comparison_mapping(comparison, execution, group):
    return {
        "method": comparison.method,
        "direction": comparison.direction,
        "cell_ids": list(comparison.cell_ids),
        "quality": comparison.quality,
        "reasons": list(comparison.reasons),
        "compatibility": compatibility_to_dict(
            comparison.compatibility, artifacts=_selected_artifacts(execution, group)
        ),
    }


def _read_comparisons(data, prepared, execution, cells, policy):
    data = _array(data, "/result/comparisons")
    if policy is None:
        if data:
            _fail("/result/comparisons", "cross-cell comparison requires an explicit policy")
        return ()
    try:
        effective = _comparison_policy(prepared, policy)
    except (ValueError, TypeError) as exc:
        _fail("/result/comparison_policy", str(exc))
    groups = _comparison_groups(cells)
    if len(data) != len(groups):
        _fail("/result/comparisons", "every method/direction group must be retained")
    comparisons = []
    for index, ((method, direction), group) in enumerate(groups.items()):
        path = f"/result/comparisons/{index}"
        item = _object(
            data[index],
            path,
            {"method", "direction", "cell_ids", "quality", "reasons", "compatibility"},
        )
        comparison = compatibility_from_dict(
            item["compatibility"], artifacts=_selected_artifacts(execution, group)
        )
        if not _exact_equal(asdict(comparison.policy), asdict(effective)):
            _fail(path, "comparison policy does not match declared axes and recorded exceptions")
        quality, reasons = _comparison_quality(group, comparison)
        expected = SweepComparison(
            method,
            direction,
            tuple(identity for identity, _ in group),
            quality,
            reasons,
            comparison,
        )
        if not _exact_equal(item, _comparison_mapping(expected, execution, group)):
            _fail(path, "comparison grouping/quality disagrees with retained cell evidence")
        comparisons.append(expected)
    return tuple(comparisons)


def sweep_report_from_dict(mapping: dict[str, Any]) -> SweepReport:
    """Read strict sweep-v1 evidence without current analysis, RNG replay or native execution."""
    try:
        _strict_json(mapping)
    except RecursionError:
        _fail("", "JSON values must not contain cycles or excessive nesting")
    data = _json_copy(mapping)
    _object(
        data,
        "",
        {"kind", "schema_version", "algorithm_revision", "producer", "extensions", "result"},
    )
    if type(data["schema_version"]) is not int:
        _fail("/schema_version", "expected an integer schema version")
    if data["schema_version"] != 1 or data["algorithm_revision"] != SWEEP_ALGORITHM:
        raise UnsupportedReportVersionError(
            "/schema_version", "unsupported sweep schema or algorithm; upgrade the reader"
        )
    if data["kind"] != "iperf3-lib.sweep":
        _fail("/kind", "expected iperf3-lib.sweep")
    producer = _object(data["producer"], "/producer", {"name", "version"})
    producer = ArtifactProducer(
        _string(producer["name"], "/producer/name"),
        _string(producer["version"], "/producer/version"),
    )
    extensions = _object(data["extensions"], "/extensions")
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+", key)
        for key in extensions
    ):
        _fail("/extensions", "extension keys must be namespaced")
    result = _object(
        data["result"],
        "/result",
        {
            "prepared",
            "execution",
            "minimum_valid_trials",
            "cells",
            "comparison_policy",
            "comparisons",
        },
    )
    execution = plan_result_from_dict(result["execution"])
    prepared = _prepared_from_dict(result["prepared"], execution.plan)
    try:
        minimum = _count(result["minimum_valid_trials"], "minimum_valid_trials", 1)
        cells = _cell_summaries_v1(prepared, execution, minimum)
    except (ValueError, TypeError) as exc:
        _fail("/result/cells", str(exc))
    if not _exact_equal([asdict(cell) for cell in cells], result["cells"]):
        _fail(
            "/result/cells",
            "stored summary counts, samples, exclusions, evidence or medians disagree with frozen v1 arithmetic",
        )
    policy = _policy_from_dict(result["comparison_policy"])
    comparisons = _read_comparisons(result["comparisons"], prepared, execution, cells, policy)
    return SweepReport(
        SweepResult(prepared, execution, minimum, cells, policy, comparisons),
        producer,
        extensions=extensions,
    )


def sweep_report_to_dict(report: SweepReport) -> dict[str, Any]:
    """Validate and detach the complete sweep report, retaining shared trial evidence."""
    if (
        not isinstance(report, SweepReport)
        or not isinstance(report.result, SweepResult)
        or not isinstance(report.producer, ArtifactProducer)
    ):
        _fail("", "expected a SweepReport with result and producer dataclasses")
    try:
        _strict_json(report.extensions, "/extensions")
    except RecursionError:
        _fail("/extensions", "extensions must not contain cycles or excessive nesting")
    prepared = _validated_prepared(report.result.prepared)
    result = report.result
    groups = _comparison_groups(result.cells)
    data = {
        "kind": report.kind,
        "schema_version": report.schema_version,
        "algorithm_revision": report.algorithm_revision,
        "producer": asdict(report.producer),
        "extensions": report.extensions,
        "result": {
            "prepared": _prepared_metadata(prepared),
            "execution": plan_result_to_dict(result.execution),
            "minimum_valid_trials": result.minimum_valid_trials,
            "cells": [asdict(cell) for cell in result.cells],
            "comparison_policy": asdict(result.comparison_policy)
            if result.comparison_policy is not None
            else None,
            "comparisons": [
                _comparison_mapping(
                    comparison,
                    result.execution,
                    groups.get((comparison.method, comparison.direction), []),
                )
                for comparison in result.comparisons
            ],
        },
    }
    data = _json_copy(data)
    sweep_report_from_dict(data)
    return data


def report_from_sweep(result: SweepResult) -> SweepReport:
    """Snapshot a sweep with this writer's producer identity, without native work."""
    return sweep_report_from_dict(
        sweep_report_to_dict(
            SweepReport(result, ArtifactProducer("iperf3-lib", version("iperf3-lib")))
        )
    )


def dumps_sweep_report(report: SweepReport, *, indent: int | None = None) -> str:
    """Write deterministic strict JSON for a validated sweep-v1 report."""
    return json.dumps(sweep_report_to_dict(report), indent=indent, sort_keys=True, allow_nan=False)


def loads_sweep_report(text: str | bytes) -> SweepReport:
    """Read a report while rejecting duplicate keys, nonfinite numbers and future versions."""
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
    return sweep_report_from_dict(data)
