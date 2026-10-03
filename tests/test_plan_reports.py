"""Strict v2 partial execution archives without changing frozen v1 reports."""

import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Literal, Union, get_type_hints

import pytest

from iperf3_lib._cancellation import _ExecutionControl
from iperf3_lib._ipc import encode_frame
from iperf3_lib.analysis import ComparisonPolicy
from iperf3_lib.artifacts import ArtifactProducer, artifact_from_result
from iperf3_lib.assessments import AssessmentPolicy, assess_plan
from iperf3_lib.config import ClientConfig
from iperf3_lib.events import NativeEvent
from iperf3_lib.exceptions import IperfCleanupError
from iperf3_lib.plan_execution import (
    PlanCancelledError,
    PlanCleanupError,
    PlanExecutionReport,
    PlanExecutionResult,
    PlanTimeoutError,
    PlanTrialRecord,
)
from iperf3_lib.plan_reports import (
    _field,
    dumps_plan_report,
    loads_plan_report,
    plan_report_from_dict,
    plan_report_from_execution,
    plan_report_to_dict,
    render_plan_junit,
    render_plan_text,
    snapshot_plan_execution,
)
from iperf3_lib.reports import (
    ReportValidationError,
    UnsupportedReportVersionError,
    plan_result_to_dict,
    report_from_dict,
    report_to_dict,
)
from iperf3_lib.result import Result, result_from_iperf_json
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, summarize_sweep
from iperf3_lib.trials import PlanBudget, TrialException, TrialPolicy, prepare_trials


def execution(stop=None, *, paused=False):
    """Build synthetic timing and native-shaped evidence without claiming native execution."""
    plan = prepare_trials(
        ClientConfig("127.0.0.1", duration=1, rate=800),
        policy=TrialPolicy(repetitions=3),
        budget=PlanBudget(3, 300),
    )
    artifact = artifact_from_result(
        result_from_iperf_json(
            {
                "start": {"test_start": {"protocol": "TCP"}},
                "end": {"sum_received": {"bytes": 100, "seconds": 1, "bits_per_second": 800}},
            }
        )
    )
    records = []
    for index, spec in enumerate(plan.trials):
        if stop is not None and (index > 1 or paused and index > 0):
            records.append(PlanTrialRecord(spec, "not_run", reason=stop))
        elif stop is not None and index == 1:
            status = {
                "cancelled": "cancelled",
                "timeout": "timed_out",
                "cleanup_failed": "cleanup_failed",
            }[stop]
            records.append(
                PlanTrialRecord(
                    spec,
                    status,
                    reason=stop,
                    started_at_seconds=102,
                    completed_at_seconds=103,
                    elapsed_seconds=1,
                    exception=TrialException(
                        "iperf3_lib.exceptions.IperfCleanupError", "still owned"
                    )
                    if status == "cleanup_failed"
                    else None,
                    cleanup_confirmed=status != "cleanup_failed",
                    partial_events=(NativeEvent("interval", {"bytes": 5}, 1, 102.5),),
                    events_observed=2,
                    events_dropped=1,
                )
            )
        else:
            records.append(
                PlanTrialRecord(
                    spec,
                    "completed",
                    copy.deepcopy(artifact),
                    started_at_seconds=100 + index * 2,
                    completed_at_seconds=101 + index * 2,
                    elapsed_seconds=1,
                )
            )
    return PlanExecutionResult(
        plan,
        tuple(records),
        100,
        106,
        6,
        0,
        stop,
        timeout_seconds=3 if stop == "timeout" else None,
    )


def report(stop=None, *, paused=False):
    """Keep report provenance distinct from retained native artifact provenance."""
    return PlanExecutionReport(
        execution(stop, paused=paused),
        ArtifactProducer("example.fixture", "2"),
        extensions={"example.synthetic": {"purpose": "codec regression"}},
    )


