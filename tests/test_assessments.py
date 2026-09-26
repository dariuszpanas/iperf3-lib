"""Median assessments keep execution outcomes and comparison evidence explicit."""

from dataclasses import replace

import pytest
from trial_helpers import history, measured, report

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.artifacts import artifact_from_result
from iperf3_lib.assessments import AssessmentPolicy, assess_plan, ci_exit_code
from iperf3_lib.result import result_from_iperf_json


def test_receiver_median_uses_measured_bytes_and_duration_not_reported_rate():
    """Receiver median uses measured bytes and duration not reported rate."""
    result = report([measured(200, 2), measured(50, 1), measured(1000, 1)])
    assert result.assessment.outcome == "pass"
    assert result.assessment.median_throughput_bps == 800
    assert [m.throughput.throughput_bps for m in result.assessment.measurements] == [800, 400, 8000]
    assert result.assessment.compatibility.compatible
    assert ci_exit_code(result) == 0


def test_even_median_exact_threshold_and_absolute_tolerance():
    """Even median exact threshold and absolute tolerance."""
    result = report(
        [measured(100), measured(200)],
        policy=AssessmentPolicy(
            minimum_valid_trials=2, minimum_throughput_bps=1201, absolute_tolerance_bps=1
        ),
    )
    assert result.assessment.median_throughput_bps == 1200
    assert result.assessment.effective_minimum_bps == 1200
    assert result.assessment.outcome == "pass"
    failed = report(
        [measured(100), measured(200)],
        policy=AssessmentPolicy(
            minimum_valid_trials=2, minimum_throughput_bps=1202, absolute_tolerance_bps=1
        ),
    )
    assert failed.assessment.outcome == "fail" and ci_exit_code(failed) == 1


def test_warmup_failure_is_retained_but_excluded_and_execution_failure_wins():
    """Warmup failure is retained but excluded and execution failure wins."""
    result = report(
        [result_from_iperf_json({"error": "refused"}), measured(), measured(), measured()],
        warmup_runs=1,
    )
    assert result.assessment.outcome == "pass"
    assert len(result.execution.trials) == 4
    assert result.assessment.exclusions[0].reason == "warmup_run"
    assert not result.execution.execution_success and ci_exit_code(result) == 2


def test_incomplete_samples_remain_distinct_and_never_imply_execution_success():
    """Incomplete samples remain distinct and never imply execution success."""
    values = [measured(), result_from_iperf_json({}), measured(), measured()]
    result = report(values)
    assert result.assessment.outcome == "pass" and ci_exit_code(result) == 3
    assert result.assessment.exclusions[0].reason == "execution_incomplete"
    failed = report(values, policy=AssessmentPolicy(minimum_throughput_bps=801))
    assert failed.assessment.outcome == "fail" and ci_exit_code(failed) == 1


def test_minimum_samples_and_stop_records_are_explicit():
    """Minimum samples and stop records are explicit."""
    result = report([measured(), OSError("broken"), measured()], stop_on_error=True)
    assert [t.status for t in result.execution.trials] == ["completed", "exception", "not_run"]
    assert result.assessment.outcome == "inconclusive"
    assert "insufficient_valid_trials" in result.assessment.reasons
    assert ci_exit_code(result) == 2


def test_missing_summary_bytes_does_not_fall_back_to_reported_throughput():
    """Missing summary bytes does not fall back to reported throughput."""
    value = measured()
    value.flows[0].receiver.bytes = None
    value.end.sum_received.bytes = None
    result = report([value, measured(), measured()])
    assert result.assessment.outcome == "inconclusive"
    assert result.assessment.exclusions[0].reason == "insufficient_summary_measurement"
    assert result.assessment.exclusions[0].diagnostics


def test_relative_baseline_tolerance_and_absolute_minimum_combine_explicitly():
    """Relative baseline tolerance and absolute minimum combine explicitly."""
    baseline = artifact_from_result(measured(200))
    policy = AssessmentPolicy(
        minimum_throughput_bps=1300, relative_tolerance=0.25, absolute_tolerance_bps=100
    )
    result = report([measured(150) for _ in range(3)], policy=policy, baselines=[baseline])
    assert result.assessment.baseline_median_bps == 1600
    assert result.assessment.effective_minimum_bps == 1200
    assert result.assessment.relative_change == -0.25
    assert result.assessment.outcome == "pass"
    baseline.result.flows[0].receiver.bytes = 12345
    assert result.baselines[0].result.flows[0].receiver.bytes == 200


