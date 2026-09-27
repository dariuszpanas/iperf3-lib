"""Deterministic sequential execution, admission budgets, and retained failures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace

import pytest

from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import RateIntent
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import (
    PlanBudget,
    TrialPolicy,
    TrialSpec,
    prepare_plan,
    prepare_trials,
    run_plan,
)


class FakeClock:
    """Advance only for explicit fake work or sleep."""

    def __init__(self):
        """Start a deterministic clock and pause history."""
        self.elapsed = 0.0
        self.pauses = []

    def monotonic(self):
        """Return fake monotonic seconds."""
        return self.elapsed

    def wall(self):
        """Return a distinct fake Unix wall clock."""
        return 1000 + self.elapsed

    def sleep(self, seconds):
        """Record and advance a requested between-run pause."""
        self.pauses.append(seconds)
        self.elapsed += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replace every runner clock operation without sleeping."""
    import iperf3_lib.trials as module

    value = FakeClock()
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(time=value.wall, monotonic=value.monotonic, sleep=value.sleep),
    )
    return value


def completed():
    """Create explicit canonical terminal evidence for deterministic executors."""
    return result_from_iperf_json(
        {
            "start": {"test_start": {"reverse": 0}},
            "end": {"sum_received": {"bytes": 100, "seconds": 1}},
        }
    )


def prepared(**policy):
    """Admit a small, finite rate-limited plan."""
    return prepare_trials(
        ClientConfig("host", duration=1, rate=800),
        policy=TrialPolicy(**policy),
        budget=PlanBudget(100, 10000),
    )


def test_warmup_repetitions_pauses_and_exact_order_are_preserved(clock):
    """Keep warm-up runs distinct from measured samples without hidden retries."""
    plan = prepared(repetitions=2, warmup_runs=1, pause_seconds=0.5)
    called = []

    def execute(spec):
        called.append((spec.phase, spec.repetition))
        clock.elapsed += 2
        return completed()

    result = run_plan(plan, executor=execute)
    assert called == [("warmup", 0), ("measured", 0), ("measured", 1)]
    assert clock.pauses == [0.5, 0.5]
    assert result.execution_success
    assert result.elapsed_seconds == 7
    assert result.observed_pause_seconds == result.plan.planned_pause_seconds == 1
    assert all(record.elapsed_seconds == 2 for record in result.trials)
    assert result.started_at_seconds == 1000 and result.completed_at_seconds == 1007
    assert result.plan.estimate.active_seconds == 3
    assert result.plan.estimate.estimated_payload_bytes == 300
    assert [record.spec.trial_id for record in result.trials] == [
        spec.trial_id for spec in plan.trials
    ]


def test_native_failures_incomplete_results_and_exceptions_are_retained_without_retry(clock):
    """Each declared trial is executed once even when earlier results failed."""
    results = iter(
        [result_from_iperf_json({"error": "refused"}), result_from_iperf_json({}), completed()]
    )
    result = run_plan(prepared(), executor=lambda spec: next(results))
    assert [trial.status for trial in result.trials] == ["failed", "incomplete", "completed"]
    assert all(trial.artifact is not None for trial in result.trials)
    assert not result.execution_success

    def broken(spec):
        raise OSError("transport unavailable")

    failure = run_plan(prepared(repetitions=1), executor=broken)
    record = failure.trials[0]
    assert record.status == "exception" and record.artifact is None
    assert record.exception.type_name == "builtins.OSError"
    assert record.exception.message == "transport unavailable"
    assert record.returned_result_evidence is None


def test_stop_on_error_keeps_every_unstarted_record_without_pause_or_retry(clock):
    """Preserve remaining declared runs when an explicit stop policy triggers."""
    result = run_plan(
        prepared(warmup_runs=1, stop_on_error=True, pause_seconds=1),
        executor=lambda spec: result_from_iperf_json({"error": "refused"}),
    )
    assert [record.status for record in result.trials] == [
        "failed",
        "not_run",
        "not_run",
        "not_run",
    ]
    assert all(record.reason == "stop_on_error" for record in result.trials[1:])
    assert result.stop_reason == "stop_on_error"
    assert not clock.pauses


