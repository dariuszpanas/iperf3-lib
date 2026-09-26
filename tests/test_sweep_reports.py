"""Frozen sweep-v1 round trips and adversarial evidence/selection validation."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sweep_helpers import Clock, measured, prepared

from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.artifacts import ArtifactProducer
from iperf3_lib.reports import ReportValidationError, UnsupportedReportVersionError
from iperf3_lib.sweep_reports import (
    SweepReport,
    dumps_sweep_report,
    loads_sweep_report,
    report_from_sweep,
    sweep_report_from_dict,
    sweep_report_to_dict,
)
from iperf3_lib.sweeps import run_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy

GOLDEN = Path(__file__).parent / "fixtures/sweeps/v1-synthetic.json"


@pytest.fixture
def report(monkeypatch):
    """Retain deterministic synthetic evidence, order and original producer identity."""
    Clock().install(monkeypatch)
    result = run_sweep(
        prepared(policy=TrialPolicy(repetitions=1), order="randomized", seed=4),
        executor=measured,
        comparison_policy=ComparisonPolicy("synthetic-sweep", ("client", "server")),
    )
    return SweepReport(
        result,
        ArtifactProducer("example.fixture", "1"),
        extensions={"example.synthetic": {"purpose": "codec regression"}},
    )


def test_round_trip_preserves_plan_order_samples_compatibility_and_producers(report):
    """Canonical artifacts and every producer/setting/order survive without execution."""
    encoded = sweep_report_to_dict(report)
    restored = loads_sweep_report(dumps_sweep_report(report, indent=2))
    assert restored == report
    assert sweep_report_to_dict(restored) == encoded
    assert restored.producer == ArtifactProducer("example.fixture", "1")
    assert [cell.cell_id for cell in restored.result.prepared.cells] == ["cell-0001", "cell-0000"]
    assert all(cell.directions[0].median_throughput_bps == 800 for cell in restored.result.cells)
    assert restored.result.comparisons[0].quality == "complete"
    encoded["result"]["execution"]["trials"][0]["artifact"]["producer"]["version"] = "different"
    assert restored.result.execution.trials[0].artifact.producer.version != "different"
    snapshot = report_from_sweep(report.result)
    assert snapshot.result == report.result
    assert snapshot.producer.name == "iperf3-lib"


def test_golden_is_reproducible_and_readable(report):
    """A retained development fixture freezes the v1 payload and summary contract."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = sweep_report_to_dict(report)
    # Nested artifact producers intentionally describe the runtime writer that produced them.
    for item in actual["result"]["execution"]["trials"]:
        item["artifact"]["producer"]["version"] = "fixture-development"
    assert actual == expected
    assert sweep_report_to_dict(sweep_report_from_dict(expected)) == expected


