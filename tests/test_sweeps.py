"""Bounded admission, deterministic execution order and evidence-preserving summaries."""

import copy
from dataclasses import replace

import pytest
from sweep_helpers import Clock, measured, prepared

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.exceptions import UnsupportedFeatureError
from iperf3_lib.intent import RateIntent
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep, summarize_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy


def test_declared_cartesian_order_ids_phase_order_and_resolved_settings():
    """The last axis changes fastest and every cell retains complete admitted settings."""
    plan = prepared(
        axes=(
            SweepAxis("parallel", (1, 2)),
            SweepAxis("method", ("forward", "reverse", "bidirectional")),
        ),
        policy=TrialPolicy(repetitions=2, warmup_runs=1, pause_seconds=0.5),
    )
    assert [cell.parameters for cell in plan.cells] == [
        {"parallel": count, "method": method}
        for count in (1, 2)
        for method in ("forward", "reverse", "bidirectional")
    ]
    assert [cell.cell_id for cell in plan.cells] == [f"cell-{index:04d}" for index in range(6)]
    assert len(plan.plan.trials) == 18
    assert plan.plan.estimate.active_seconds == 18
    assert plan.plan.planned_pause_seconds == 8.5
    for cell in plan.cells:
        specs = [spec for spec in plan.plan.trials if spec.cell_id == cell.cell_id]
        assert [(spec.phase, spec.repetition) for spec in specs] == [
            ("warmup", 0),
            ("measured", 0),
            ("measured", 1),
        ]
        assert tuple(spec.trial_id for spec in specs) == cell.trial_ids
        assert all(spec.resolved_config == cell.resolved_config for spec in specs)
        assert cell.config.reverse == (cell.parameters["method"] == "reverse")
        assert cell.config.bidirectional == (cell.parameters["method"] == "bidirectional")


def test_seeded_shuffle_records_final_order_and_keeps_cell_phases():
    """Stable IDs remain tied to declared assignments while cell execution order changes."""
    axes = (SweepAxis("parallel", (1, 2, 3, 4)),)
    first = prepared(
        axes=axes, order="randomized", seed=17, policy=TrialPolicy(repetitions=1, warmup_runs=1)
    )
    second = prepared(
        axes=axes, order="randomized", seed=17, policy=TrialPolicy(repetitions=1, warmup_runs=1)
    )
    assert first == second
    assert first.seed == first.plan.order_seed == 17
    assert sorted(cell.cell_id for cell in first.cells) == [f"cell-{i:04d}" for i in range(4)]
    assert [spec.cell_id for spec in first.plan.trials] == [
        cell.cell_id for cell in first.cells for _ in range(2)
    ]


@pytest.mark.parametrize(
    "name,values",
    [
        ("reverse", (True,)),
        ("bidirectional", (True,)),
        ("mptcp", (False,)),
        ("json_stream", (False,)),
        ("unknown", (1,)),
        ("parallel", ()),
        ("parallel", [1, 2]),
        ("parallel", iter([1, 2])),
        ("parallel", (1, True)),
        ("parallel", (1, 1)),
        ("protocol", ("tcp", Protocol.TCP)),
        ("protocol", ("quic",)),
        ("method", ("both",)),
        ("server", ("",)),
        ("rate", (1.5,)),
        ("duration", (None,)),
    ],
)
def test_invalid_axes_reject(name, values):
    """Axes are finite typed values, with no unsupported boolean mode combinations."""
    with pytest.raises((ValueError, TypeError)):
        SweepAxis(name, values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"axes": ()},
        {"axes": iter(())},
        {"axes": ("parallel",)},
        {"axes": (SweepAxis("parallel", (1,)), SweepAxis("parallel", (2,)))},
        {"order": "randomized"},
        {"seed": 1},
        {"order": "randomized", "seed": True},
        {"order": "randomized", "seed": -1},
        {"order": "randomized", "seed": 1.5},
        {"order": "other"},
        {"budget": PlanBudget(None, None)},
    ],
)
def test_invalid_plan_metadata_rejects_before_execution(kwargs):
    """Invalid policies do not produce an admitted plan or native traffic."""
    with pytest.raises((ValueError, TypeError)):
        prepared(**kwargs)


