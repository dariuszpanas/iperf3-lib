"""Finite sequential parameter sweeps over the shared trial execution engine."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from itertools import product
from typing import Literal

from ._evidence import has_observed_evidence
from .analysis import (
    AnalysisTrial,
    ComparisonPolicy,
    CompatibilityAnalysis,
    check_compatibility,
    summary_throughput,
)
from .config import ClientConfig, Protocol
from .intent import RateIntent, resolve_rate
from .reports import plan_result_from_dict, plan_result_to_dict, summary_eligible_v1
from .result import JSONValue, Result
from .trials import (
    PlanBudget,
    PlanResult,
    PreparedPlan,
    TrialPolicy,
    TrialSpec,
    prepare_plan,
    run_plan,
)

_AXES = frozenset(
    {
        "server",
        "port",
        "protocol",
        "duration",
        "parallel",
        "omit",
        "blksize",
        "rate",
        "tos",
        "method",
    }
)
_METHODS = ("forward", "reverse", "bidirectional")
_STATUSES = ("completed", "failed", "incomplete", "exception", "not_run")
type Quality = Literal["complete", "partial", "insufficient_data"]
type Direction = Literal["client_to_server", "server_to_client"]
type Method = Literal["forward", "reverse", "bidirectional"]


def _count(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _axis_value(name, value):
    if name == "method":
        if type(value) is not str or value not in _METHODS:
            raise ValueError("method values must be forward, reverse or bidirectional")
        return value
    if name == "protocol":
        if not isinstance(value, str):
            raise ValueError("protocol axis values must be protocol strings")
        return Protocol(value).value
    if name == "server":
        if type(value) is not str or not value.strip():
            raise ValueError("server axis values must be nonempty strings")
        return value
    if value is None and name in {"rate", "blksize", "tos"}:
        return None
    return _count(value, f"{name} axis value")


@dataclass(frozen=True)
class SweepAxis:
    """One declared finite axis with ordered distinct configuration values."""

    name: str
    values: tuple[JSONValue, ...]

    def __post_init__(self) -> None:
        """Require supported names and finite, typed, distinct values."""
        if type(self.name) is not str or self.name not in _AXES:
            raise ValueError("unsupported sweep axis")
        if type(self.values) is not tuple or not self.values:
            raise ValueError("axis values must be a nonempty finite tuple")
        values = tuple(_axis_value(self.name, value) for value in self.values)
        if len(set(values)) != len(values):
            raise ValueError("axis values must be distinct after normalization")
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class SweepCell:
    """Stable Cartesian identity, selected values and complete admitted settings."""

    cell_id: str
    parameters: dict[str, JSONValue]
    config: ClientConfig
    resolved_config: ClientConfig
    trial_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreparedSweep:
    """Detached axes, recorded final cell order and one shared prepared trial plan."""

    base_config: ClientConfig
    axes: tuple[SweepAxis, ...]
    cells: tuple[SweepCell, ...]
    plan: PreparedPlan
    rate_intent: RateIntent | None
    order: Literal["declared", "randomized"]
    seed: int | None


@dataclass(frozen=True)
class SettingCheck:
    """Expected axis/allocation versus a verified native value, with receipt paths."""

    name: str
    expected: JSONValue
    observed: JSONValue
    state: Literal["matched", "native_default", "mismatch", "unknown"]
    evidence_paths: tuple[str, ...]


@dataclass(frozen=True)
class SweepSample:
    """One measured receiver rate with canonical byte/time evidence."""

    trial_id: str
    bytes: int
    measured_seconds: float
    throughput_bps: float
    evidence_path: str
    eligible_for_cell: bool
    setting_checks: tuple[SettingCheck, ...]


@dataclass(frozen=True)
class SweepExclusion:
    """A retained trial excluded from one cell's measured population."""

    trial_id: str
    reason: str


@dataclass(frozen=True)
class DirectionalSummary:
    """Descriptive receiver samples for one methodology and traffic direction."""

    direction: Direction
    quality: Quality
    samples: tuple[SweepSample, ...]
    median_throughput_bps: float | None
    exclusions: tuple[SweepExclusion, ...]
    observation: Literal["receiver"] = "receiver"


@dataclass(frozen=True)
class CellSummary:
    """Every cell retains execution counts and separate directional measurements."""

    cell_id: str
    method: Method
    execution_counts: dict[str, int]
    directions: tuple[DirectionalSummary, ...]


@dataclass(frozen=True)
class SweepComparison:
    """Explicit compatibility within one method/direction group, without a winner."""

    method: Method
    direction: Direction
    cell_ids: tuple[str, ...]
    quality: Quality
    reasons: tuple[str, ...]
    compatibility: CompatibilityAnalysis