def test_reader_does_not_reanalyze_reparse_load_native_or_replay_rng(report, monkeypatch):
    """Import validates frozen formulas and final recorded order without rerunning algorithms."""
    import iperf3_lib.analysis as analysis
    import iperf3_lib.result as result_module
    import iperf3_lib.sweeps as sweeps
    import iperf3_lib.trials as trials

    text = dumps_sweep_report(report)

    def fail(*args, **kwargs):
        pytest.fail("report reader invoked current execution/analysis")

    monkeypatch.setattr(analysis, "summary_throughput", fail)
    monkeypatch.setattr(analysis, "check_compatibility", fail)
    monkeypatch.setattr(sweeps, "summary_throughput", fail)
    monkeypatch.setattr(sweeps, "check_compatibility", fail)
    monkeypatch.setattr(result_module, "result_from_iperf_json", fail)
    monkeypatch.setattr(trials, "run_plan", fail)
    monkeypatch.setattr(sweeps.random, "Random", fail)
    assert loads_sweep_report(text) == report


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(kind="iperf3-lib.assessment"),
        lambda d: d["producer"].update(version=" "),
        lambda d: d["extensions"].update(unnamespaced=1),
        lambda d: d["result"].update(minimum_valid_trials=True),
        lambda d: d["result"]["prepared"]["cells"][0]["parameters"].update(parallel=99),
        lambda d: d["result"]["prepared"]["cells"].reverse(),
        lambda d: d["result"]["prepared"]["cells"].pop(),
        lambda d: d["result"]["prepared"]["axes"][0]["values"].append(3),
        lambda d: d["result"]["execution"]["plan"]["budget"].update(max_active_seconds=None),
        lambda d: d["result"]["cells"][0]["execution_counts"].update(completed=2),
        lambda d: d["result"]["cells"][0]["directions"][0].update(median_throughput_bps=999),
        lambda d: d["result"]["cells"][0]["directions"][0]["samples"][0].update(bytes=True),
        lambda d: d["result"]["cells"][0]["directions"][0]["samples"][0].update(
            evidence_path="/raw"
        ),
        lambda d: d["result"]["cells"][0]["directions"][0]["samples"][0].update(
            eligible_for_cell=False
        ),
        lambda d: d["result"]["cells"][0]["directions"][0]["samples"][0]["setting_checks"][
            0
        ].update(observed=99),
        lambda d: d["result"]["comparisons"].clear(),
        lambda d: d["result"].update(comparison_policy=None),
        lambda d: d["result"]["comparisons"][0].update(quality="partial"),
        lambda d: d["result"]["comparisons"][0]["compatibility"].update(compatible=False),
        lambda d: d["result"]["comparisons"][0].update(direction="server_to_client"),
    ],
)
def test_tampering_is_rejected_against_retained_execution(report, mutate):
    """Stored labels, membership, arithmetic and comparison evidence cannot contradict trials."""
    data = sweep_report_to_dict(report)
    mutate(data)
    with pytest.raises((ReportValidationError, ValueError)):
        sweep_report_from_dict(data)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("algorithm_revision", "future")])
def test_future_contract_requires_new_reader(report, field, value):
    """Unknown interpretation versions are rejected rather than silently recomputed."""
    data = sweep_report_to_dict(report)
    data[field] = value
    with pytest.raises(UnsupportedReportVersionError):
        sweep_report_from_dict(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1,), object(), {1: "bad"}])
def test_extensions_and_mapping_inputs_require_strict_json(report, value):
    """No tuple coercion, nonfinite numbers, custom objects or non-string object keys."""
    with pytest.raises(ReportValidationError):
        sweep_report_to_dict(replace(report, extensions={"example.invalid": value}))
    data = sweep_report_to_dict(report)
    data["extensions"]["example.invalid"] = value
    with pytest.raises(ReportValidationError):
        sweep_report_from_dict(data)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', "{bad", "null"])
def test_json_syntax_duplicates_and_constants_rejected(text):
    """Input syntax cannot erase repeated keys or admit nonfinite JSON constants."""
    with pytest.raises(ReportValidationError):
        loads_sweep_report(text)


def test_cyclic_extensions_rejected(report):
    """Recursive Python containers cannot become saved report values."""
    recursive = []
    recursive.append(recursive)
    with pytest.raises(ReportValidationError):
        sweep_report_to_dict(replace(report, extensions={"example.cycle": recursive}))


@pytest.mark.parametrize("count", [0, 100])
def test_eligible_samples_cannot_be_cherry_picked_as_insufficient(count):
    """Every eligible result must contribute even when its measured throughput is zero."""
    result = run_sweep(prepared(), executor=lambda spec: measured(spec, count=count))
    data = sweep_report_to_dict(report_from_sweep(result))
    summary = data["result"]["cells"][0]["directions"][0]
    removed = summary["samples"].pop()
    summary["exclusions"].append(
        {"trial_id": removed["trial_id"], "reason": "receiver_measurement_unavailable"}
    )
    summary["quality"] = "partial"
    # Remaining identical sample keeps the median valid; selection still contradicts evidence.
    with pytest.raises(ReportValidationError, match="frozen v1"):
        sweep_report_from_dict(data)