def test_product_bound_precedes_cartesian_materialization(monkeypatch):
    """The total expanded trial cap includes warm-up before allocating any cells."""
    import iperf3_lib.sweeps as sweeps

    monkeypatch.setattr(sweeps, "product", lambda *args: pytest.fail("must reject before product"))
    with pytest.raises(ValueError, match="max_trials"):
        prepared(
            axes=(
                SweepAxis("parallel", tuple(range(1, 101))),
                SweepAxis("port", tuple(range(5000, 5100))),
            ),
            policy=TrialPolicy(repetitions=2, warmup_runs=1, max_trials=1000),
        )


@pytest.mark.parametrize(
    "axes",
    [
        (SweepAxis("protocol", ("tcp", "udp")), SweepAxis("blksize", (8,))),
        (SweepAxis("parallel", (1, 129)),),
        (SweepAxis("duration", (1, 0)),),
    ],
)
def test_later_invalid_combinations_reject_the_full_plan(axes):
    """An early valid cell cannot generate traffic before a later invalid cell is discovered."""
    with pytest.raises(ValueError):
        prepared(axes=axes)


@pytest.mark.parametrize("field", ["mptcp", "json_stream"])
def test_unsupported_base_modes_rejected(field):
    """Wrapper-unsupported modes remain rejected even when the axis itself is valid."""
    config = ClientConfig("host", duration=1, rate=1)
    setattr(config, field, True)
    with pytest.raises(UnsupportedFeatureError):
        prepared(base=config)


def test_all_budgets_include_warmup_omit_parallel_and_both_directions():
    """Exact estimate equality admits; exceeding either finite budget rejects all work."""
    base = ClientConfig("host", duration=2, omit=1, rate=800)
    axes = (SweepAxis("parallel", (1, 2)), SweepAxis("method", ("forward", "bidirectional")))
    policy = TrialPolicy(repetitions=1, warmup_runs=1)
    plan = prepare_sweep(base, axes, policy=policy, budget=PlanBudget(24, 5400))
    assert plan.plan.estimate.active_seconds == 24
    assert plan.plan.estimate.estimated_payload_bytes == 5400
    for budget in (PlanBudget(23, 5400), PlanBudget(24, 5399)):
        with pytest.raises(ValueError):
            prepare_sweep(base, axes, policy=policy, budget=budget)
    with pytest.raises(ValueError, match="payload"):
        prepared(base=ClientConfig("host", duration=1), budget=PlanBudget(4, 1000000))
    unknown = prepared(base=ClientConfig("host", duration=1), budget=PlanBudget(4, None))
    assert unknown.plan.estimate.estimated_payload_bytes is None


def test_rate_intent_resolves_each_cell_and_conflicting_rate_axis_rejects():
    """One fixed aggregate target yields independent native allocations with explicit remainder."""
    plan = prepared(
        base=ClientConfig("host", duration=1),
        rate_intent=RateIntent(aggregate_bps_per_direction=1_000_001),
    )
    assert [cell.resolved_config.rate for cell in plan.cells] == [1_000_001, 500_000]
    assert [cell.config.rate for cell in plan.cells] == [None, None]
    with pytest.raises(ValueError, match="rate axis"):
        prepared(
            base=ClientConfig("host", duration=1),
            axes=(SweepAxis("rate", (1, 2)),),
            rate_intent=RateIntent(per_stream_bps=1),
        )
    with pytest.raises(ValueError):
        prepared(
            base=ClientConfig("host", duration=1),
            rate_intent=RateIntent(aggregate_bps_per_direction=1),
        )


