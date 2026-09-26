"""Evidence-preserving median assessments separate from trial execution success."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .analysis import (
    AnalysisDiagnostic,
    AnalysisTrial,
    ComparisonPolicy,
    CompatibilityAnalysis,
    ThroughputAnalysis,
    check_compatibility,
    summary_throughput,
)
from .artifacts import ResultArtifact, artifact_from_dict, artifact_to_dict
from .trials import PlanResult

ASSESSMENT_ALGORITHM: Literal["median-summary-v1"] = "median-summary-v1"


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class AssessmentPolicy:
    """Inclusive receiver-throughput criteria with explicit sample and tolerance rules.

    Absolute tolerance is subtracted from the stricter of the absolute minimum
    and baseline median times (1 - relative tolerance), with a floor of zero.
    Relative tolerance applies only to a supplied baseline. Rates use bits/s.
    """

    direction: Literal["client_to_server", "server_to_client"] = "client_to_server"
    observation: Literal["sender", "receiver"] = "receiver"
    minimum_valid_trials: int = 3
    minimum_valid_baselines: int = 1
    minimum_throughput_bps: float | None = None
    absolute_tolerance_bps: float = 0
    relative_tolerance: float = 0

    def __post_init__(self) -> None:
        """Reject ambiguous selections, invalid sample counts and nonfinite thresholds."""
        if self.direction not in ("client_to_server", "server_to_client"):
            raise ValueError("invalid assessment direction")
        if self.observation not in ("sender", "receiver"):
            raise ValueError("invalid assessment observation")
        for name in ("minimum_valid_trials", "minimum_valid_baselines"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("minimum_throughput_bps", "absolute_tolerance_bps", "relative_tolerance"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, name)
            elif name != "minimum_throughput_bps":
                raise ValueError(f"{name} cannot be None")
        if self.relative_tolerance > 1:
            raise ValueError("relative_tolerance must be between zero and one")


@dataclass(frozen=True)
class AssessedMeasurement:
    """One retained, explicitly namespaced trial's summary analysis."""

    trial_id: str
    throughput: ThroughputAnalysis


@dataclass(frozen=True)
class ExcludedTrial:
    """A retained trial excluded from the performance population, with its reason."""

    trial_id: str
    reason: str
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class Assessment:
    """A stable v1 median decision with selected measurements and compatibility evidence."""

    outcome: Literal["pass", "fail", "inconclusive"]
    measurements: tuple[AssessedMeasurement, ...]
    baseline_measurements: tuple[AssessedMeasurement, ...]
    exclusions: tuple[ExcludedTrial, ...]
    compatibility: CompatibilityAnalysis
    median_throughput_bps: float | None
    baseline_median_bps: float | None
    effective_minimum_bps: float | None
    relative_change: float | None
    reasons: tuple[str, ...]
    aggregation: Literal["median"] = "median"
    algorithm_revision: Literal["median-summary-v1"] = ASSESSMENT_ALGORITHM


@dataclass(frozen=True)
class AssessmentReport:
    """A full execution, retained baseline artifacts, criteria, and assessment."""

    execution: PlanResult
    policy: AssessmentPolicy
    comparison: ComparisonPolicy
    baselines: tuple[ResultArtifact, ...] | None
    assessment: Assessment
    kind: Literal["iperf3-lib.assessment"] = "iperf3-lib.assessment"
    schema_version: int = 1


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    lower, upper = ordered[middle - 1 : middle + 1]
    return lower + (upper - lower) / 2


def _decision_v1(policy, measurements, baseline_measurements, baseline_requested, compatibility):
    """Apply frozen report-v1 aggregation/threshold rules to already retained analysis."""
    current = _median([item.throughput.throughput_bps for item in measurements])
    baseline = _median([item.throughput.throughput_bps for item in baseline_measurements])
    reasons = []
    if len(measurements) < policy.minimum_valid_trials:
        reasons.append("insufficient_valid_trials")
    if baseline_requested and len(baseline_measurements) < policy.minimum_valid_baselines:
        reasons.append("insufficient_valid_baselines")
    if not compatibility.compatible:
        reasons.append("incompatible_or_unknown_methodology")
    if not baseline_requested and policy.minimum_throughput_bps is None:
        reasons.append("no_acceptance_criterion")
    if not baseline_requested and policy.relative_tolerance:
        reasons.append("relative_tolerance_requires_baseline")
    thresholds = []
    if policy.minimum_throughput_bps is not None:
        thresholds.append(policy.minimum_throughput_bps)
    if baseline_requested and baseline is not None:
        thresholds.append(baseline * (1 - policy.relative_tolerance))
    threshold = max(0, max(thresholds) - policy.absolute_tolerance_bps) if thresholds else None
    relative = None
    if current is not None and baseline is not None and baseline > 0:
        change = (current - baseline) / baseline
        if math.isfinite(change):
            relative = change
    if reasons:
        outcome = "inconclusive"
    elif current is not None and threshold is not None:
        outcome = "pass" if current >= threshold else "fail"
        reasons.append("criteria_satisfied" if outcome == "pass" else "below_required_throughput")
    else:
        outcome = "inconclusive"
        reasons.append("measurement_unavailable")
    if baseline == 0:
        reasons.append("zero_baseline_relative_change_unavailable")
    return outcome, current, baseline, threshold, relative, tuple(reasons)


