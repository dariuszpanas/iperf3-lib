"""Bounded native sweep qualification uses returned JSON for every admitted setting."""

import pytest

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import RateIntent
from iperf3_lib.sweep_reports import dumps_sweep_report, loads_sweep_report, report_from_sweep
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy

pytestmark = pytest.mark.integration


def test_native_sweep_retains_each_cell_direction_allocation_and_artifact(iperf3_server):
    """Verify stream/method axes, warm-up retention, allocation and actual receiver bytes/time."""
    host, port = iperf3_server
    plan = prepare_sweep(
        ClientConfig(host, port=port, duration=1, blksize=4096),
        (
            SweepAxis("parallel", (1, 2)),
            SweepAxis("method", ("forward", "reverse", "bidirectional")),
        ),
        rate_intent=RateIntent(aggregate_bps_per_direction=500001),
        policy=TrialPolicy(repetitions=1, warmup_runs=1),
        budget=PlanBudget(max_active_seconds=12, max_payload_bytes=2000000),
        order="randomized",
        seed=31,
    )
    sweep = run_sweep(
        plan, comparison_policy=ComparisonPolicy("native-sweep", ("client", "server"))
    )
    assert sweep.execution.execution_success, sweep.execution
    assert len(sweep.cells) == 6 and len(sweep.execution.trials) == 12
    assert sweep.prepared.cells == plan.cells
    assert len(sweep.comparisons) == 4
    assert all(group.quality == "complete" for group in sweep.comparisons), sweep.comparisons
    records = {record.spec.trial_id: record for record in sweep.execution.trials}
    for record in records.values():
        result = record.artifact.result
        start = result.raw["start"]["test_start"]
        config = record.spec.resolved_config
        assert start["num_streams"] == config.parallel
        assert start["reverse"] == int(config.reverse)
        assert start["bidir"] == int(config.bidirectional)
        assert start["target_bitrate"] == 500001 // config.parallel
        assert start["duration"] == 1 and start["blksize"] == 4096
        assert result.execution.configuration.requested["rate"] == config.rate
        allocation = result.extensions["iperf3_lib.rate_intent"]["resolution"]
        assert allocation["native_per_stream_bps"] == config.rate
        assert allocation["unused_bps_per_direction"] == 500001 % config.parallel
    for cell in sweep.cells:
        assert cell.execution_counts["completed"] == 2
        for summary in cell.directions:
            assert summary.quality == "complete"
            assert len(summary.samples) == len(summary.exclusions) == 1
            assert summary.exclusions[0].reason == "warmup_run"
            sample = summary.samples[0]
            assert sample.eligible_for_cell
            assert all(check.state == "matched" for check in sample.setting_checks)
            result = records[sample.trial_id].artifact.result
            suffix = (
                "_bidir_reverse"
                if cell.method == "bidirectional" and summary.direction == "server_to_client"
                else ""
            )
            native = result.raw["end"][f"sum_received{suffix}"]
            assert sample.bytes == native["bytes"] > 0
            assert sample.measured_seconds == native["seconds"] > 0
            assert sample.throughput_bps == 8 * native["bytes"] / native["seconds"]
            assert summary.median_throughput_bps == sample.throughput_bps
    report = report_from_sweep(sweep)
    assert loads_sweep_report(dumps_sweep_report(report)) == report