def test_detached_inputs_and_mutated_prepared_plan_revalidation():
    """Caller changes and executor mutations cannot rewrite admitted configurations."""
    base = ClientConfig("host", duration=1, rate=800)
    plan = prepared(base=base)
    original = copy.deepcopy(plan)
    base.rate = 900
    assert plan == original
    plan.cells[0].parameters["parallel"] = 9
    with pytest.raises(ValueError, match="admitted"):
        run_sweep(plan, executor=lambda spec: pytest.fail("no traffic"))
    base.server = True
    with pytest.raises(TypeError):
        prepared(base=base)


def test_shared_engine_pauses_warmup_exclusion_and_receiver_medians(monkeypatch):
    """Cell boundaries share the same clock and pause policy as repeated trials."""
    clock = Clock()
    clock.install(monkeypatch)
    plan = prepared(policy=TrialPolicy(repetitions=2, warmup_runs=1, pause_seconds=0.5))

    def execute(spec):
        clock.elapsed += 1
        return measured(
            spec, count=99999 if spec.phase == "warmup" else 100 * (spec.repetition + 1)
        )

    result = run_sweep(plan, executor=execute, minimum_valid_trials=2)
    assert result.execution.execution_success
    assert clock.pauses == [0.5] * 5
    assert result.execution.elapsed_seconds == 8.5
    for cell in result.cells:
        summary = cell.directions[0]
        assert summary.quality == "complete"
        assert [sample.throughput_bps for sample in summary.samples] == [800, 1600]
        assert summary.median_throughput_bps == 1200
        assert [item.reason for item in summary.exclusions] == ["warmup_run"]


@pytest.mark.parametrize("failure", ["failed", "incomplete", "exception"])
def test_failed_cells_and_unstarted_cells_remain_visible(failure):
    """No failure is retried, discarded or replaced by a zero measurement."""
    seen = []

    def execute(spec):
        seen.append(spec.trial_id)
        if failure == "exception":
            raise RuntimeError("synthetic wrapper exception")
        return measured(spec, failed=failure == "failed", incomplete=failure == "incomplete")

    result = run_sweep(
        prepared(policy=TrialPolicy(repetitions=2, stop_on_error=True)), executor=execute
    )
    assert len(seen) == 1
    assert len(result.cells) == 2 and len(result.execution.trials) == 4
    assert result.execution.trials[0].status == failure
    assert (
        result.execution.trials[0].artifact is None
        if failure == "exception"
        else result.execution.trials[0].artifact is not None
    )
    assert [record.status for record in result.execution.trials[1:]] == ["not_run"] * 3
    assert all(cell.directions[0].quality == "insufficient_data" for cell in result.cells)
    assert result.cells[1].execution_counts["not_run"] == 2


def test_elapsed_budget_stops_during_intercell_pause_with_all_cells_retained(monkeypatch):
    """Admission limits clip waits between calls without promising native cancellation."""
    clock = Clock()
    clock.install(monkeypatch)
    plan = prepared(
        policy=TrialPolicy(repetitions=1, pause_seconds=2), budget=PlanBudget(2, 10000000, 1.5)
    )

    def execute(spec):
        clock.elapsed += 1
        return measured(spec)

    result = run_sweep(plan, executor=execute)
    assert clock.pauses == [0.5]
    assert result.execution.stop_reason == "elapsed_admission_limit"
    assert [record.status for record in result.execution.trials] == ["completed", "not_run"]
    assert len(result.cells) == 2
    zero = prepared(policy=TrialPolicy(repetitions=1), budget=PlanBudget(2, 10000000, 0))
    assert all(
        record.status == "not_run"
        for record in run_sweep(zero, executor=lambda spec: pytest.fail("no run")).execution.trials
    )


