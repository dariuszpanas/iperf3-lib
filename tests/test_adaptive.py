"""Controlled bounded exploration without monotonic assumptions or hidden failures."""

import copy
from dataclasses import replace

import pytest
from sweep_helpers import Clock, measured

from iperf3_lib.adaptive import (
    AdaptiveBatchResult,
    AdaptiveUDPPolicy,
    _validate_result_v1,
    next_adaptive_batch,
    prepare_adaptive_udp,
    summarize_adaptive_udp,
)
from iperf3_lib.config import ClientConfig
from iperf3_lib.result import VerifiedSetting, result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialPolicy, run_plan


def synthetic_result(spec, *, loss_percent=0, sender_fraction=1, packets=100):
    """Create explicit synthetic native-shaped sender and receiver observations."""
    base = measured(spec)
    raw = base.raw
    requested = spec.rate_intent.aggregate_bps_per_direction
    raw["end"]["sum_sent"].update(bytes=int(requested * sender_fraction / 8), seconds=1)
    raw["end"]["sum_received"].update(
        bytes=int(requested / 8),
        seconds=1,
        packets=packets,
        lost_packets=int(loss_percent),
        lost_percent=loss_percent,
    )
    result = result_from_iperf_json(raw)
    result.extensions = base.extensions
    return result


def prepared(*, rates=(800, 1600), config=None, policy=None, budget=None, **search):
    """Admit a small fixed experiment with all finite budgets stated explicitly."""
    parameters = {
        "min_rate_bps": min(rates),
        "max_rate_bps": max(rates),
        "max_distinct_rates": 6,
        "max_refinement_depth": 2,
        "receiver_loss_percent": 5,
        "minimum_valid_trials": 2,
        "minimum_sender_fraction": 0.9,
    }
    parameters.update(search)
    return prepare_adaptive_udp(
        config or ClientConfig("127.0.0.1", protocol="udp", duration=1, blksize=1200),
        rates,
        policy=policy or TrialPolicy(repetitions=2, max_trials=30),
        budget=budget or PlanBudget(30, 100000),
        adaptive_policy=AdaptiveUDPPolicy(**parameters),
    )


def execute_batch(plan, history=(), executor=synthetic_result):
    """Execute exactly the next admitted batch and retain its full trial history."""
    decision = next_adaptive_batch(plan, history)
    assert decision.batch is not None
    batch = decision.batch
    return (
        *history,
        AdaptiveBatchResult(
            batch,
            run_plan(batch.plan, executor=executor),
            plan.policy.pause_seconds if history else 0,
        ),
    )


def finish(plan, executor=synthetic_result):
    """Run the finite pure planner with deterministic synthetic observations."""
    history = ()
    while next_adaptive_batch(plan, history).batch is not None:
        history = execute_batch(plan, history, executor)
    return summarize_adaptive_udp(plan, history)


def test_all_pass_repeats_only_highest_and_reports_confirmed_ceiling():
    """An all-pass grid does not create differences from confirmation bookkeeping."""
    result = finish(prepared(rates=(1600, 800, 1200)))
    assert [item.batch.rates for item in result.batches] == [(1600, 800, 1200), (1600,)]
    assert result.acceptable_rates_bps == (1600,)
    assert result.highest_eligible_bps == 1600
    assert result.ceiling_censored
    assert result.outcome == "acceptable_tested_rates"
    assert result.decision.reason == "no_differing_adjacent_outcomes"
    assert [value.status for value in result.summaries] == [
        "provisional",
        "provisional",
        "eligible",
    ]
    assert _validate_result_v1(result) == result


def test_nonmonotonic_lower_failure_does_not_erase_higher_pass():
    """A low-rate loss observation never binary-prunes a higher tested success."""
    result = finish(
        prepared(rates=(800, 1600, 2400), max_refinement_depth=0),
        lambda spec: synthetic_result(
            spec, loss_percent=20 if spec.rate_intent.aggregate_bps_per_direction == 1600 else 0
        ),
    )
    assert result.highest_eligible_bps == 2400
    assert [summary.status for summary in result.summaries] == [
        "provisional",
        "rejected",
        "eligible",
    ]
    assert result.decision.reason == "refinement_depth_limit"