@pytest.mark.parametrize(
    "limit,expected_completed,expected_pause",
    [(0, 0, 0), (1, 1, 0), (2.5, 1, 0.5), (3, 1, 1), (4, 2, 1)],
)
def test_elapsed_admission_stop_never_claims_to_interrupt_a_native_call(
    clock, limit, expected_completed, expected_pause
):
    """The last call may overshoot; stop only before admitting the next run."""
    plan = prepared(pause_seconds=1)
    plan = replace(plan, budget=PlanBudget(100, 10000, limit))

    def execute(spec):
        clock.elapsed += 2
        return completed()

    result = run_plan(plan, executor=execute)
    assert sum(record.status == "completed" for record in result.trials) == expected_completed
    assert result.observed_pause_seconds == expected_pause
    assert result.stop_reason == "elapsed_admission_limit"
    assert all(
        record.started_at_seconds is None for record in result.trials if record.status == "not_run"
    )


def test_admission_detaches_config_and_resolved_intent_before_executor_code(clock):
    """Executor or caller mutation cannot alter recorded admitted settings."""
    config = ClientConfig("host", duration=1, parallel=2)
    plan = prepare_trials(
        config,
        rate_intent=RateIntent(aggregate_bps_per_direction=1001),
        policy=TrialPolicy(repetitions=2),
        budget=PlanBudget(2, 250),
    )
    config.parallel = 20
    seen = []

    def execute(spec):
        seen.append(spec.resolved_config.rate)
        spec.config.parallel = 99
        plan.trials[1].config.parallel = 100
        return completed()

    result = run_plan(plan, executor=execute)
    assert seen == [500, 500]
    assert all(record.spec.config.parallel == 2 for record in result.trials)
    assert result.plan.estimate.estimated_payload_bytes == 250
    detached = result.trials[0].spec.resolved_config
    detached.rate = 99
    assert result.trials[0].spec.resolved_config.rate == 500


def test_mutated_plan_is_revalidated_before_execution(clock):
    """Frozen containers do not bypass validation of their mutable nested configs."""
    plan = prepared()
    plan.trials[0].config.duration = 0
    with pytest.raises(ValueError, match="duration"):
        run_plan(plan, executor=lambda _: pytest.fail("executed invalid plan"))


def test_trial_count_cap_is_checked_before_materializing_sequence():
    """Reject oversized plans before iterating or allocating their elements."""

    class Oversized(Sequence):
        """Declare a huge finite size but prohibit element access."""

        def __len__(self):
            return 10**10

        def __getitem__(self, index):
            pytest.fail("oversized plan was materialized")

    with pytest.raises(ValueError, match="trial count"):
        prepare_plan(Oversized(), policy=TrialPolicy(), budget=PlanBudget(None, None))
    with pytest.raises(ValueError, match="max_trials"):
        TrialPolicy(repetitions=10**10)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repetitions": 0},
        {"repetitions": True},
        {"warmup_runs": -1},
        {"pause_seconds": float("nan")},
        {"pause_seconds": -1},
        {"pause_seconds": True},
        {"max_trials": 0},
        {"stop_on_error": 1},
    ],
)
def test_invalid_policies_fail_before_traffic(kwargs):
    """Require finite counts, delays, and strict booleans."""
    with pytest.raises(ValueError):
        TrialPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_active_seconds": -1},
        {"max_payload_bytes": True},
        {"stop_after_elapsed_seconds": float("inf")},
        {"stop_after_elapsed_seconds": -1},
    ],
)
def test_invalid_budgets_are_rejected(kwargs):
    """Unknown limits are explicit None; finite limits are strict valid quantities."""
    values = {"max_active_seconds": None, "max_payload_bytes": None, **kwargs}
    with pytest.raises(ValueError):
        PlanBudget(**values)


@pytest.mark.parametrize(
    "change",
    [
        {"trial_id": " "},
        {"cell_id": 2},
        {"phase": "other"},
        {"repetition": True},
        {"config": None},
        {"rate_intent": 1},
    ],
)
def test_invalid_trial_spec_is_rejected(change):
    """Validate all public spec fields during whole-plan admission."""
    spec = TrialSpec("one", "cell", "measured", 0, ClientConfig("host", rate=800))
    with pytest.raises((TypeError, ValueError)):
        prepare_plan([replace(spec, **change)], policy=TrialPolicy(), budget=PlanBudget(None, None))