def assess_plan(
    execution: PlanResult,
    *,
    policy: AssessmentPolicy,
    comparison: ComparisonPolicy,
    baselines: Sequence[ResultArtifact] | None = None,
) -> AssessmentReport:
    """Assess retained measured trials, preserving exclusions and independent execution status.

    Failed/incomplete/warm-up runs remain in the report but never become valid
    throughput samples. Baselines and candidate measurements must share actual
    compatible settings according to the public analysis checker. An empty
    explicitly supplied baseline is inconclusive rather than an absent criterion.
    """
    if not isinstance(execution, PlanResult):
        raise ValueError("execution must be a PlanResult")
    if not isinstance(policy, AssessmentPolicy) or not isinstance(comparison, ComparisonPolicy):
        raise ValueError("policy and comparison must be explicit assessment/comparison policies")
    if baselines is not None and (
        not isinstance(baselines, Sequence) or isinstance(baselines, (str, bytes))
    ):
        raise ValueError("baselines must be a finite sequence of result artifacts")
    policy = replace(policy)
    comparison = replace(comparison, allowed_differences=dict(comparison.allowed_differences))
    # Reuse the versioned execution contract to validate mutable nested evidence
    # before selecting samples. Import here to keep the model/codec dependency acyclic.
    from .reports import plan_result_from_dict, plan_result_to_dict

    execution = plan_result_from_dict(plan_result_to_dict(execution))
    retained_baselines = (
        tuple(artifact_from_dict(artifact_to_dict(artifact)) for artifact in baselines)
        if baselines is not None
        else None
    )
    measurements = []
    baseline_measurements = []
    exclusions = []
    comparable = []

    def analyze(identifier, artifact, target):
        analysis = summary_throughput(
            artifact.result, direction=policy.direction, observation=policy.observation
        )
        if analysis.quality != "complete" or analysis.throughput_bps is None:
            exclusions.append(
                ExcludedTrial(identifier, "insufficient_summary_measurement", analysis.diagnostics)
            )
            return
        target.append(AssessedMeasurement(identifier, analysis))
        comparable.append(AnalysisTrial(identifier, artifact.result))

    for record in execution.trials:
        identifier = f"trial:{record.spec.trial_id}"
        if record.spec.phase == "warmup":
            exclusions.append(ExcludedTrial(identifier, "warmup_run"))
        elif record.status != "completed" or record.artifact is None:
            exclusions.append(ExcludedTrial(identifier, f"execution_{record.status}"))
        else:
            analyze(identifier, record.artifact, measurements)
    for index, artifact in enumerate(retained_baselines or ()):
        analyze(f"baseline:{index}", artifact, baseline_measurements)
    compatible = check_compatibility(comparable, policy=comparison)
    outcome, current, baseline, threshold, relative, reasons = _decision_v1(
        policy, measurements, baseline_measurements, retained_baselines is not None, compatible
    )
    assessment = Assessment(
        outcome,
        tuple(measurements),
        tuple(baseline_measurements),
        tuple(exclusions),
        compatible,
        current,
        baseline,
        threshold,
        relative,
        reasons,
    )
    return AssessmentReport(execution, policy, comparison, retained_baselines, assessment)


def ci_exit_code(report: AssessmentReport) -> int:
    """Return 2 for execution failure, then 1 rejection, 3 incomplete/inconclusive, else 0.

    Actual failed/exception trials, including warm-up, take precedence. A known
    performance rejection outranks partial execution. Incomplete or unstarted
    trials prevent success even when the available performance samples pass.
    This is pure classification and never exits the process.
    """
    if not isinstance(report, AssessmentReport):
        raise ValueError("report must be an AssessmentReport")
    if any(trial.status in ("failed", "exception") for trial in report.execution.trials):
        return 2
    if report.assessment.outcome == "fail":
        return 1
    if report.assessment.outcome == "inconclusive" or not report.execution.execution_success:
        return 3
    return 0
