"""Bounded native repeated plans retain settings and bytes/time evidence."""

import pytest

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.assessments import AssessmentPolicy, assess_plan, ci_exit_code
from iperf3_lib.config import ClientConfig
from iperf3_lib.reports import dumps_report, loads_report
from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials, run_plan

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("reverse", [False, True])
def test_native_repeated_plan_retains_returned_settings_and_report(iperf3_server, reverse):
    """Qualify real forward/reverse warm-up and measured runs at bounded target rates."""
    host, port = iperf3_server
    config = ClientConfig(host, port=port, duration=1, rate=500000, blksize=4096, reverse=reverse)
    plan = prepare_trials(
        config, policy=TrialPolicy(repetitions=2, warmup_runs=1), budget=PlanBudget(3, 187500)
    )
    execution = run_plan(plan)
    assert execution.execution_success, execution
    assert len(execution.trials) == 3
    direction = "server_to_client" if reverse else "client_to_server"
    for trial in execution.trials:
        result = trial.artifact.result
        raw = result.raw["start"]["test_start"]
        assert raw["reverse"] == int(reverse)
        assert raw["duration"] == 1 and raw["num_streams"] == 1
        assert raw["target_bitrate"] == 500000 and raw["blksize"] == 4096
        assert result.execution.configuration.requested["rate"] == 500000
        flow = next(flow for flow in result.flows if flow.direction == direction)
        assert flow.receiver.bytes > 0 and flow.receiver.duration_seconds > 0
        assert result.execution.timing.completed_at_seconds is not None
    report = assess_plan(
        execution,
        policy=AssessmentPolicy(
            direction=direction, minimum_valid_trials=2, minimum_throughput_bps=1
        ),
        comparison=ComparisonPolicy("native-repeated", ("client", "server")),
    )
    assert report.assessment.outcome == "pass", report.assessment
    assert len(report.assessment.measurements) == 2
    assert report.assessment.exclusions[0].reason == "warmup_run"
    assert ci_exit_code(report) == 0
    assert loads_report(dumps_report(report)) == report
