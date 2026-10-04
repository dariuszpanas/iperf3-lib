"""Frozen adaptive-UDP archives reject changed evidence, decisions, and JSON shape."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sweep_helpers import Clock, measured

from iperf3_lib.adaptive import (
    AdaptiveBatchResult,
    AdaptiveUDPPolicy,
    next_adaptive_batch,
    prepare_adaptive_udp,
    summarize_adaptive_udp,
)
from iperf3_lib.adaptive_reports import (
    AdaptiveUDPReport,
    adaptive_udp_report_from_dict,
    adaptive_udp_report_to_dict,
    dumps_adaptive_udp_report,
    loads_adaptive_udp_report,
    render_adaptive_udp_text,
    report_from_adaptive_udp,
)
from iperf3_lib.artifacts import ArtifactProducer
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.reports import ReportValidationError, UnsupportedReportVersionError
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialPolicy, run_plan

GOLDEN = Path(__file__).parent / "fixtures/adaptive/v1-synthetic.json"


def _measured(spec, *, fault=None):
    original = measured(spec)
    raw = copy.deepcopy(original.raw)
    config = spec.resolved_config
    count = config.rate * config.parallel // 8
    loss = 20 if config.rate * config.parallel > 16000 else 0
    raw["end"]["sum_sent"].update(bytes=count, seconds=1)
    raw["end"]["sum_received"].update(
        bytes=count * (1000 - loss) // 1000,
        seconds=1,
        packets=1000,
        lost_packets=loss,
        # Retain this independently from the packet-derived decision value.
        lost_percent=loss / 10 + 0.25,
    )
    if fault == "missing_packets":
        del raw["end"]["sum_received"]["packets"]
    elif fault == "under_driven":
        raw["end"]["sum_sent"]["bytes"] = count // 2
    elif fault == "failed":
        raw["error"] = "synthetic adaptive execution failure"
    elif fault == "exception":
        raise RuntimeError("synthetic adaptive executor failure")
    elif fault in {"sender_overflow", "receiver_overflow"}:
        endpoint = "sum_sent" if fault == "sender_overflow" else "sum_received"
        raw["end"][endpoint].update(bytes=1, seconds=1e-320)
    result = result_from_iperf_json(raw)
    result.extensions = original.extensions
    return result


def _experiment(*, fault=None, empty=False):
    prepared = prepare_adaptive_udp(
        ClientConfig("127.0.0.1", protocol=Protocol.UDP, duration=1, blksize=1200),
        (8000, 24000),
        policy=TrialPolicy(repetitions=2, pause_seconds=0.5, max_trials=30),
        budget=PlanBudget(30, 1000000),
        adaptive_policy=AdaptiveUDPPolicy(8000, 24000, 4, 1, 1, 2, 0.9),
    )
    history = []
    if not empty:
        for _ in range(30):
            decision = next_adaptive_batch(prepared, tuple(history))
            if decision.batch is None:
                break
            batch = decision.batch
            execution = run_plan(batch.plan, executor=lambda spec: _measured(spec, fault=fault))
            history.append(
                AdaptiveBatchResult(
                    batch, execution, prepared.policy.pause_seconds if history else 0
                )
            )
        else:
            pytest.fail("finite synthetic adaptive experiment did not terminate")
    return summarize_adaptive_udp(prepared, tuple(history))


@pytest.fixture(scope="module")
def report():
    """Retain controlled non-monotonicity-free evidence with mismatching native loss."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        Clock().install(monkeypatch)
        return AdaptiveUDPReport(
            _experiment(),
            ArtifactProducer("example.fixture", "1"),
            extensions={"example.synthetic": {"purpose": "frozen codec regression"}},
        )


def test_round_trip_retains_evidence_order_pauses_and_original_producers(report):
    """Archive round trips preserve every observation and all nested native artifacts."""
    encoded = adaptive_udp_report_to_dict(report)
    restored = loads_adaptive_udp_report(dumps_adaptive_udp_report(report, indent=2))
    assert restored == report
    assert adaptive_udp_report_to_dict(restored) == encoded
    assert restored.producer == ArtifactProducer("example.fixture", "1")
    assert restored.result.observed_pause_seconds > 0
    assert restored.result.highest_eligible_bps == 16000
    assert restored.result.acceptable_rates_bps == (8000, 16000)
    assert any(
        item.count_loss_percent != item.native_loss_percent
        for summary in restored.result.summaries
        for item in summary.observations
    )
    encoded["result"]["batches"][0]["execution"]["trials"][0]["artifact"]["producer"]["version"] = (
        "changed"
    )
    assert restored.result.batches[0].execution.trials[0].artifact.producer.version != "changed"
    snapshot = report_from_adaptive_udp(report.result)
    assert snapshot.result == report.result
    assert snapshot.producer.name == "iperf3-lib"


