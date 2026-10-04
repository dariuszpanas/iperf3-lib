"""Whole-experiment execution, cooldown, mutation and failure ownership tests."""

from dataclasses import replace

import pytest
from sweep_helpers import Clock
from test_adaptive import synthetic_result

from iperf3_lib import adaptive_execution, trials
from iperf3_lib.adaptive import AdaptiveUDPPolicy, prepare_adaptive_udp
from iperf3_lib.adaptive_execution import run_adaptive_udp
from iperf3_lib.config import ClientConfig
from iperf3_lib.trials import PlanBudget, TrialPolicy


def preparation(*, max_trials=18, stop_on_error=False):
    """Admit a small declared experiment with visible warmups and cooldowns."""
    return prepare_adaptive_udp(
        ClientConfig("127.0.0.1", protocol="udp", duration=1, blksize=1200),
        (1_000_000, 2_000_000),
        policy=TrialPolicy(
            repetitions=2,
            warmup_runs=1,
            pause_seconds=0.25,
            max_trials=max_trials,
            stop_on_error=stop_on_error,
        ),
        budget=PlanBudget(18, 8_000_000),
        adaptive_policy=AdaptiveUDPPolicy(1_000_000, 2_000_000, 2, 0, 1, 2, 0.95),
    )


def install_clock(monkeypatch):
    """Share a deterministic clock across native-call and batch boundaries."""
    clock = Clock()
    clock.install(monkeypatch)
    monkeypatch.setattr(adaptive_execution, "time", trials.time)
    return clock


def test_runner_records_cooldown_across_batch_boundary(monkeypatch):
    """Every consecutive admitted call has exactly one global cooldown."""
    clock = install_clock(monkeypatch)
    calls = []

    def execute(spec):
        calls.append((spec.trial_id, clock.elapsed))
        clock.elapsed += 1
        return synthetic_result(spec)

    result = run_adaptive_udp(preparation(), executor=execute)
    assert len(calls) == 9
    assert len(result.batches) == 2
    assert [item.pause_before_seconds for item in result.batches] == [0, 0.25]
    assert clock.pauses == [0.25] * 8
    assert result.observed_pause_seconds == 2
    assert [started for _, started in calls] == [index * 1.25 for index in range(9)]
    assert result.highest_eligible_bps == 2_000_000
    assert result.ceiling_censored


def test_executor_mutations_cannot_change_later_batches(monkeypatch):
    """Caller and executor aliases cannot rewrite an already admitted experiment."""
    install_clock(monkeypatch)
    original = preparation()

    def execute(spec):
        result = synthetic_result(spec)
        original.config.server = "changed.invalid"
        spec.config.server = "executor.invalid"
        return result

    result = run_adaptive_udp(original, executor=execute)
    assert result.prepared.config.server == "127.0.0.1"
    assert result.highest_eligible_bps == 2_000_000
    assert all(
        record.spec.config.server == "127.0.0.1"
        for batch in result.batches
        for record in batch.execution.trials
    )


def test_stop_on_error_retains_unstarted_grid_and_prevents_later_batches(monkeypatch):
    """A failed initial native call cannot be retried or followed by confirmation."""
    clock = install_clock(monkeypatch)
    calls = []

    def execute(spec):
        calls.append(spec.trial_id)
        raise RuntimeError("controlled native transport failure")

    result = run_adaptive_udp(preparation(stop_on_error=True), executor=execute)
    assert len(calls) == 1
    assert len(result.batches) == 1
    assert [record.status for record in result.batches[0].execution.trials] == [
        "exception",
        "not_run",
        "not_run",
        "not_run",
        "not_run",
        "not_run",
    ]
    assert result.decision.reason == "execution_stopped"
    assert result.outcome == "inconclusive"
    assert clock.pauses == []


def test_confirmation_failure_is_retained_when_lower_candidate_is_selected(monkeypatch):
    """A failed high confirmation remains visible after a lower rate qualifies."""
    install_clock(monkeypatch)

    def execute(spec):
        if spec.trial_id.startswith("batch-0001") and spec.phase == "measured":
            raise RuntimeError("confirmation failed")
        return synthetic_result(spec)

    result = run_adaptive_udp(preparation(), executor=execute)
    assert len(result.batches) == 3
    assert result.highest_eligible_bps == 1_000_000
    high = next(item for item in result.summaries if item.rate_bps == 2_000_000)
    assert high.status == "inconclusive"
    assert sum(item.execution_status == "exception" for item in high.observations) == 2
    assert [item.batch.rates for item in result.batches[1:]] == [(2_000_000,), (1_000_000,)]


def test_exhausted_budget_stops_before_confirmation_or_extra_cooldown(monkeypatch):
    """Preliminary successes cannot bypass confirmation admission limits."""
    clock = install_clock(monkeypatch)
    result = run_adaptive_udp(preparation(max_trials=6), executor=synthetic_result)
    assert len(result.batches) == 1
    assert result.outcome == "inconclusive"
    assert result.highest_eligible_bps is None
    assert result.decision.reason == "trial_count_limit"
    assert clock.pauses == [0.25] * 5


def test_unrepresentable_measurement_retains_prior_batches_and_raw_result(monkeypatch):
    """A numeric evidence failure does not discard a completed experiment history."""
    install_clock(monkeypatch)

    def execute(spec):
        result = synthetic_result(spec)
        if spec.trial_id.startswith("batch-0001") and spec.phase == "measured":
            result.flows[0].sender.bytes = 10**400
        return result

    result = run_adaptive_udp(preparation(), executor=execute)
    assert len(result.batches) == 3
    assert result.highest_eligible_bps == 1_000_000
    high = next(item for item in result.summaries if item.rate_bps == 2_000_000)
    assert high.status == "inconclusive"
    assert sum(item.sender_bps is None for item in high.observations) == 2
    assert [
        record.artifact.result.flows[0].sender.bytes
        for record in result.batches[1].execution.trials
        if record.spec.phase == "measured"
    ] == [10**400] * 2


def test_invalid_runner_inputs_fail_before_execution():
    """Revalidate mutable prepared state before any caller-owned transport runs."""
    with pytest.raises(ValueError, match="executor"):
        run_adaptive_udp(preparation(), executor=object())
    broken = replace(preparation(), config=ClientConfig("127.0.0.1"))
    with pytest.raises(ValueError, match="UDP"):
        run_adaptive_udp(broken, executor=lambda _: pytest.fail("must not execute"))