@pytest.mark.parametrize(
    "annotation",
    [
        Union[Literal["cancelled", "timeout"], None],  # noqa: UP007 - exercise Python 3.12's representation
        Literal["cancelled", "timeout"] | None,
        get_type_hints(PlanExecutionResult)["stop_reason"],
    ],
)
@pytest.mark.parametrize("encoding", [False, True])
def test_optional_literal_annotations_work_across_python_union_representations(
    annotation, encoding
):
    """The 3.12 typing.Union spelling and newer union objects share v2 semantics."""
    assert _field(annotation, None, "/stop_reason", encoding=encoding) is None
    assert _field(annotation, "cancelled", "/stop_reason", encoding=encoding) == "cancelled"
    with pytest.raises(ReportValidationError):
        _field(annotation, "unexpected", "/stop_reason", encoding=encoding)
    with pytest.raises(ReportValidationError):
        _field(annotation, False, "/stop_reason", encoding=encoding)


@pytest.mark.parametrize("stop", [None, "cancelled", "timeout", "cleanup_failed"])
def test_round_trip_retains_partial_outcomes_artifacts_events_and_producers(stop):
    """Every declared record and copied evidence survives a detached strict archive."""
    original = report(stop)
    restored = loads_plan_report(dumps_plan_report(original, indent=2))
    assert restored == original
    assert restored.execution.execution_success is (stop is None)
    assert restored.execution.cleanup_confirmed is (stop != "cleanup_failed")
    assert restored.producer == ArtifactProducer("example.fixture", "2")
    assert restored.execution.trials[0].artifact.producer.name == "iperf3-lib"
    encoded = plan_report_to_dict(original)
    encoded["execution"]["trials"][0]["artifact"]["result"]["raw"]["changed"] = True
    assert "changed" not in restored.execution.trials[0].artifact.result.raw


def test_snapshot_and_exception_evidence_are_detached_and_cancellation_args_survive():
    """A caller's subsequent mutation cannot rewrite already raised partial evidence."""
    original = execution("cancelled")
    token = object()
    error = PlanCancelledError(original, "first cancel", token)
    assert error.args == ("first cancel", token)
    original.plan.trials[0].config.rate = 7
    original.trials[1].partial_events[0].data["bytes"] = 99
    original.trials[0].artifact.result.raw["changed"] = True
    assert error.partial_result.plan.trials[0].config.rate == 800
    assert error.partial_result.trials[1].partial_events[0].data["bytes"] == 5
    assert "changed" not in error.partial_result.trials[0].artifact.result.raw
    assert PlanCancelledError(execution("cancelled")).args == ()
    assert isinstance(PlanTimeoutError(execution("timeout")), TimeoutError)


def test_legacy_result_acquires_explicit_metadata_before_plan_reporting():
    """Canonical normalization closes the missing-metadata path before history encoding."""
    original = execution()
    legacy = Result(ok=False, raw={})
    artifact = artifact_from_result(legacy)
    assert legacy.execution is None
    assert artifact.result.execution.status == "incomplete"
    assert any(item.code == "compatibility.inferred_status" for item in artifact.result.diagnostics)
    record = replace(
        original.trials[0],
        status="incomplete",
        artifact=artifact,
    )
    current = replace(original, trials=(record, *original.trials[1:]))
    restored = loads_plan_report(dumps_plan_report(plan_report_from_execution(current)))
    assert restored.execution == current
    assert not restored.execution.execution_success
    artifact.result.execution = None
    with pytest.raises(
        ReportValidationError, match="canonical artifacts require execution metadata"
    ):
        snapshot_plan_execution(current)
    encoded = plan_report_to_dict(restored)
    encoded["execution"]["trials"][0]["artifact"]["result"]["execution"] = None
    with pytest.raises(
        ReportValidationError, match="canonical artifacts require execution metadata"
    ):
        plan_report_from_dict(encoded)


def test_cleanup_exception_retains_every_original_control_error_and_cause():
    """Detached reporting cannot replace or discard live ownership evidence."""
    originals = tuple(
        IperfCleanupError(str(index), control=_ExecutionControl()) for index in range(2)
    )
    first_cause = TimeoutError("first deadline")
    originals[0].__cause__ = first_cause
    error = PlanCleanupError(execution("cleanup_failed"), originals)
    assert error.cleanup_errors == originals
    assert all(
        retained is original
        for retained, original in zip(error.cleanup_errors, originals, strict=True)
    )
    assert error.cleanup_errors[0].__cause__ is first_cause
    assert error.cleanup_errors[0]._control is originals[0]._control
    assert not error.partial_result.cleanup_confirmed
    with pytest.raises(ValueError, match="cleanup_errors"):
        PlanCleanupError(execution("cleanup_failed"), ())