def test_duplicate_unsupported_unbounded_and_invalid_plan_inputs_are_rejected():
    """Static admission rejects unsupported modes and unverifiable finite traffic budgets."""
    spec = TrialSpec("one", "cell", "measured", 0, ClientConfig("host"))
    for specs in ([], [spec, spec], iter([spec]), "bad", [object()]):
        with pytest.raises(ValueError):
            prepare_plan(specs, policy=TrialPolicy(), budget=PlanBudget(None, None))
    with pytest.raises(ValueError, match="unbounded"):
        prepare_trials(spec.config, budget=PlanBudget(None, 100))
    with pytest.raises(ValueError, match="unlimited"):
        prepare_trials(ClientConfig("host", duration=0), budget=PlanBudget(None, None))
    with pytest.raises(ValueError):
        prepare_plan([spec], policy=None, budget=PlanBudget(None, None))
    with pytest.raises(ValueError):
        prepare_plan([spec], policy=TrialPolicy(), budget=PlanBudget(None, None), order_seed=True)
    with pytest.raises(ValueError):
        prepare_trials(spec.config, budget=PlanBudget(None, None), policy=None)
    with pytest.raises(ValueError):
        run_plan(None)
    with pytest.raises(ValueError):
        run_plan(prepared(), executor=1)


def test_unencodable_returned_result_preserves_json_safe_evidence(clock):
    """Artifact validation failure cannot silently discard a returned native document."""
    result = completed()
    result.flows[0].receiver.bits_per_second = float("nan")
    result.extensions["example.bad"] = object()
    record = run_plan(prepared(repetitions=1), executor=lambda _: result).trials[0]
    assert record.status == "exception" and record.artifact is None
    assert record.exception is not None
    assert record.returned_result_evidence["raw"] == result.raw
    assert record.returned_result_evidence["ok"] is True
    assert "flows" not in record.returned_result_evidence
    assert "extensions" not in record.returned_result_evidence
    assert any("not JSON-safe" in message for message in record.diagnostics)


def test_wrong_executor_return_is_recorded_but_process_control_propagates(clock):
    """Only ordinary execution exceptions are retained as failed trials."""
    record = run_plan(prepared(repetitions=1), executor=lambda _: "bad").trials[0]
    assert record.status == "exception" and record.exception.type_name == "builtins.TypeError"

    def interrupted(spec):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_plan(prepared(repetitions=1), executor=interrupted)


@pytest.mark.parametrize(
    "sequence",
    [
        [("measured", 0)],
        [("warmup", 0), ("measured", 0), ("measured", 0)],
        [("measured", 0), ("warmup", 0), ("measured", 1)],
        [("warmup", 0), ("measured", 1), ("measured", 0)],
    ],
)
def test_each_cell_requires_exact_policy_counts_and_phase_order(sequence):
    """Reject contradictory per-cell policies before native execution."""
    specs = [
        TrialSpec(str(index), "cell", phase, repetition, ClientConfig("host"))
        for index, (phase, repetition) in enumerate(sequence)
    ]
    with pytest.raises(ValueError, match="each cell"):
        prepare_plan(
            specs, policy=TrialPolicy(repetitions=2, warmup_runs=1), budget=PlanBudget(None, None)
        )


def test_interleaved_cells_preserve_each_cells_declared_order():
    """Allow declared cross-cell order while each cell retains its complete policy."""
    specs = [
        TrialSpec(f"{cell}:{phase}", cell, phase, 0, ClientConfig("host", rate=800))
        for phase in ("warmup", "measured")
        for cell in ("b", "a")
    ]
    plan = prepare_plan(
        specs,
        policy=TrialPolicy(repetitions=1, warmup_runs=1),
        budget=PlanBudget(40, 4000),
        order_seed=0,
    )
    assert plan.order_seed == 0 and [spec.cell_id for spec in plan.trials] == ["b", "a", "b", "a"]


def test_numeric_overflow_and_cyclic_partial_evidence_are_rejected(clock):
    """Huge numeric values or cyclic fields cannot leak through as JSON evidence."""
    with pytest.raises(ValueError):
        TrialPolicy(pause_seconds=10**1000)
    result = completed()
    result.raw["cycle"] = result.raw
    retained = run_plan(prepared(repetitions=1), executor=lambda _: result).trials[0]
    assert retained.status == "exception"
    assert "raw" not in retained.returned_result_evidence
    assert retained.diagnostics