def test_refinement_preserves_intervals_and_confirms_promising_midpoint():
    """A mixed grid preserves every point and tests finite integer midpoint batches."""
    result = finish(
        prepared(max_refinement_depth=1),
        lambda spec: synthetic_result(
            spec, loss_percent=10 if spec.rate_intent.aggregate_bps_per_direction > 1200 else 0
        ),
    )
    assert [batch.batch.rates for batch in result.batches] == [
        (800, 1600),
        (800,),
        (1200,),
        (1200,),
    ]
    assert result.acceptable_rates_bps == (800, 1200)
    assert result.highest_eligible_bps == 1200
    assert not result.ceiling_censored
    assert result.decision.reason == "refinement_depth_limit"


def test_failed_confirmation_is_never_retried_until_success():
    """A confirmation loss remains evidence and promotes the next lower candidate."""

    def execute(spec):
        loss = 20 if spec.trial_id.startswith("batch-0001") else 0
        return synthetic_result(spec, loss_percent=loss)

    result = finish(prepared(max_refinement_depth=0), execute)
    assert [batch.batch.rates for batch in result.batches] == [(800, 1600), (1600,), (800,)]
    assert result.highest_eligible_bps == 800
    high = result.summaries[1]
    assert [sample.count_loss_percent for sample in high.observations] == [0, 0, 20, 20]
    assert high.status == "rejected"


@pytest.mark.parametrize(
    "loss,expected", [(5, "acceptable_tested_rates"), (6, "no_acceptable_tested_rate")]
)
def test_exact_loss_boundary_and_all_rejected_outcome(loss, expected):
    """Threshold equality passes and every observed loss failure yields no tested range."""
    result = finish(prepared(), lambda spec: synthetic_result(spec, loss_percent=loss))
    assert result.outcome == expected


@pytest.mark.parametrize(
    "fraction,expected", [(0.9, "acceptable_tested_rates"), (0.89, "inconclusive")]
)
def test_sender_under_delivery_cannot_establish_success(fraction, expected):
    """Low receiver loss at an under-driven target does not qualify the requested load."""
    result = finish(prepared(), lambda spec: synthetic_result(spec, sender_fraction=fraction))
    assert result.outcome == expected
    assert all(
        sample.sender_fraction == fraction
        for summary in result.summaries
        for sample in summary.observations
    )


@pytest.mark.parametrize("packets", [0, None])
def test_unknown_or_zero_packet_counts_cannot_establish_loss(packets):
    """Missing denominator evidence stays unknown, including a native zero-loss claim."""
    result = finish(prepared(), lambda spec: synthetic_result(spec, packets=packets))
    assert result.outcome == "inconclusive"
    assert all(
        sample.count_loss_percent is None and sample.native_loss_percent == 0
        for summary in result.summaries
        for sample in summary.observations
    )


def test_native_loss_is_retained_separately_from_count_derived_loss():
    """Conflicting native percentage remains visible while counts govern acceptance."""

    def execute(spec):
        result = synthetic_result(spec, loss_percent=5)
        result.flows[0].receiver.lost_percent = 90
        return result

    result = finish(prepared(), execute)
    assert result.outcome == "acceptable_tested_rates"
    sample = result.summaries[-1].observations[-1]
    assert sample.count_loss_percent == 5
    assert sample.native_loss_percent == 90


@pytest.mark.parametrize("failure", ["exception", "failed", "incomplete"])
def test_failures_remain_in_every_summary_and_prevent_acceptance(failure):
    """Execution failures are retained alongside earlier passing evidence at the rate."""

    def execute(spec):
        if spec.repetition == 0:
            return synthetic_result(spec)
        if failure == "exception":
            raise RuntimeError("controlled failure")
        return measured(spec, failed=failure == "failed", incomplete=failure == "incomplete")

    result = finish(prepared(), execute)
    assert result.outcome == "inconclusive"
    assert all(
        [sample.execution_status for sample in summary.observations] == ["completed", failure]
        for summary in result.summaries
    )


def test_stop_on_error_retains_unstarted_reservations_without_new_admission():
    """A stopped execution retains its failed prefix and unstarted finite suffix."""
    plan = prepared(policy=TrialPolicy(repetitions=2, max_trials=30, stop_on_error=True))

    def fail(spec):
        raise RuntimeError("controlled failure")

    result = finish(plan, fail)
    assert result.decision.reason == "execution_stopped"
    assert [record.status for record in result.batches[0].execution.trials] == [
        "exception",
        "not_run",
        "not_run",
        "not_run",
    ]