def test_methods_and_directions_remain_separate_populations():
    """Forward, reverse and bidirectional cells produce four explicit method/direction groups."""
    axes = (
        SweepAxis("parallel", (1, 2)),
        SweepAxis("method", ("forward", "reverse", "bidirectional")),
    )
    result = run_sweep(
        prepared(axes=axes, policy=TrialPolicy(repetitions=1)),
        executor=measured,
        comparison_policy=ComparisonPolicy("sweep", ("client", "server")),
    )
    assert len(result.comparisons) == 4
    assert {(group.method, group.direction) for group in result.comparisons} == {
        ("forward", "client_to_server"),
        ("reverse", "server_to_client"),
        ("bidirectional", "client_to_server"),
        ("bidirectional", "server_to_client"),
    }
    assert all(len(group.cell_ids) == 2 for group in result.comparisons)
    assert all(group.quality == "complete" for group in result.comparisons)
    assert all(
        group.compatibility.policy.varying_fields == ("method", "parallel")
        for group in result.comparisons
    )


def test_explicit_comparison_policy_unknown_evidence_and_reasoned_exceptions():
    """Cell medians remain descriptive; cross-cell compatibility requires observed settings."""

    def different_version(spec):
        result = measured(spec)
        if spec.config.parallel == 2:
            result.execution.native_version = "iperf 3.19.1"
        return result

    result = run_sweep(prepared(), executor=different_version)
    assert result.comparisons == ()
    policy = ComparisonPolicy("sweep", ("client", "server"))
    checked = summarize_sweep(result.prepared, result.execution, comparison_policy=policy)
    assert checked.comparisons[0].quality == "insufficient_data"
    allowed = summarize_sweep(
        result.prepared,
        result.execution,
        comparison_policy=replace(
            policy, allowed_differences={"native_version": "Explicit native comparison."}
        ),
    )
    assert allowed.comparisons[0].quality == "partial"
    assert allowed.comparisons[0].compatibility.compatible
    with pytest.raises(ValueError, match="declared"):
        run_sweep(
            prepared(),
            executor=lambda spec: pytest.fail("preflight"),
            comparison_policy=replace(policy, varying_fields=("duration",)),
        )


def test_missing_zero_and_wrong_method_measurements_retain_distinct_reasons():
    """Measured zero is a sample; absent data or wrong methodology is excluded."""

    def execute(spec):
        result = measured(spec, count=0)
        if spec.repetition == 1:
            result.flows[0].receiver.bytes = None
        return result

    result = run_sweep(prepared(), executor=execute)
    assert result.cells[0].directions[0].median_throughput_bps == 0
    assert result.cells[0].directions[0].quality == "partial"
    assert result.cells[0].directions[0].exclusions[0].reason == "receiver_measurement_unavailable"
    wrong = run_sweep(
        prepared(),
        executor=lambda spec: measured(replace(spec, config=replace(spec.config, reverse=True))),
    )
    assert all(cell.directions[0].exclusions[0].reason == "method_mismatch" for cell in wrong.cells)


def test_one_cell_group_is_not_a_confident_cross_cell_comparison():
    """A group of one may have compatible repetitions but cannot compare cells."""
    result = run_sweep(
        prepared(axes=(SweepAxis("parallel", (1,)),)),
        executor=measured,
        comparison_policy=ComparisonPolicy("sweep", ("client", "server")),
    )
    assert result.comparisons[0].compatibility.compatible
    assert result.comparisons[0].quality == "insufficient_data"
    assert result.comparisons[0].reasons == ("fewer_than_two_cells",)