def test_axis_mismatch_eligibility_cannot_be_promoted():
    """A stored report cannot turn the wrong native setting into a qualified cell."""

    def execute(spec):
        result = measured(spec)
        result.execution.configuration.effective["parallel"].value = 1
        result.raw["start"]["test_start"]["num_streams"] = 1
        return result

    result = run_sweep(prepared(), executor=execute)
    report = report_from_sweep(result)
    assert loads_sweep_report(dumps_sweep_report(report)) == report
    data = sweep_report_to_dict(report)
    summary = data["result"]["cells"][1]["directions"][0]
    for sample in summary["samples"]:
        sample["eligible_for_cell"] = True
        sample["setting_checks"][0]["state"] = "matched"
    summary["median_throughput_bps"] = 800
    summary["quality"] = "complete"
    with pytest.raises(ReportValidationError):
        sweep_report_from_dict(data)


def test_writer_rejects_mutated_dataclass_summary(report):
    """Frozen outer models cannot hide mutation in retained nested mappings."""
    modified = copy.deepcopy(report)
    modified.result.cells[0].execution_counts["failed"] = 3
    with pytest.raises(ReportValidationError):
        sweep_report_to_dict(modified)


@pytest.mark.parametrize(
    "outcome", ["failed", "incomplete", "exception", "not_run", "elapsed_stop", "missing_receiver"]
)
def test_partial_histories_round_trip_without_analysis_or_execution(outcome, monkeypatch):
    """Every legitimate partial population remains readable under frozen v1 semantics."""
    import iperf3_lib.sweeps as sweeps
    import iperf3_lib.trials as trials

    clock = Clock()
    clock.install(monkeypatch)
    policy = TrialPolicy(
        repetitions=1, stop_on_error=True, pause_seconds=2 if outcome == "elapsed_stop" else 0
    )
    limit = 0 if outcome == "not_run" else 1.5 if outcome == "elapsed_stop" else None
    plan = prepared(policy=policy, budget=PlanBudget(2, 2000000, limit))

    def execute(spec):
        clock.elapsed += 1
        if outcome == "exception":
            raise RuntimeError("synthetic executor exception")
        result = measured(spec, failed=outcome == "failed", incomplete=outcome == "incomplete")
        if outcome == "missing_receiver":
            result.flows[0].receiver.bytes = None
        return result

    result = run_sweep(
        plan, executor=execute, comparison_policy=ComparisonPolicy("partial", ("client", "server"))
    )
    report = report_from_sweep(result)
    text = dumps_sweep_report(report)

    def fail(*args, **kwargs):
        pytest.fail("frozen import must not invoke execution or current analysis")

    monkeypatch.setattr(sweeps, "summary_throughput", fail)
    monkeypatch.setattr(sweeps, "check_compatibility", fail)
    monkeypatch.setattr(sweeps.random, "Random", fail)
    monkeypatch.setattr(trials, "run_plan", fail)
    restored = loads_sweep_report(text)
    assert restored == report
    assert len(restored.result.execution.trials) == len(plan.plan.trials) == 2
    assert restored.result.comparisons[0].quality == "insufficient_data"
    if outcome in {"failed", "incomplete", "exception"}:
        assert [record.status for record in restored.result.execution.trials] == [
            outcome,
            "not_run",
        ]
    elif outcome == "not_run":
        assert all(record.status == "not_run" for record in restored.result.execution.trials)
    elif outcome == "elapsed_stop":
        assert restored.result.execution.stop_reason == "elapsed_admission_limit"
        assert [record.status for record in restored.result.execution.trials] == [
            "completed",
            "not_run",
        ]
    else:
        assert restored.result.execution.execution_success
        assert all(
            cell.directions[0].exclusions[0].reason == "receiver_measurement_unavailable"
            for cell in restored.result.cells
        )