@pytest.mark.parametrize("stop", ["cancelled", "timeout"])
def test_completed_result_race_and_cleanup_failure_keep_first_stop_reason(stop):
    """A native completion does not erase an already selected plan interruption."""
    completed = replace(
        execution(), stop_reason=stop, timeout_seconds=3 if stop == "timeout" else None
    )
    assert not snapshot_plan_execution(completed).execution_success
    cleanup = replace(
        execution("cleanup_failed"),
        stop_reason=stop,
        timeout_seconds=3 if stop == "timeout" else None,
    )
    records = (
        cleanup.trials[0],
        replace(cleanup.trials[1], reason=stop),
        replace(cleanup.trials[2], reason=stop),
    )
    snapshot = snapshot_plan_execution(replace(cleanup, trials=records))
    assert snapshot.stop_reason == stop and not snapshot.cleanup_confirmed


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.pop("pause_semantics"),
        lambda d: d.update(execution_mode="concurrent"),
        lambda d: d.update(schema_version=True),
        lambda d: d["producer"].update(name=""),
        lambda d: d["extensions"].update(unscoped=True),
        lambda d: d["execution"].update(execution_success=True),
        lambda d: d["execution"].update(cleanup_confirmed=False),
        lambda d: d["execution"].update(elapsed_seconds=0),
        lambda d: d["execution"].update(timeout_seconds=True),
        lambda d: d["execution"].update(timeout_seconds=0),
        lambda d: d["execution"].update(stop_reason="invented"),
        lambda d: d["execution"]["trials"].pop(),
        lambda d: d["execution"]["trials"][1].update(cleanup_confirmed=False),
        lambda d: d["execution"]["trials"][1].update(reason=None),
        lambda d: d["execution"]["trials"][1].update(reason="other stop"),
        lambda d: d["execution"]["trials"][1].update(
            artifact=d["execution"]["trials"][0]["artifact"]
        ),
        lambda d: d["execution"]["trials"][1].update(started_at_seconds=None),
        lambda d: d["execution"]["trials"][1].update(events_observed=3),
        lambda d: d["execution"]["trials"][1].update(events_dropped=-1),
        lambda d: d["execution"]["trials"][1].update(events_observed=True),
        lambda d: d["execution"]["trials"][1]["partial_events"][0].update(sequence=0),
        lambda d: d["execution"]["trials"][1]["partial_events"][0].update(sequence=True),
        lambda d: d["execution"]["trials"][1]["partial_events"][0].update(
            received_at_seconds=float("nan")
        ),
        lambda d: d["execution"]["trials"][1]["partial_events"][0].update(kind=""),
        lambda d: d["execution"]["trials"][2].update(elapsed_seconds=0),
        lambda d: d["execution"]["trials"][2].update(reason="other stop"),
    ],
)
def test_contradictory_fields_and_fabricated_interruption_evidence_are_rejected(mutate):
    """Strict metadata and causal state are checked rather than trusted from JSON."""
    data = plan_report_to_dict(report("cancelled"))
    mutate(data)
    with pytest.raises(ReportValidationError):
        plan_report_from_dict(data)


def test_events_require_increasing_sequence_count_bytes_and_interrupted_status():
    """Retention has count and serialized-byte limits independent of native data size."""
    original = execution("cancelled")
    middle = original.trials[1]

    def candidate(events):
        changed = replace(
            middle, partial_events=events, events_observed=len(events), events_dropped=0
        )
        return replace(original, trials=(original.trials[0], changed, original.trials[2]))

    for events in (
        (NativeEvent("interval", {}, 1, 1), NativeEvent("interval", {}, 1, 2)),
        tuple(NativeEvent("interval", {}, i + 1, 1) for i in range(65)),
        (NativeEvent("interval", "x" * 65536, 1, 1),),
        tuple(NativeEvent("interval", "x" * 64000, i + 1, 1) for i in range(17)),
    ):
        with pytest.raises(ReportValidationError):
            snapshot_plan_execution(candidate(events))
    # A body fitting encode_frame's payload cap can still exceed the complete
    # retained-frame cap once its four-byte header is included.
    event = {"kind": "interval", "data": "", "sequence": 1, "received_at_seconds": 1}
    event["data"] = "x" * (65536 - len(encode_frame(event)) + 1)
    assert len(encode_frame(event)) == 65537
    with pytest.raises(ReportValidationError, match="header"):
        snapshot_plan_execution(candidate((NativeEvent(**event),)))
    events = tuple(NativeEvent("interval", {}, i + 1, 1) for i in range(64))
    assert len(snapshot_plan_execution(candidate(events)).trials[1].partial_events) == 64
    records = (
        replace(original.trials[0], partial_events=events, events_observed=64),
        *original.trials[1:],
    )
    with pytest.raises(ReportValidationError, match="only interrupted"):
        snapshot_plan_execution(replace(original, trials=records))


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":2,"schema_version":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        "[]",
        "{",
        b"\xff",
        1,
    ],
)
def test_malformed_json_is_rejected(text):
    """Duplicate fields and nonfinite values never acquire report meaning."""
    with pytest.raises(ReportValidationError):
        loads_plan_report(text)


