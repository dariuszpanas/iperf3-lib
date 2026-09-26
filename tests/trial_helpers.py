"""Assessment fixtures use synthetic measurements with genuine native setting receipts."""

import json
from pathlib import Path

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.assessments import AssessmentPolicy, assess_plan
from iperf3_lib.config import ClientConfig
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials, run_plan

FIXTURE = Path(__file__).parent / "fixtures/native/3.21/tcp-forward-client.json"


def measured(byte_count=100, seconds=1, *, parallel=2):
    """Change summary samples explicitly; never claim these variants are captures."""
    raw = json.loads(FIXTURE.read_text())
    raw["start"]["test_start"]["num_streams"] = parallel
    for name in ("sum_sent", "sum_received"):
        raw["end"][name]["bytes"] = byte_count
        raw["end"][name]["seconds"] = seconds
        raw["end"][name]["bits_per_second"] = 99999999
    return result_from_iperf_json(raw)


def history(results, *, warmup_runs=0, stop_on_error=False, cell_id="default"):
    """Run finite fake results through the real common runner."""
    results = tuple(results)
    plan = prepare_trials(
        ClientConfig("127.0.0.1", duration=1, parallel=2, rate=4000000),
        policy=TrialPolicy(
            repetitions=len(results) - warmup_runs,
            warmup_runs=warmup_runs,
            stop_on_error=stop_on_error,
        ),
        budget=PlanBudget(100, 100000000),
        cell_id=cell_id,
    )
    pending = iter(results)

    def execute(spec):
        result = next(pending)
        if isinstance(result, Exception):
            raise result
        return result

    return run_plan(plan, executor=execute)


def report(results=None, *, policy=None, baselines=None, comparison=None, **options):
    """Build a complete assessment with native-verified comparison settings."""
    return assess_plan(
        history(results if results is not None else [measured() for _ in range(3)], **options),
        policy=policy or AssessmentPolicy(minimum_throughput_bps=800),
        comparison=comparison or ComparisonPolicy("loopback", ("client", "server")),
        baselines=baselines,
    )