@pytest.mark.parametrize(
    "limit,reason",
    [
        ("trials", "trial_count_limit"),
        ("active", "active_time_limit"),
        ("payload", "payload_limit"),
    ],
)
def test_next_batch_respects_each_whole_experiment_budget(limit, reason):
    """An initial pass without budget for confirmation remains provisional."""
    plan = prepared(
        policy=TrialPolicy(repetitions=2, max_trials=4 if limit == "trials" else 30),
        budget=PlanBudget(4 if limit == "active" else 30, 600 if limit == "payload" else 100000),
    )
    result = finish(plan)
    assert len(result.batches) == 1
    assert result.decision.reason == reason
    assert result.outcome == "inconclusive"
    assert result.highest_eligible_bps is None


def test_budget_estimates_include_warmup_omit_and_aggregate_floor():
    """Budget arithmetic reserves all native active time and rounded aggregate payload."""
    config = ClientConfig("host", protocol="udp", duration=2, omit=1, parallel=2, blksize=1200)
    plan = prepared(
        rates=(801,),
        config=config,
        policy=TrialPolicy(repetitions=2, warmup_runs=1, max_trials=6),
        budget=PlanBudget(18, 1800),
    )
    batch = next_adaptive_batch(plan).batch
    assert batch.plan.estimate.active_seconds == 9
    assert batch.plan.estimate.estimated_payload_bytes == 900
    result = finish(plan)
    sample = result.summaries[0].observations[1]
    assert (
        sample.requested_rate_bps,
        sample.native_per_stream_bps,
        sample.allocated_rate_bps,
        sample.unused_rate_bps,
    ) == (801, 400, 800, 1)
    assert sample.sender_fraction == 800 / 801
    assert not result.summaries[0].observations[0].valid


@pytest.mark.parametrize(
    "rates,search,expected",
    [
        ((800, 801), {}, "integer_grid_exhausted"),
        ((800, 1600), {"max_distinct_rates": 2}, "distinct_rate_limit"),
        ((800, 1600), {"max_refinement_depth": 0}, "refinement_depth_limit"),
    ],
)
def test_finite_rate_and_depth_exhaustion(rates, search, expected):
    """Integer spacing, distinct-point count and depth each have explicit terminal reasons."""
    plan = prepared(rates=rates, **search)
    result = finish(
        plan,
        lambda spec: synthetic_result(
            spec,
            loss_percent=0 if spec.rate_intent.aggregate_bps_per_direction == min(rates) else 20,
        ),
    )
    assert result.decision.reason == expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("min_rate_bps", 0),
        ("max_rate_bps", 2**64),
        ("max_distinct_rates", 0),
        ("max_refinement_depth", -1),
        ("minimum_valid_trials", 0),
        ("confirmation_batches", 0),
        ("receiver_loss_percent", 101),
        ("receiver_loss_percent", float("nan")),
        ("minimum_sender_fraction", 0),
        ("minimum_sender_fraction", 1.1),
        ("max_refinement_depth", True),
    ],
)
def test_invalid_search_policy_rejects(name, value):
    """Search metadata rejects unlimited values, booleans and nonfinite thresholds."""
    with pytest.raises(ValueError):
        prepared(**{name: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config": ClientConfig("host", duration=1, blksize=1200)},
        {"config": ClientConfig("host", protocol="udp", duration=1)},
        {"config": ClientConfig("host", protocol="udp", duration=0, blksize=1200)},
        {
            "config": ClientConfig(
                "host", protocol="udp", duration=1, bidirectional=True, blksize=1200
            )
        },
        {"config": ClientConfig("host", protocol="udp", duration=1, rate=100, blksize=1200)},
        {"budget": PlanBudget(None, 10000)},
        {"budget": PlanBudget(10, None)},
        {"budget": PlanBudget(10, 10000, 1)},
        {"minimum_valid_trials": 3},
        {"rates": (800, 800)},
        {"min_rate_bps": 400},
        {"max_rate_bps": 2400},
        {"budget": PlanBudget(1, 10000)},
        {"budget": PlanBudget(10, 1)},
    ],
)
def test_invalid_preparation_never_produces_a_batch(kwargs):
    """All static admission restrictions apply before executor or native access."""
    with pytest.raises(ValueError):
        prepared(**kwargs)


def test_preparation_history_and_decisions_are_detached():
    """Mutating caller objects cannot alter the already admitted experiment."""
    config = ClientConfig("host", protocol="udp", duration=1, blksize=1200)
    plan = prepared(config=config)
    config.server = "other"
    assert plan.config.server == "host"
    history = execute_batch(plan)
    result = summarize_adaptive_udp(plan, history)
    history[0].execution.trials[0].artifact.result.raw["modified"] = True
    assert "modified" not in result.batches[0].execution.trials[0].artifact.result.raw
    result.decision.batch.plan.trials[0].config.server = "changed"
    assert next_adaptive_batch(plan, result.batches).batch.plan.trials[0].config.server == "host"


