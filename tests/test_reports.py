"""Strict report-v1 roundtrips preserve retained evidence and frozen decisions."""

import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from trial_helpers import measured, report

from iperf3_lib.analysis import AnalysisTrial, check_compatibility
from iperf3_lib.artifacts import artifact_from_result
from iperf3_lib.assessments import AssessmentPolicy, ExcludedTrial, _decision_v1
from iperf3_lib.reports import (
    ReportValidationError,
    UnsupportedReportVersionError,
    dumps_report,
    loads_report,
    plan_result_from_dict,
    plan_result_to_dict,
    render_junit,
    render_text,
    report_from_dict,
    report_to_dict,
    summary_eligible_v1,
)
from iperf3_lib.result import result_from_iperf_json


def test_report_and_reusable_plan_payload_roundtrip_without_reanalysis(monkeypatch):
    """Report and reusable plan payload roundtrip without reanalysis."""
    original = report(baselines=[artifact_from_result(measured())])
    data = report_to_dict(original)

    def forbidden(*args, **kwargs):
        raise AssertionError("archived evidence must not be reinterpreted")

    import iperf3_lib.analysis as analysis
    import iperf3_lib.assessments as assessments

    for module in (assessments, analysis):
        monkeypatch.setattr(module, "summary_throughput", forbidden)
        monkeypatch.setattr(module, "check_compatibility", forbidden)
    restored = loads_report(json.dumps(data))
    assert restored == original
    assert dumps_report(restored) == dumps_report(original)
    assert plan_result_from_dict(plan_result_to_dict(original.execution)) == original.execution
    data["execution"]["plan"]["trials"][0]["config"]["server"] = "mutated"
    assert original.execution.plan.trials[0].config.server == "127.0.0.1"


def test_roundtrip_failed_incomplete_unstarted_and_exception_evidence():
    """Roundtrip failed incomplete unstarted and exception evidence."""
    for values, options in (
        ([result_from_iperf_json({"error": "native failure"}), measured(), measured()], {}),
        ([result_from_iperf_json({}), measured(), measured()], {}),
        ([OSError("transport failure"), measured(), measured()], {"stop_on_error": True}),
    ):
        original = report(values, **options)
        assert loads_report(dumps_report(original)) == original
    malformed = measured()
    malformed.extensions["example.bad"] = {"value": float("nan")}
    original = report([malformed, measured(), measured()])
    restored = loads_report(dumps_report(original))
    assert restored.execution.trials[0].status == "exception"
    assert restored.execution.trials[0].returned_result_evidence["raw"] == malformed.raw


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(unknown=1),
        lambda d: d.pop("policy"),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(kind="result"),
        lambda d: d.update(ci_exit_code=3),
        lambda d: d["execution"].update(execution_success=False),
        lambda d: d["execution"]["plan"]["trials"][0]["resolved_config"].update(rate=3),
        lambda d: d["execution"]["plan"]["estimate"].update(active_seconds=999),
        lambda d: d["execution"]["plan"]["policy"].update(max_trials=1),
        lambda d: d["execution"]["trials"].pop(),
        lambda d: d["execution"]["trials"][0].update(status="failed"),
        lambda d: d["execution"]["trials"][0].update(elapsed_seconds=-1),
        lambda d: d["execution"].update(elapsed_seconds=-1),
        lambda d: d["execution"].update(stop_reason="unknown"),
        lambda d: d["assessment"].update(outcome="fail"),
        lambda d: d["assessment"].update(median_throughput_bps=1),
        lambda d: d["assessment"].update(relative_change=0.1),
        lambda d: d["assessment"]["measurements"][0]["throughput"].update(bytes=123),
        lambda d: d["assessment"]["measurements"][0]["throughput"].update(throughput_bps=801),
        lambda d: d["assessment"]["measurements"][0]["throughput"]["evidence"][0].update(
            path="/fake"
        ),
        lambda d: d["assessment"]["compatibility"]["fingerprints"].pop("trial:default:measured:0"),
        lambda d: d["assessment"]["compatibility"]["policy"].update(group_id="different"),
        lambda d: d["assessment"]["measurements"].append(
            copy.deepcopy(d["assessment"]["measurements"][0])
        ),
        lambda d: d["policy"].update(minimum_valid_trials=True),
        lambda d: d["execution"]["trials"][0]["artifact"]["result"]["raw"].update(bad=float("nan")),
    ],
)
def test_strict_reader_rejects_tampering_and_contradictory_evidence(mutation):
    """Strict reader rejects tampering and contradictory evidence."""
    data = report_to_dict(report())
    mutation(data)
    with pytest.raises(ReportValidationError):
        report_from_dict(data)