def test_reading_does_not_load_native_or_reparse_retained_results(monkeypatch):
    """Archive loading validates recorded values without reexecuting analysis or traffic."""
    import iperf3_lib.ffi.api as native
    import iperf3_lib.result as result_module

    text = dumps_plan_report(report("cancelled"))

    class Forbidden:
        def __getattr__(self, name):
            pytest.fail("report loading touched native code")

    monkeypatch.setattr(native, "lib", Forbidden())
    monkeypatch.setattr(
        result_module,
        "result_from_iperf_json",
        lambda *args, **kwargs: pytest.fail("reparsed native JSON"),
    )
    assert loads_plan_report(text).execution.stop_reason == "cancelled"


def test_v1_fixtures_stay_unchanged_and_readers_reject_other_versions():
    """Separate dataclasses/codecs preserve the archived assessment-v1 contract."""
    directory = Path(__file__).parent / "fixtures" / "reports" / "v1"
    for path in directory.glob("*.json"):
        retained = json.loads(path.read_text(encoding="utf-8"))
        assert report_to_dict(report_from_dict(retained)) == retained
        with pytest.raises(UnsupportedReportVersionError):
            plan_report_from_dict(retained)
    with pytest.raises(UnsupportedReportVersionError):
        report_from_dict(plan_report_to_dict(report()))


def test_frozen_v2_fixture_preserves_its_exact_schema_and_partial_evidence():
    """A retained fixture detects accidental field or archive-contract drift."""
    path = Path(__file__).parent / "fixtures" / "plan-reports" / "v2-cancelled-synthetic.json"
    retained = json.loads(path.read_text(encoding="utf-8"))
    assert plan_report_to_dict(plan_report_from_dict(retained)) == retained
    current = plan_report_to_dict(report("cancelled"))
    current["execution"]["trials"][0]["artifact"]["producer"]["version"] = "fixture-development"
    assert current == retained


def test_v1_execution_assessment_and_sweep_apis_reject_v2_without_coercion():
    """The new result type cannot silently pass through old sequential report contracts."""
    current = execution()
    with pytest.raises(ReportValidationError, match="PlanResult"):
        plan_result_to_dict(current)
    with pytest.raises(ValueError, match="PlanResult"):
        assess_plan(
            current,
            policy=AssessmentPolicy(),
            comparison=ComparisonPolicy("synthetic-plan", ("client", "server")),
        )
    prepared = prepare_sweep(
        ClientConfig("127.0.0.1", duration=1, rate=800),
        [SweepAxis("parallel", (1,))],
        policy=TrialPolicy(repetitions=1),
        budget=PlanBudget(1, 100),
    )
    with pytest.raises(ReportValidationError, match="PlanResult"):
        summarize_sweep(prepared, current)


@pytest.mark.parametrize("paused", [False, True])
def test_execution_renderers_cannot_hide_cancellation_as_success(paused):
    """Even cancellation during a pause produces an explicit JUnit plan error."""
    original = report("cancelled", paused=paused)
    assert "Execution success=False" in render_plan_text(original)
    root = ET.fromstring(render_plan_junit(original))
    assert int(root.attrib["errors"]) > 0
    assert root.find("testcase[@name='plan_execution']/error").attrib["type"] == "cancelled"
    assert root.findall("testcase/skipped")
    successful = ET.fromstring(render_plan_junit(report()))
    assert successful.attrib["errors"] == "0"
    assert plan_report_from_execution(original.execution).execution == original.execution