@pytest.mark.parametrize("mutation", ["batch", "execution", "cooldown", "summary"])
def test_tampered_history_or_derived_result_is_rejected(mutation):
    """Recorded decisions must match the immutable v1 policy and all retained evidence."""
    result = finish(prepared())
    if mutation == "batch":
        item = replace(result.batches[0], batch=replace(result.batches[0].batch, reason="other"))
        result = replace(result, batches=(item, *result.batches[1:]))
    elif mutation == "execution":
        result.batches[0].execution.plan.trials[0].config.port = 6000
    elif mutation == "cooldown":
        item = replace(result.batches[0], pause_before_seconds=1)
        result = replace(result, batches=(item, *result.batches[1:]))
    else:
        result = replace(result, highest_eligible_bps=123)
    with pytest.raises(ValueError):
        _validate_result_v1(result)


@pytest.mark.parametrize("kind", ["missing", "mismatch", "dangling"])
def test_unverified_native_target_never_establishes_eligibility(kind):
    """A copied requested target is insufficient without a matching observed receipt."""

    def execute(spec):
        result = synthetic_result(spec)
        settings = result.execution.configuration.effective
        if kind == "missing":
            settings.pop("rate")
        elif kind == "mismatch":
            settings["rate"].value += 1
        else:
            settings["rate"].evidence_paths = ["/raw/missing"]
        return result

    result = finish(prepared(), execute)
    assert result.outcome == "inconclusive"


def test_activated_advanced_setting_needs_its_own_native_receipt():
    """Repeated identical requests never substitute for actual advanced-setting evidence."""
    config = ClientConfig("host", protocol="udp", duration=1, blksize=1200, pacing_timer_us=1000)
    plan = prepared(config=config)
    assert finish(plan).outcome == "inconclusive"

    def execute(spec):
        result = synthetic_result(spec)
        result.raw["timer"] = 1000
        result.execution.configuration.effective["pacing_timer_us"] = VerifiedSetting(
            1000, "verified", ["/raw/timer"]
        )
        return result

    assert finish(plan, execute).outcome == "acceptable_tested_rates"


def test_multiple_explicit_confirmations_are_bounded_and_retained():
    """Positive confirmation policy produces the exact declared number of repeat batches."""
    result = finish(prepared(confirmation_batches=2))
    assert [item.batch.rates for item in result.batches] == [(800, 1600), (1600,), (1600,)]
    assert result.summaries[-1].confirmation_batches == 2
    assert len(result.summaries[-1].observations) == 6


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_udp_fixture_extraction_preserves_separate_endpoint_measurements(version, reverse):
    """Normalized archived native observations remain usable without a live library."""
    import json
    from pathlib import Path

    name = "udp-reverse-client.json" if reverse else "udp-client.json"
    raw = json.loads((Path(__file__).parent / "fixtures/native" / version / name).read_text())
    native = result_from_iperf_json(raw)
    config = ClientConfig(
        "127.0.0.1", protocol="udp", duration=1, parallel=2, blksize=1200, reverse=reverse
    )
    plan = prepared(rates=(8000000,), config=config, budget=PlanBudget(30, 100000000))
    result = finish(plan, lambda spec: copy.deepcopy(native))
    sample = result.summaries[0].observations[0]
    assert sample.sender_bps == sample.sender_bytes * 8 / sample.sender_seconds
    assert sample.receiver_bps == sample.receiver_bytes * 8 / sample.receiver_seconds
    assert sample.count_loss_percent == sample.receiver_lost_packets * 100 / sample.receiver_packets
    assert result.outcome == "acceptable_tested_rates"


def test_grid_trial_bound_is_checked_before_materializing_or_resolving(monkeypatch):
    """An oversized finite grid cannot allocate trials or resolve every configuration."""
    import iperf3_lib.adaptive as adaptive

    monkeypatch.setattr(adaptive, "resolve_rate", lambda *args: pytest.fail("preflight first"))
    with pytest.raises(ValueError, match="trial_count_limit"):
        prepared(policy=TrialPolicy(repetitions=2, max_trials=2))