@pytest.mark.parametrize(
    "field,value", [("schema_version", 2), ("algorithm_revision", "median-summary-v2")]
)
def test_unknown_versions_are_explicit(field, value):
    """Unknown versions are explicit."""
    data = report_to_dict(report())
    target = data if field == "schema_version" else data["assessment"]
    target[field] = value
    with pytest.raises(UnsupportedReportVersionError):
        report_from_dict(data)


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"x":NaN}',
        '{"x":Infinity}',
        "[]",
        "{",
        b"\xff",
        2,
    ],
)
def test_invalid_json_duplicate_keys_and_nonfinite_constants_rejected(text):
    """Invalid json duplicate keys and nonfinite constants rejected."""
    with pytest.raises(ReportValidationError):
        loads_report(text)


def test_writer_revalidates_mutable_nested_data():
    """Writer revalidates mutable nested data."""
    original = report()
    original.execution.trials[0].artifact.result.flows[0].receiver.bytes = -1
    with pytest.raises(ReportValidationError):
        dumps_report(original)


def test_junit_error_failure_and_inconclusive_counts_and_xml_escaping():
    """Junit error failure and inconclusive counts and xml escaping."""
    original = report(
        [OSError("bad <&\x01"), measured(), measured()],
        policy=AssessmentPolicy(minimum_throughput_bps=900, minimum_valid_trials=2),
    )
    xml = ET.fromstring(render_junit(original))
    assert (
        xml.attrib["tests"] == "4" and xml.attrib["errors"] == "1" and xml.attrib["failures"] == "1"
    )
    assert xml.find("testcase/error").attrib["message"] == "bad <&\ufffd"
    assert "Performance: fail" in render_text(original)
    incomplete = report([result_from_iperf_json({}), measured(), measured()])
    assert ET.fromstring(render_junit(incomplete)).attrib["failures"] == "2"
    assert ET.fromstring(render_junit(incomplete, inconclusive="skipped")).attrib["skipped"] == "2"
    with pytest.raises(ValueError):
        render_junit(original, inconclusive="ignore")


def test_report_public_dicts_have_full_original_and_resolved_configuration():
    """Report public dicts have full original and resolved configuration."""
    data = report_to_dict(report())
    spec = data["execution"]["plan"]["trials"][0]
    assert set(spec) == {
        "trial_id",
        "cell_id",
        "phase",
        "repetition",
        "config",
        "rate_intent",
        "resolved_config",
    }
    assert spec["resolved_config"]["rate"] == spec["config"]["rate"] == 4000000
    assert data["assessment"]["algorithm_revision"] == "median-summary-v1"
    assert data["ci_exit_code"] == 0


def test_archive_keeps_original_compatibility_decision_not_latest_policy():
    """Archive keeps original compatibility decision not latest policy."""
    original = report()
    data = report_to_dict(original)
    # Compatibility is an archived decision, not a cryptographic certificate.
    # A future checker may have different supported methodology; loading retains it.
    data["assessment"]["compatibility"]["diagnostics"].append(
        {
            "code": "archive.context",
            "message": "Retained historical checker context",
            "evidence": [],
        }
    )
    restored = report_from_dict(data)
    assert restored.assessment.compatibility.diagnostics[-1].code == "archive.context"


def test_frozen_v1_golden_reports_are_readable_without_current_analysis(monkeypatch):
    """Frozen v1 golden reports are readable without current analysis."""
    import iperf3_lib.assessments as assessments

    def forbidden(*args, **kwargs):
        raise AssertionError("golden archive must not rerun analysis")

    monkeypatch.setattr(assessments, "summary_throughput", forbidden)
    monkeypatch.setattr(assessments, "check_compatibility", forbidden)
    fixtures = tuple((Path(__file__).parent / "fixtures/reports/v1").glob("*.json"))
    assert len(fixtures) >= 2
    for path in fixtures:
        text = path.read_text()
        restored = loads_report(text)
        assert json.loads(dumps_report(restored)) == json.loads(text)