def test_golden_reproduces_frozen_v1_schema_and_decision_contract(report):
    """A retained synthetic fixture catches changes to version-one interpretation."""
    actual = adaptive_udp_report_to_dict(report)
    for batch in actual["result"]["batches"]:
        for item in batch["execution"]["trials"]:
            if item["artifact"] is not None:
                item["artifact"]["producer"]["version"] = "fixture-development"
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected
    assert adaptive_udp_report_to_dict(adaptive_udp_report_from_dict(expected)) == expected


def test_reader_never_calls_current_planner_analysis_normalizer_or_native(report, monkeypatch):
    """Frozen validation is independent of future public execution or analysis changes."""
    import iperf3_lib.adaptive as adaptive
    import iperf3_lib.analysis as analysis
    import iperf3_lib.result as result_module
    import iperf3_lib.sweeps as sweeps
    import iperf3_lib.trials as trials

    text = dumps_adaptive_udp_report(report)

    def fail(*args, **kwargs):
        pytest.fail("archive reader invoked current execution, planning, or analysis")

    for name in ("prepare_adaptive_udp", "next_adaptive_batch", "summarize_adaptive_udp"):
        monkeypatch.setattr(adaptive, name, fail)
    monkeypatch.setattr(analysis, "summary_throughput", fail)
    monkeypatch.setattr(analysis, "check_compatibility", fail)
    monkeypatch.setattr(result_module, "result_from_iperf_json", fail)
    monkeypatch.setattr(trials, "run_plan", fail)
    monkeypatch.setattr(sweeps.random, "Random", fail)
    assert loads_adaptive_udp_report(text) == report


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(kind="iperf3-lib.sweep"),
        lambda d: d["producer"].update(version=" "),
        lambda d: d["extensions"].update(unnamespaced=1),
        lambda d: d["result"].update(highest_eligible_bps=24000),
        lambda d: d["result"].update(acceptable_rates_bps=[8000, 16000, 24000]),
        lambda d: d["result"].update(ceiling_censored=True),
        lambda d: d["result"].update(outcome="inconclusive"),
        lambda d: d["result"]["prepared"]["initial_rates"].reverse(),
        lambda d: d["result"]["prepared"]["initial_rates"].append(8000),
        lambda d: d["result"]["prepared"]["adaptive_policy"].update(max_distinct_rates=True),
        lambda d: d["result"]["prepared"]["adaptive_policy"].update(minimum_sender_fraction=1.1),
        lambda d: d["result"]["prepared"]["budget"].update(max_active_seconds=None),
        lambda d: d["result"]["prepared"]["budget"].update(stop_after_elapsed_seconds=1),
        lambda d: d["result"]["batches"].reverse(),
        lambda d: d["result"]["batches"][0]["batch"].update(index=2),
        lambda d: d["result"]["batches"][0]["batch"].update(reason="confirmation"),
        lambda d: d["result"]["batches"][0]["batch"].update(depth=1),
        lambda d: d["result"]["batches"][0].update(pause_before_seconds=-1),
        lambda d: d["result"]["batches"][1].update(pause_before_seconds=0),
        lambda d: d["result"]["decision"].update(reason="invented"),
        lambda d: d["result"]["summaries"].reverse(),
        lambda d: d["result"]["summaries"][0].update(valid_trials=999),
        lambda d: d["result"]["summaries"][0].update(confirmation_batches=0),
        lambda d: d["result"]["summaries"][0]["observations"].pop(),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(sender_bytes=True),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(sender_bps=1),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(sender_fraction=0),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(receiver_packets=0),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(count_loss_percent=99),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(native_loss_percent=99),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(valid=False),
        lambda d: d["result"]["summaries"][0]["observations"][0].update(acceptable=False),
        lambda d: d["result"]["summaries"][0]["observations"][0]["setting_checks"][0].update(
            state="unknown"
        ),
    ],
)
def test_changed_evidence_admission_and_decisions_are_rejected(report, mutate):
    """Stored output cannot cherry-pick or reinterpret the retained experiment."""
    data = adaptive_udp_report_to_dict(report)
    mutate(data)
    with pytest.raises(ReportValidationError):
        adaptive_udp_report_from_dict(data)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("algorithm_revision", "future")])
def test_future_versions_require_explicit_reader_support(report, field, value):
    """A newer interpretation cannot silently acquire the current reader's decisions."""
    data = adaptive_udp_report_to_dict(report)
    data[field] = value
    with pytest.raises(UnsupportedReportVersionError):
        adaptive_udp_report_from_dict(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1,), object(), {1: "bad"}])
def test_extensions_and_mapping_require_strict_finite_json(report, value):
    """Python-only values cannot be silently coerced into an archived report."""
    with pytest.raises(ReportValidationError):
        adaptive_udp_report_to_dict(replace(report, extensions={"example.invalid": value}))
    data = adaptive_udp_report_to_dict(report)
    data["extensions"]["example.invalid"] = value
    with pytest.raises(ReportValidationError):
        adaptive_udp_report_from_dict(data)