def test_both_nonmonotonic_intervals_are_explored_without_binary_pruning():
    """After an upper mismatch is refined, the lower mismatch remains eligible for refinement."""
    plan = prepared(rates=(800, 1600, 2400), max_refinement_depth=1)
    result = finish(
        plan,
        lambda spec: synthetic_result(
            spec, loss_percent=20 if spec.rate_intent.aggregate_bps_per_direction == 1600 else 0
        ),
    )
    rates = [item.batch.rates for item in result.batches]
    assert (2000,) in rates and (1200,) in rates
    assert result.highest_eligible_bps == 2400
    assert result.decision.reason == "refinement_depth_limit"


@pytest.mark.parametrize("deficit,accepted", [(0.3, False), (0.01, False), (5e-10, True)])
def test_within_batch_cooldown_minimum_is_validated_with_clock_tolerance(
    monkeypatch, deficit, accepted
):
    """Retained histories cannot claim completed transitions with omitted cooldowns."""
    Clock().install(monkeypatch)
    plan = prepared(policy=TrialPolicy(repetitions=2, pause_seconds=0.1, max_trials=30))
    history = execute_batch(plan)
    item = history[0]
    execution = replace(
        item.execution, observed_pause_seconds=item.execution.observed_pause_seconds - deficit
    )
    altered = (replace(item, execution=execution),)
    if accepted:
        assert (
            summarize_adaptive_udp(plan, altered).batches[0].execution.observed_pause_seconds
            == execution.observed_pause_seconds
        )
    else:
        with pytest.raises(ValueError, match="within-batch cooldown"):
            summarize_adaptive_udp(plan, altered)


def test_stop_on_error_cooldown_counts_only_actual_started_transitions(monkeypatch):
    """An unstarted suffix neither consumes nor requires post-failure cooldowns."""
    clock = Clock()
    clock.install(monkeypatch)
    plan = prepared(
        policy=TrialPolicy(repetitions=2, pause_seconds=0.1, max_trials=30, stop_on_error=True)
    )

    def execute(spec):
        if spec.repetition == 1:
            raise RuntimeError("controlled second trial failure")
        return synthetic_result(spec)

    history = execute_batch(plan, executor=execute)
    result = summarize_adaptive_udp(plan, history)
    assert result.decision.reason == "execution_stopped"
    assert [record.status for record in history[0].execution.trials] == [
        "completed",
        "exception",
        "not_run",
        "not_run",
    ]
    assert clock.pauses == [0.1]
    item = replace(history[0], execution=replace(history[0].execution, observed_pause_seconds=0))
    with pytest.raises(ValueError, match="within-batch cooldown"):
        summarize_adaptive_udp(plan, (item,))


def test_between_batch_cooldown_allows_only_clock_precision_deficit(monkeypatch):
    """Global cooldown receipts tolerate floating representation rather than lost pauses."""
    Clock().install(monkeypatch)
    plan = prepared(policy=TrialPolicy(repetitions=2, pause_seconds=0.1, max_trials=30))
    history = execute_batch(plan, execute_batch(plan))
    slight = (*history[:-1], replace(history[-1], pause_before_seconds=0.1 - 5e-10))
    assert summarize_adaptive_udp(plan, slight).highest_eligible_bps == 1600
    absent = (*history[:-1], replace(history[-1], pause_before_seconds=0))
    with pytest.raises(ValueError, match="between-batch cooldown"):
        summarize_adaptive_udp(plan, absent)


@pytest.mark.parametrize("endpoint", ["sender", "receiver"])
def test_unrepresentable_endpoint_rate_retains_raw_counts_and_inconclusive_evidence(endpoint):
    """Serializable large integers survive even when derived float rates cannot."""
    huge_bytes = 10**400

    def execute(spec):
        result = synthetic_result(spec)
        getattr(result.flows[0], endpoint).bytes = huge_bytes
        key = "sum_sent" if endpoint == "sender" else "sum_received"
        result.raw["end"][key]["bytes"] = huge_bytes
        return result

    result = finish(prepared(), execute)
    assert result.outcome == "inconclusive"
    assert len(result.batches) == 1
    for summary in result.summaries:
        for observation in summary.observations:
            assert getattr(observation, f"{endpoint}_bytes") == huge_bytes
            assert getattr(observation, f"{endpoint}_seconds") == 1
            assert getattr(observation, f"{endpoint}_bps") is None
            assert f"{endpoint}_measurement_invalid" in observation.reasons
            assert not observation.valid
    assert all(record.status == "completed" for record in result.batches[0].execution.trials)
    assert _validate_result_v1(result) == result