def test_reusable_plan_codec_rejects_unexecuted_evidence_and_time_contradictions():
    """Reusable plan codec rejects unexecuted evidence and time contradictions."""
    original = report([OSError("bad"), measured(), measured()], stop_on_error=True)
    data = plan_result_to_dict(original.execution)
    data["trials"][1]["started_at_seconds"] = 1
    with pytest.raises(ReportValidationError):
        plan_result_from_dict(data)
    data = plan_result_to_dict(original.execution)
    data["elapsed_seconds"] = 0
    data["trials"][0]["elapsed_seconds"] = 1
    with pytest.raises(ReportValidationError):
        plan_result_from_dict(data)
    changed = replace(original.execution, stop_reason="elapsed_admission_limit")
    with pytest.raises(ReportValidationError):
        plan_result_to_dict(changed)


@pytest.mark.parametrize("field,value", [("rate", 123), ("protocol", "udp"), ("method", "reverse")])
def test_fingerprints_must_match_retained_canonical_settings(field, value):
    """Matching fingerprint identities alone cannot validate fabricated methodology."""
    data = report_to_dict(report())
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"][field] = value
    with pytest.raises(ReportValidationError, match="fingerprints"):
        report_from_dict(data)


def test_unknown_or_differing_fixed_settings_cannot_be_marked_compatible():
    """Frozen v1 policy verifies missing evidence and fixed differences on import."""
    data = report_to_dict(report())
    first = data["execution"]["trials"][0]["artifact"]["result"]
    first["execution"]["configuration"]["effective"].pop("rate")
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"]["rate"] = None
    with pytest.raises(ReportValidationError, match="compatibility"):
        report_from_dict(data)
    data = report_to_dict(report())
    first = data["execution"]["trials"][0]["artifact"]["result"]
    first["execution"]["configuration"]["effective"]["rate"]["value"] = 123
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"]["rate"] = 123
    with pytest.raises(ReportValidationError, match="compatibility"):
        report_from_dict(data)


def test_aggregate_rate_allocation_cannot_resolve_to_native_unlimited_zero():
    """An aggregate target below parallel cannot masquerade as a compatible plan."""
    data = report_to_dict(report())
    for index, record in enumerate(data["execution"]["trials"]):
        result = record["artifact"]["result"]
        result["extensions"]["iperf3_lib.rate_intent"] = {
            "schema_version": 1,
            "intent": {"aggregate_bps_per_direction": 1},
            "resolution": {"source": "aggregate"},
        }
        result["execution"]["configuration"]["effective"]["rate"]["value"] = 0
        fingerprint = data["assessment"]["compatibility"]["fingerprints"][
            f"trial:default:measured:{index}"
        ]
        fingerprint["rate"] = 0
        fingerprint["rate_intent"] = {"basis": "aggregate_per_direction", "target_bps": 1}
    with pytest.raises(ReportValidationError, match="compatibility"):
        report_from_dict(data)


def test_reusable_frozen_compatibility_codec_validates_retained_artifacts():
    """Other experiment envelopes share v1 provenance rules without private analysis."""
    from iperf3_lib.reports import compatibility_from_dict, compatibility_to_dict

    original = report()
    artifacts = {
        f"trial:{record.spec.trial_id}": record.artifact for record in original.execution.trials
    }
    data = compatibility_to_dict(original.assessment.compatibility, artifacts=artifacts)
    assert compatibility_from_dict(data, artifacts=artifacts) == original.assessment.compatibility
    with pytest.raises(ReportValidationError):
        compatibility_from_dict(data, artifacts={"": next(iter(artifacts.values()))})
    data["fingerprints"]["trial:default:measured:0"]["rate"] = 0
    with pytest.raises(ReportValidationError):
        compatibility_from_dict(data, artifacts=artifacts)


@pytest.mark.parametrize(
    "name,value",
    [
        ("rate", True),
        ("tos", 256),
        ("port", 0),
        ("duration", -1),
        ("parallel", 129),
        ("blksize", 1048577),
        ("server", " "),
    ],
)
def test_frozen_comparison_enforces_verified_setting_types_and_bounds(name, value):
    """Matching corrupted canonical and fingerprint settings still violate v1 semantics."""
    data = report_to_dict(report())
    data["execution"]["trials"][0]["artifact"]["result"]["execution"]["configuration"]["effective"][
        name
    ]["value"] = value
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"][name] = value
    with pytest.raises(ReportValidationError):
        report_from_dict(data)