@dataclass(frozen=True)
class SweepResult:
    """Complete sweep execution, per-cell summaries and optional explicit comparisons."""

    prepared: PreparedSweep
    execution: PlanResult
    minimum_valid_trials: int
    cells: tuple[CellSummary, ...]
    comparison_policy: ComparisonPolicy | None
    comparisons: tuple[SweepComparison, ...]


def _exact_equal(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def _prepare_v1(
    base_config, axes, *, policy, budget, rate_intent, order, seed, recorded_order=None
):
    if not isinstance(base_config, ClientConfig):
        raise ValueError("base_config must be a ClientConfig")
    base = replace(base_config)
    base = replace(base, server=str(base.server))
    if not isinstance(policy, TrialPolicy) or not isinstance(budget, PlanBudget):
        raise ValueError("policy and budget must be explicit TrialPolicy and PlanBudget")
    policy, budget = replace(policy), replace(budget)
    if budget.max_active_seconds is None:
        raise ValueError("sweeps require a finite max_active_seconds budget")
    if not isinstance(axes, Sequence) or isinstance(axes, (str, bytes)) or not axes:
        raise ValueError("axes must be a nonempty finite sequence")
    if any(not isinstance(axis, SweepAxis) for axis in axes):
        raise ValueError("axes must contain SweepAxis values")
    axes = tuple(replace(axis) for axis in axes)
    names = [axis.name for axis in axes]
    if len(set(names)) != len(names):
        raise ValueError("axis names must be distinct")
    if order not in ("declared", "randomized"):
        raise ValueError("order must be declared or randomized")
    if order == "declared" and seed is not None:
        raise ValueError("declared order does not accept a random seed")
    if order == "randomized":
        _count(seed, "seed")
    if rate_intent is not None and not isinstance(rate_intent, RateIntent):
        raise ValueError("rate_intent must be a RateIntent or None")
    intent = replace(rate_intent) if rate_intent is not None else None
    if intent is not None and "rate" in names:
        raise ValueError("a rate axis cannot be combined with base rate intent")
    cell_count = math.prod(len(axis.values) for axis in axes)
    if cell_count * (policy.warmup_runs + policy.repetitions) > policy.max_trials:
        raise ValueError("sweep product including warm-up and repetitions exceeds max_trials")
    declared = []
    for index, values in enumerate(product(*(axis.values for axis in axes))):
        parameters = dict(zip(names, values, strict=True))
        changes = dict(parameters)
        method = changes.pop("method", None)
        if method is not None:
            changes.update(reverse=method == "reverse", bidirectional=method == "bidirectional")
        config = replace(base, **changes)
        resolved = replace(config, rate=resolve_rate(config, intent).native_per_stream_bps)
        cell_id = f"cell-{index:04d}"
        trial_ids = tuple(
            f"{cell_id}:{phase}:{i}"
            for phase, count in (("warmup", policy.warmup_runs), ("measured", policy.repetitions))
            for i in range(count)
        )
        declared.append(SweepCell(cell_id, parameters, config, resolved, trial_ids))
    if recorded_order is not None:
        if (
            type(recorded_order) is not tuple
            or len(recorded_order) != len(declared)
            or set(recorded_order) != {cell.cell_id for cell in declared}
        ):
            raise ValueError("recorded cell order must contain every cell exactly once")
        if order == "declared" and recorded_order != tuple(cell.cell_id for cell in declared):
            raise ValueError("declared cell order cannot be rearranged")
        by_id = {cell.cell_id: cell for cell in declared}
        cells = [by_id[identity] for identity in recorded_order]
    else:
        cells = declared
        if order == "randomized":
            random.Random(seed).shuffle(cells)
    specs = tuple(
        TrialSpec(f"{cell.cell_id}:{phase}:{i}", cell.cell_id, phase, i, cell.config, intent)
        for cell in cells
        for phase, count in (("warmup", policy.warmup_runs), ("measured", policy.repetitions))
        for i in range(count)
    )
    plan = prepare_plan(specs, policy=policy, budget=budget, order_seed=seed)
    return PreparedSweep(base, axes, tuple(cells), plan, intent, order, seed)


def prepare_sweep(
    base_config: ClientConfig,
    axes: Sequence[SweepAxis],
    *,
    policy: TrialPolicy,
    budget: PlanBudget,
    rate_intent: RateIntent | None = None,
    order: Literal["declared", "randomized"] = "declared",
    seed: int | None = None,
) -> PreparedSweep:
    """Preflight a bounded Cartesian plan before materialization or native traffic.

    The active-duration estimate includes omit time and every warm-up run. Pauses
    are separate. A finite payload budget rejects unknown/unlimited estimates;
    None explicitly permits them. Neither budget is a hard native traffic limit.
    """
    return _prepare_v1(
        base_config,
        axes,
        policy=policy,
        budget=budget,
        rate_intent=rate_intent,
        order=order,
        seed=seed,
    )


def _validated_prepared(prepared):
    if not isinstance(prepared, PreparedSweep):
        raise ValueError("prepared must be a PreparedSweep")
    expected = _prepare_v1(
        prepared.base_config,
        prepared.axes,
        policy=prepared.plan.policy,
        budget=prepared.plan.budget,
        rate_intent=prepared.rate_intent,
        order=prepared.order,
        seed=prepared.seed,
        recorded_order=tuple(cell.cell_id for cell in prepared.cells),
    )
    if not _exact_equal(asdict(expected), asdict(prepared)):
        raise ValueError("prepared sweep does not match its admitted axes, settings or order")
    return expected


def _method(config) -> Method:
    return "bidirectional" if config.bidirectional else "reverse" if config.reverse else "forward"


def _directions(method) -> tuple[Direction, ...]:
    return (
        ("client_to_server", "server_to_client")
        if method == "bidirectional"
        else ("server_to_client",)
        if method == "reverse"
        else ("client_to_server",)
    )


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (
        ordered[middle]
        if len(ordered) % 2
        else ordered[middle - 1] + (ordered[middle] - ordered[middle - 1]) / 2
    )


def _setting_checks(result, cell, intent):
    expected = {}
    for name in cell.parameters:
        if name == "method":
            expected.update(reverse=cell.config.reverse, bidirectional=cell.config.bidirectional)
        else:
            value = getattr(cell.config, name)
            expected[name] = value.value if isinstance(value, Protocol) else value
    if intent is not None:
        expected["rate"] = cell.resolved_config.rate
        if intent.aggregate_bps_per_direction is not None:
            expected["parallel"] = cell.resolved_config.parallel
    checks = []
    effective = result.execution.configuration.effective if result.execution else {}
    for name, wanted in sorted(expected.items()):
        setting = effective.get(name)
        verified = (
            setting is not None
            and setting.state == "verified"
            and setting.value is not None
            and has_observed_evidence(result, setting.evidence_paths)
        )
        observed = setting.value if verified else None
        state = (
            "unknown"
            if not verified
            else "native_default"
            if wanted is None
            else "matched"
            if _exact_equal(wanted, observed)
            else "mismatch"
        )
        checks.append(
            SettingCheck(
                name,
                wanted,
                observed,
                state,
                tuple(setting.evidence_paths) if setting is not None else (),
            )
        )
    return tuple(checks)


def _sample_v1(record, direction, method, cell, intent, *, current_analysis):
    if record.spec.phase == "warmup":
        return None, "warmup_run"
    if record.status != "completed" or record.artifact is None:
        return None, record.status
    result = record.artifact.result
    if result.execution is None or result.execution.method != method:
        return None, "method_mismatch"
    if result.protocol != record.spec.config.protocol.value:
        return None, "protocol_mismatch"
    if not summary_eligible_v1(record.artifact, direction=direction, observation="receiver"):
        return None, "receiver_measurement_unavailable"
    matches = [(i, flow) for i, flow in enumerate(result.flows) if flow.direction == direction]
    if len(matches) != 1 or matches[0][1].receiver is None:
        return None, "receiver_measurement_unavailable"
    index, flow = matches[0]
    stats = flow.receiver
    if (
        stats.bytes is None
        or stats.duration_seconds is None
        or stats.duration_seconds <= 0
        or stats.omitted is True
    ):
        return None, "receiver_measurement_unavailable"
    if current_analysis:
        analysis = summary_throughput(result, direction=direction, observation="receiver")
        rate = analysis.throughput_bps
    else:
        try:
            rate = 8 * stats.bytes / stats.duration_seconds
        except OverflowError as exc:
            raise ValueError("stored sweep rate exceeds finite range") from exc
    if rate is None or not math.isfinite(rate):
        raise ValueError("sweep throughput must be finite")
    checks = _setting_checks(result, cell, intent)
    eligible = all(check.state in {"matched", "native_default"} for check in checks)
    return SweepSample(
        record.spec.trial_id,
        stats.bytes,
        stats.duration_seconds,
        rate,
        f"/flows/{index}/receiver",
        eligible,
        checks,
    ), None


def _cell_summaries_v1(prepared, execution, minimum_valid_trials, *, current_analysis=False):
    _count(minimum_valid_trials, "minimum_valid_trials", 1)
    by_cell = {cell.cell_id: [] for cell in prepared.cells}
    for record in execution.trials:
        by_cell[record.spec.cell_id].append(record)
    summaries = []
    for cell in prepared.cells:
        records = by_cell[cell.cell_id]
        counts = {
            status: sum(record.status == status for record in records) for status in _STATUSES
        }
        method = _method(cell.config)
        directions = []
        for direction in _directions(method):
            samples, exclusions = [], []
            for record in records:
                sample, reason = _sample_v1(
                    record,
                    direction,
                    method,
                    cell,
                    prepared.rate_intent,
                    current_analysis=current_analysis,
                )
                if sample is None:
                    exclusions.append(SweepExclusion(record.spec.trial_id, reason))
                else:
                    samples.append(sample)
            eligible = [sample for sample in samples if sample.eligible_for_cell]
            quality = (
                "insufficient_data"
                if len(eligible) < minimum_valid_trials
                else "partial"
                if any(exclusion.reason != "warmup_run" for exclusion in exclusions)
                or counts["completed"] != len(records)
                or len(eligible) != len(samples)
                else "complete"
            )
            directions.append(
                DirectionalSummary(
                    direction,
                    quality,
                    tuple(samples),
                    _median([sample.throughput_bps for sample in eligible]),
                    tuple(exclusions),
                )
            )
        summaries.append(CellSummary(cell.cell_id, method, counts, tuple(directions)))
    return tuple(summaries)


def _comparison_groups(cells):
    groups = {}
    for cell in cells:
        for summary in cell.directions:
            groups.setdefault((cell.method, summary.direction), []).append((cell.cell_id, summary))
    return groups


def _comparison_policy(prepared, policy):
    if not isinstance(policy, ComparisonPolicy):
        raise ValueError("comparison_policy must be a ComparisonPolicy")
    names = {axis.name for axis in prepared.axes}
    if set(policy.varying_fields) - names:
        raise ValueError("varying comparison fields must be declared sweep axes")
    return replace(
        policy,
        varying_fields=tuple(sorted(names)),
        allowed_differences=dict(policy.allowed_differences),
    )


def _comparison_quality(group, compatibility):
    reasons = []
    if len(group) < 2:
        reasons.append("fewer_than_two_cells")
    if any(summary.quality == "insufficient_data" for _, summary in group):
        reasons.append("insufficient_cell_measurements")
    if not compatibility.compatible:
        reasons.append("incompatible_or_unknown_evidence")
    quality = (
        "insufficient_data"
        if reasons
        else "partial"
        if compatibility.quality == "partial"
        or any(summary.quality == "partial" for _, summary in group)
        else "complete"
    )
    return quality, tuple(reasons)


def summarize_sweep(
    prepared: PreparedSweep,
    execution: PlanResult,
    *,
    minimum_valid_trials: int = 1,
    comparison_policy: ComparisonPolicy | None = None,
) -> SweepResult:
    """Describe every retained cell; comparisons require an explicit evidence policy."""
    prepared = _validated_prepared(prepared)
    execution = plan_result_from_dict(plan_result_to_dict(execution))
    if not _exact_equal(asdict(prepared.plan), asdict(execution.plan)):
        raise ValueError("execution plan does not match the prepared sweep")
    cells = _cell_summaries_v1(prepared, execution, minimum_valid_trials, current_analysis=True)
    comparisons = []
    if comparison_policy is not None:
        effective = _comparison_policy(prepared, comparison_policy)
        artifacts = {
            record.spec.trial_id: record.artifact
            for record in execution.trials
            if record.artifact is not None
        }
        for (method, direction), group in _comparison_groups(cells).items():
            trials = [
                AnalysisTrial(sample.trial_id, artifacts[sample.trial_id].result)
                for _, summary in group
                for sample in summary.samples
                if sample.eligible_for_cell
            ]
            comparison = check_compatibility(trials, policy=effective)
            quality, reasons = _comparison_quality(group, comparison)
            comparisons.append(
                SweepComparison(
                    method,
                    direction,
                    tuple(identity for identity, _ in group),
                    quality,
                    reasons,
                    comparison,
                )
            )
        comparison_policy = replace(
            comparison_policy, allowed_differences=dict(comparison_policy.allowed_differences)
        )
    return SweepResult(
        prepared, execution, minimum_valid_trials, cells, comparison_policy, tuple(comparisons)
    )


def run_sweep(
    prepared: PreparedSweep,
    *,
    executor: Callable[[TrialSpec], Result] | None = None,
    minimum_valid_trials: int = 1,
    comparison_policy: ComparisonPolicy | None = None,
) -> SweepResult:
    """Execute sequentially with one budget and preserve all unstarted or failed cells."""
    admitted = _validated_prepared(prepared)
    _count(minimum_valid_trials, "minimum_valid_trials", 1)
    if comparison_policy is not None:
        comparison_policy = replace(
            comparison_policy, allowed_differences=dict(comparison_policy.allowed_differences)
        )
        _comparison_policy(admitted, comparison_policy)
    execution = run_plan(admitted.plan, executor=executor)
    return summarize_sweep(
        admitted,
        execution,
        minimum_valid_trials=minimum_valid_trials,
        comparison_policy=comparison_policy,
    )