@pytest.mark.parametrize(
    "text", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', "{bad", "null", b"\xff"]
)
def test_malformed_json_duplicate_keys_and_constants_are_rejected(text):
    """JSON syntax cannot discard contradictory duplicate values or nonfinite numbers."""
    with pytest.raises(ReportValidationError):
        loads_adaptive_udp_report(text)


def test_cyclic_extensions_and_invalid_outer_model_are_rejected(report):
    """Recursive mutable containers fail cleanly before any data is emitted."""
    recursive = []
    recursive.append(recursive)
    with pytest.raises(ReportValidationError):
        adaptive_udp_report_to_dict(replace(report, extensions={"example.cycle": recursive}))
    with pytest.raises(ReportValidationError):
        adaptive_udp_report_to_dict(None)
    with pytest.raises(ReportValidationError):
        loads_adaptive_udp_report({})


@pytest.mark.parametrize("fault", ["missing_packets", "under_driven", "failed", "exception"])
def test_partial_and_unacceptable_observations_remain_readable(monkeypatch, fault):
    """Unavailable or failed evidence is retained without becoming a tested pass."""
    Clock().install(monkeypatch)
    report = report_from_adaptive_udp(_experiment(fault=fault))
    restored = loads_adaptive_udp_report(dumps_adaptive_udp_report(report))
    assert restored == report
    assert restored.result.highest_eligible_bps is None
    assert restored.result.acceptable_rates_bps == ()
    assert all(summary.status != "eligible" for summary in restored.result.summaries)
    assert any(
        item.reasons for summary in restored.result.summaries for item in summary.observations
    )


def test_unstarted_experiment_preserves_next_admitted_batch(monkeypatch):
    """Saving a planning-only snapshot does not run or pretend to complete its proposal."""
    Clock().install(monkeypatch)
    report = report_from_adaptive_udp(_experiment(empty=True))
    restored = loads_adaptive_udp_report(dumps_adaptive_udp_report(report))
    assert restored.result.batches == ()
    assert restored.result.summaries == ()
    assert restored.result.decision.batch is not None
    assert restored.result.outcome == "inconclusive"
    text = render_adaptive_udp_text(restored)
    assert "next admitted batch; not yet executed" in text
    assert "still untested in this batch: 8000, 24000" in text


def test_text_distinguishes_requested_measured_loss_and_tested_points(report):
    """Human output reports observations and limitations without a capacity assertion."""
    text = render_adaptive_udp_text(report)
    assert "Highest tested eligible offered load: 16000 bit/s" in text
    assert "Acceptable tested offered loads (bit/s): 8000, 16000" in text
    assert "Requested 24000 bit/s: rejected" in text
    assert "Allocated aggregate:" in text
    assert "Achieved sender:" in text and "Achieved receiver:" in text
    assert "Count-derived receiver loss:" in text and "Native-reported receiver loss:" in text
    assert "at least 2 valid trials and 1 confirmation batches" in text
    assert "Untested gaps are unknown" in text
    assert "do not establish physical network capacity" in text


@pytest.mark.parametrize("endpoint", ["sender", "receiver"])
def test_overflowing_measurement_keeps_finite_source_fields_in_archive(monkeypatch, endpoint):
    """Unrepresentable derived rates cannot erase retained finite byte/time evidence."""
    Clock().install(monkeypatch)
    report = report_from_adaptive_udp(_experiment(fault=f"{endpoint}_overflow"))
    restored = loads_adaptive_udp_report(dumps_adaptive_udp_report(report))
    assert restored == report
    assert restored.result.highest_eligible_bps is None
    assert restored.result.acceptable_rates_bps == ()
    for summary in restored.result.summaries:
        for observation in summary.observations:
            assert not observation.valid
            assert getattr(observation, f"{endpoint}_bytes") == 1
            assert getattr(observation, f"{endpoint}_seconds") == 1e-320
            assert getattr(observation, f"{endpoint}_bps") is None
            assert f"{endpoint}_measurement_invalid" in observation.reasons
    for batch in restored.result.batches:
        for record in batch.execution.trials:
            measurement = getattr(record.artifact.result.flows[0], endpoint)
            assert measurement.bytes == 1
            assert measurement.duration_seconds == 1e-320


def test_archive_cannot_erase_within_batch_cooldown(report):
    """Recorded started repetitions require their declared aggregate cooldown."""
    data = adaptive_udp_report_to_dict(report)
    data["result"]["batches"][0]["execution"]["observed_pause_seconds"] = 0
    with pytest.raises(ReportValidationError, match="cooldown"):
        adaptive_udp_report_from_dict(data)


def test_archive_allows_only_float_precision_cooldown_deficit(report):
    """Subnanosecond clock arithmetic does not invalidate an otherwise retained batch."""
    data = adaptive_udp_report_to_dict(report)
    execution = data["result"]["batches"][0]["execution"]
    execution["observed_pause_seconds"] -= 1e-12
    restored = adaptive_udp_report_from_dict(data)
    assert adaptive_udp_report_to_dict(restored) == data