def test_stop_on_error_history_cannot_execute_following_trials():
    """A serialized history must obey its admission stop policy."""
    data = report_to_dict(report([result_from_iperf_json({}), measured(), measured()]))
    data["execution"]["plan"]["policy"]["stop_on_error"] = True
    with pytest.raises(ReportValidationError, match="stop_on_error"):
        report_from_dict(data)


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_baseline_measurements_require_explicitly_completed_execution(status):
    """Stored byte counts cannot turn an unsuccessful baseline into a valid sample."""
    data = report_to_dict(report(baselines=[artifact_from_result(measured())]))
    result = data["baselines"][0]["result"]
    result["ok"] = False
    result["execution"]["status"] = status
    result["error"] = "native failed" if status == "failed" else None
    with pytest.raises(ReportValidationError):
        report_from_dict(data)


@pytest.mark.parametrize("value", [True, 1.0])
def test_stored_fingerprint_scalar_types_cannot_exploit_python_numeric_equality(value):
    """Boolean or floating fingerprint rates cannot equal a canonical integer rate."""
    data = report_to_dict(report())
    for index, record in enumerate(data["execution"]["trials"]):
        record["artifact"]["result"]["execution"]["configuration"]["effective"]["rate"]["value"] = 1
        data["assessment"]["compatibility"]["fingerprints"][f"trial:default:measured:{index}"][
            "rate"
        ] = 1
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"]["rate"] = value
    with pytest.raises(ReportValidationError, match="fingerprints"):
        report_from_dict(data)


@pytest.mark.parametrize("name", ["native_version", "native_system_info"])
def test_native_provenance_strings_cannot_be_whitespace(name):
    """Frozen v1 provenance validation retains nonempty native identity semantics."""
    data = report_to_dict(report())
    data["execution"]["trials"][0]["artifact"]["result"]["execution"][name] = " "
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"][name] = " "
    with pytest.raises(ReportValidationError):
        report_from_dict(data)


def _omit_eligible_measurement(original, *, baseline=False, index=0):
    """Construct internally recalculated selection tampering with retained original evidence."""
    candidates = list(original.assessment.measurements)
    baselines = list(original.assessment.baseline_measurements)
    omitted = (baselines if baseline else candidates).pop(index)
    artifacts = {
        f"trial:{record.spec.trial_id}": record.artifact for record in original.execution.trials
    }
    artifacts.update(
        {f"baseline:{index}": artifact for index, artifact in enumerate(original.baselines or ())}
    )
    compatibility = check_compatibility(
        [
            AnalysisTrial(item.trial_id, artifacts[item.trial_id].result)
            for item in candidates + baselines
        ],
        policy=original.comparison,
    )
    outcome, median, baseline_median, threshold, relative, reasons = _decision_v1(
        original.policy, candidates, baselines, original.baselines is not None, compatibility
    )
    return replace(
        original,
        assessment=replace(
            original.assessment,
            measurements=tuple(candidates),
            baseline_measurements=tuple(baselines),
            exclusions=original.assessment.exclusions
            + (ExcludedTrial(omitted.trial_id, "insufficient_summary_measurement"),),
            compatibility=compatibility,
            outcome=outcome,
            median_throughput_bps=median,
            baseline_median_bps=baseline_median,
            effective_minimum_bps=threshold,
            relative_change=relative,
            reasons=reasons,
        ),
    )


@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize("index", [0, 1])
def test_every_eligible_candidate_and_baseline_including_zero_must_be_selected(baseline, index):
    """Recomputed arithmetic cannot justify excluding a retained valid zero or nonzero."""
    import iperf3_lib.reports as codec

    original = report(
        [measured(0), measured(100)],
        baselines=[artifact_from_result(measured(0)), artifact_from_result(measured(100))],
        policy=AssessmentPolicy(minimum_valid_trials=1, minimum_throughput_bps=600),
    )
    changed = _omit_eligible_measurement(original, baseline=baseline, index=index)
    # Construct the untrusted payload without the public writer's semantic checks.
    data = codec._convert(type(changed), changed, "", encoding=True)
    with pytest.raises(ReportValidationError, match="eligible summaries"):
        report_from_dict(data)
    with pytest.raises(ReportValidationError, match="eligible summaries"):
        dumps_report(changed)


def test_omitting_measured_zero_cannot_turn_a_failed_assessment_into_a_passing_archive():
    """The original two-sample population fails even if its favorable subset passes."""
    original = report(
        [measured(0), measured(100)],
        policy=AssessmentPolicy(minimum_valid_trials=1, minimum_throughput_bps=600),
    )
    assert original.assessment.outcome == "fail"
    assert original.assessment.median_throughput_bps == 400
    changed = _omit_eligible_measurement(original)
    assert changed.assessment.outcome == "pass"
    assert changed.assessment.median_throughput_bps == 800
    with pytest.raises(ReportValidationError, match="eligible summaries"):
        dumps_report(changed)