def test_zero_baseline_never_divides_and_zero_is_a_real_measurement():
    """Zero baseline never divides and zero is a real measurement."""
    result = report(
        [measured(0) for _ in range(3)],
        policy=AssessmentPolicy(),
        baselines=[artifact_from_result(measured(0))],
    )
    assert result.assessment.outcome == "pass"
    assert result.assessment.median_throughput_bps == 0
    assert result.assessment.relative_change is None
    assert "zero_baseline_relative_change_unavailable" in result.assessment.reasons


@pytest.mark.parametrize(
    "baselines,policy,reason",
    [
        (None, AssessmentPolicy(), "no_acceptance_criterion"),
        ([], AssessmentPolicy(), "insufficient_valid_baselines"),
        (
            None,
            AssessmentPolicy(minimum_throughput_bps=800, relative_tolerance=0.1),
            "relative_tolerance_requires_baseline",
        ),
        (
            [artifact_from_result(measured())],
            AssessmentPolicy(minimum_valid_baselines=2),
            "insufficient_valid_baselines",
        ),
    ],
)
def test_absent_empty_and_insufficient_baselines_are_distinct(baselines, policy, reason):
    """Absent empty and insufficient baselines are distinct."""
    result = report(policy=policy, baselines=baselines)
    assert result.assessment.outcome == "inconclusive" and reason in result.assessment.reasons


def test_baseline_configuration_mismatch_requires_a_retained_reason():
    """Baseline configuration mismatch requires a retained reason."""
    baseline = artifact_from_result(measured(parallel=3))
    result = report(baselines=[baseline])
    assert result.assessment.outcome == "inconclusive"
    allowed = report(
        baselines=[baseline],
        comparison=ComparisonPolicy(
            "loopback",
            ("a", "b"),
            allowed_differences={
                "parallel": "Application intentionally qualifies a changed stream count"
            },
        ),
    )
    assert allowed.assessment.outcome == "pass"
    assert allowed.assessment.compatibility.policy.allowed_differences["parallel"]


def test_unknown_settings_remain_inconclusive_even_when_difference_allowed():
    """Unknown settings remain inconclusive even when difference allowed."""
    unknown = measured()
    unknown.execution.configuration.effective.pop("rate")
    result = report(
        [unknown, measured(), measured()],
        comparison=ComparisonPolicy(
            "loopback", ("a", "b"), allowed_differences={"rate": "Intentional rate comparison"}
        ),
    )
    assert (
        not result.assessment.compatibility.compatible
        and result.assessment.outcome == "inconclusive"
    )


def test_baseline_namespace_cannot_collide_with_caller_trial_ids():
    """Baseline namespace cannot collide with caller trial ids."""
    result = report(baselines=[artifact_from_result(measured())], cell_id="baseline:0")
    identifiers = [
        m.trial_id
        for m in (*result.assessment.measurements, *result.assessment.baseline_measurements)
    ]
    assert len(identifiers) == len(set(identifiers)) == 4
    assert identifiers[-1] == "baseline:0"


@pytest.mark.parametrize(
    "field,value",
    [
        ("direction", "unknown"),
        ("observation", "both"),
        ("minimum_valid_trials", True),
        ("minimum_valid_baselines", 0),
        ("minimum_throughput_bps", -1),
        ("absolute_tolerance_bps", None),
        ("relative_tolerance", 1.01),
        ("relative_tolerance", float("nan")),
        ("minimum_throughput_bps", 10**1000),
    ],
)
def test_invalid_assessment_policy_is_rejected(field, value):
    """Invalid assessment policy is rejected."""
    with pytest.raises(ValueError):
        AssessmentPolicy(**{field: value})


def test_invalid_inputs_and_incomplete_execution_order_are_rejected():
    """Invalid inputs and incomplete execution order are rejected."""
    with pytest.raises(ValueError):
        assess_plan(None, policy=AssessmentPolicy(), comparison=ComparisonPolicy("a", ("b", "c")))
    execution = history([measured()])
    with pytest.raises(ValueError):
        assess_plan(
            replace(execution, trials=()),
            policy=AssessmentPolicy(),
            comparison=ComparisonPolicy("a", ("b", "c")),
        )
    with pytest.raises(ValueError):
        report(baselines=iter([]))
    with pytest.raises(ValueError):
        ci_exit_code(None)


def test_assessment_detaches_execution_and_comparison_settings():
    """Assessment detaches execution and comparison settings."""
    original = history([measured() for _ in range(3)])
    comparison = ComparisonPolicy("a", ("b", "c"), allowed_differences={"parallel": "reason"})
    result = assess_plan(
        original, policy=AssessmentPolicy(minimum_throughput_bps=800), comparison=comparison
    )
    original.trials[0].artifact.result.raw["new"] = "mutation"
    comparison.allowed_differences["rate"] = "later"
    assert "new" not in result.execution.trials[0].artifact.result.raw
    assert "rate" not in result.comparison.allowed_differences