@pytest.mark.parametrize("evidence", ["wrong", "unavailable", "missing_receipt"])
def test_declared_axis_mismatch_or_unknown_keeps_observation_unqualified(evidence):
    """A successful run cannot stand in for an axis value it did not verify."""

    def execute(spec):
        result = measured(spec)
        if spec.config.parallel == 2:
            setting = result.execution.configuration.effective["parallel"]
            if evidence == "wrong":
                setting.value = 1
                result.raw["start"]["test_start"]["num_streams"] = 1
            else:
                # Invalid receipt pointers are rejected earlier by the artifact boundary.
                # Native uncertainty remains a valid, explicitly unqualified observation.
                setting.state = "unavailable"
                setting.value = None
                if evidence == "missing_receipt":
                    setting.evidence_paths = []
        return result

    result = run_sweep(
        prepared(), executor=execute, comparison_policy=ComparisonPolicy("sweep", ("a", "b"))
    )
    assert result.execution.execution_success
    assert result.cells[0].directions[0].quality == "complete"
    second = result.cells[1].directions[0]
    assert second.quality == "insufficient_data"
    assert second.median_throughput_bps is None
    assert len(second.samples) == 2
    for sample in second.samples:
        assert sample.throughput_bps == 800 and not sample.eligible_for_cell
        check = sample.setting_checks[0]
        assert check.name == "parallel" and check.expected == 2
        assert check.state == ("mismatch" if evidence == "wrong" else "unknown")
    assert result.comparisons[0].quality == "insufficient_data"
    assert set(result.comparisons[0].compatibility.fingerprints) == {
        sample.trial_id for sample in result.cells[0].directions[0].samples
    }


def test_aggregate_rate_mismatch_rejects_cell_qualification_even_when_parallel_matches():
    """Rate intent checks actual per-stream allocation independently of varying axes."""

    def execute(spec):
        result = measured(spec)
        if spec.config.parallel == 2:
            result.execution.configuration.effective["rate"].value = 1_000_001
            result.raw["start"]["test_start"]["target_bitrate"] = 1_000_001
        return result

    result = run_sweep(
        prepared(
            base=ClientConfig("host", duration=1),
            rate_intent=RateIntent(aggregate_bps_per_direction=1_000_001),
        ),
        executor=execute,
    )
    assert result.cells[0].directions[0].quality == "complete"
    sample = result.cells[1].directions[0].samples[0]
    assert not sample.eligible_for_cell
    rate = next(check for check in sample.setting_checks if check.name == "rate")
    assert (rate.expected, rate.observed, rate.state) == (500_000, 1_000_001, "mismatch")


def test_native_default_axis_retains_observed_choice_and_requires_evidence():
    """None requests native defaults without pretending it identifies an observed numeric value."""
    plan = prepared(axes=(SweepAxis("blksize", (None, 1024)),))
    result = run_sweep(plan, executor=measured)
    check = result.cells[0].directions[0].samples[0].setting_checks[0]
    assert (check.expected, check.observed, check.state) == (None, 4096, "native_default")
    assert result.cells[0].directions[0].quality == "complete"

    def unavailable(spec):
        result = measured(spec)
        result.execution.configuration.effective.pop("blksize")
        return result

    result = run_sweep(plan, executor=unavailable)
    assert all(cell.directions[0].quality == "insufficient_data" for cell in result.cells)


def test_empty_axes_rejected_before_any_plan_is_materialized():
    """A sweep requires an explicit variable or single-value axis."""
    with pytest.raises(ValueError, match="nonempty"):
        prepared(axes=())


@pytest.mark.parametrize("value,observed", [(None, 0), (0, 0), (4000000, 4000000)])
def test_rate_axis_native_default_and_explicit_zero_are_distinct(value, observed):
    """A default request is recorded as such while explicit unlimited zero is matched."""
    result = run_sweep(
        prepared(axes=(SweepAxis("rate", (value,)),), budget=PlanBudget(10, None)),
        executor=measured,
    )
    check = result.cells[0].directions[0].samples[0].setting_checks[0]
    assert check.expected == value and check.observed == observed
    assert check.state == ("native_default" if value is None else "matched")


@pytest.mark.parametrize("name", ["reverse", "bidirectional"])
def test_method_axis_requires_both_native_boolean_receipts(name):
    """A method label cannot qualify missing or contradictory native option observations."""

    def execute(spec):
        result = measured(spec)
        result.execution.configuration.effective[name].value = True
        return result

    result = run_sweep(prepared(axes=(SweepAxis("method", ("forward",)),)), executor=execute)
    sample = result.cells[0].directions[0].samples[0]
    assert not sample.eligible_for_cell
    check = next(item for item in sample.setting_checks if item.name == name)
    assert (check.expected, check.observed, check.state) == (False, True, "mismatch")