@pytest.mark.parametrize(
    "results,options,trial_id,wrong_reason",
    [
        ([measured(), measured(), measured()], {"warmup_runs": 1}, "warmup:0", "execution_failed"),
        ([result_from_iperf_json({"error": "failed"}), measured()], {}, "measured:0", "warmup_run"),
        ([result_from_iperf_json({}), measured()], {}, "measured:0", "execution_failed"),
        ([OSError("failed"), measured()], {}, "measured:0", "insufficient_summary_measurement"),
        (
            [OSError("failed"), measured()],
            {"stop_on_error": True},
            "measured:1",
            "execution_exception",
        ),
    ],
)
def test_exclusion_reason_must_match_retained_phase_and_execution(
    results, options, trial_id, wrong_reason
):
    """A nonempty explanation must describe the actual retained trial status."""
    data = report_to_dict(report(results, **options))
    exclusion = next(
        item
        for item in data["assessment"]["exclusions"]
        if item["trial_id"] == f"trial:default:{trial_id}"
    )
    exclusion["reason"] = wrong_reason
    with pytest.raises(ReportValidationError, match="exclusion reason"):
        report_from_dict(data)


@pytest.mark.parametrize("missing", ["bytes", "duration", "zero_duration", "omitted", "receiver"])
def test_ineligible_candidate_and_baseline_summaries_remain_portable_without_current_analysis(
    monkeypatch, missing
):
    """The frozen selector preserves genuine absence and omission instead of inventing samples."""
    incomplete = measured()
    receiver = incomplete.flows[0].receiver
    if missing == "bytes":
        receiver.bytes = None
    elif missing == "duration":
        receiver.duration_seconds = None
    elif missing == "zero_duration":
        receiver.duration_seconds = 0
    elif missing == "omitted":
        receiver.omitted = True
    else:
        incomplete.flows[0].receiver = None
        incomplete.end.sum_received = None
    original = report(
        [incomplete, measured()],
        baselines=[artifact_from_result(incomplete), artifact_from_result(measured())],
        policy=AssessmentPolicy(minimum_valid_trials=1),
    )
    assert {item.trial_id for item in original.assessment.exclusions} == {
        "trial:default:measured:0",
        "baseline:0",
    }
    assert all(
        item.reason == "insufficient_summary_measurement" for item in original.assessment.exclusions
    )

    import iperf3_lib.analysis as analysis
    import iperf3_lib.assessments as assessments

    def forbidden(*args, **kwargs):
        raise AssertionError("frozen selection must not invoke current analysis")

    for module in (analysis, assessments):
        monkeypatch.setattr(module, "summary_throughput", forbidden)
        monkeypatch.setattr(module, "check_compatibility", forbidden)
    assert loads_report(dumps_report(original)) == original
    assert not summary_eligible_v1(
        artifact_from_result(incomplete), direction="client_to_server", observation="receiver"
    )


@pytest.mark.parametrize("raw", [{"error": "native failure"}, {}])
def test_failed_or_incomplete_baselines_are_legitimately_excluded(raw):
    """Unsuccessful baselines stay in the archive with their genuine selection reason."""
    original = report(
        baselines=[
            artifact_from_result(result_from_iperf_json(raw)),
            artifact_from_result(measured()),
        ]
    )
    assert original.assessment.exclusions[0].trial_id == "baseline:0"
    assert original.assessment.exclusions[0].reason == "insufficient_summary_measurement"
    assert loads_report(dumps_report(original)) == original
    data = report_to_dict(original)
    data["assessment"]["exclusions"][0]["reason"] = "execution_failed"
    with pytest.raises(ReportValidationError, match="exclusion reason"):
        report_from_dict(data)


def test_reusable_frozen_eligibility_validates_selection_and_artifact():
    """Other report envelopes share zero-preserving eligibility and canonical validation."""
    zero = artifact_from_result(measured(0))
    assert summary_eligible_v1(zero, direction="client_to_server", observation="receiver")
    assert not summary_eligible_v1(zero, direction="server_to_client", observation="receiver")
    for selection in (
        {"direction": "unknown", "observation": "receiver"},
        {"direction": "client_to_server", "observation": "unknown"},
    ):
        with pytest.raises(ReportValidationError, match="invalid summary"):
            summary_eligible_v1(zero, **selection)
    zero.result.flows[0].receiver.bytes = -1
    with pytest.raises(ReportValidationError):
        summary_eligible_v1(zero, direction="client_to_server", observation="receiver")
