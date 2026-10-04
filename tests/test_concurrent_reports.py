"""Independent admission-ledger validation for frozen-schema concurrent reports."""

import asyncio
import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Literal, Union

import pytest

from iperf3_lib._cancellation import _ExecutionControl
from iperf3_lib.artifacts import ArtifactProducer, artifact_from_result
from iperf3_lib.concurrent_execution import (
    ConcurrentExecutionPolicy,
    ConcurrentPlanCancelledError,
    ConcurrentPlanCleanupError,
    ConcurrentPlanReport,
    ConcurrentPlanResult,
    ConcurrentPlanTimeoutError,
    ConcurrentTrialRecord,
    prepare_concurrent_plan,
)
from iperf3_lib.concurrent_reports import (
    _field,
    concurrent_report_from_dict,
    concurrent_report_from_execution,
    concurrent_report_to_dict,
    dumps_concurrent_report,
    loads_concurrent_report,
    render_concurrent_junit,
    render_concurrent_text,
    snapshot_concurrent_execution,
)
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.events import NativeEvent
from iperf3_lib.exceptions import IperfCleanupError
from iperf3_lib.intent import RateIntent
from iperf3_lib.plan_reports import plan_report_from_dict, snapshot_plan_execution
from iperf3_lib.reports import (
    ReportValidationError,
    UnsupportedReportVersionError,
    plan_result_to_dict,
    report_from_dict,
)
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialException, TrialPolicy, TrialSpec, prepare_plan


def plan(*, stop_on_error=False, pause=0, limit=None, configs=None):
    """Declare two cells without asserting any real native traffic occurred."""
    configs = configs or [
        ClientConfig("A.example", duration=1, rate=800),
        ClientConfig("B.example", duration=1, rate=800),
    ]
    return prepare_plan(
        tuple(
            TrialSpec(f"{cell}-{i}", cell, "measured", i, config)
            for cell, config in zip(("a", "b"), configs, strict=True)
            for i in range(2)
        ),
        policy=TrialPolicy(repetitions=2, stop_on_error=stop_on_error, pause_seconds=pause),
        budget=PlanBudget(None, None, limit),
    )


def execution(mode=None):
    """Build overlapping completed runs or interleaved partial histories."""
    prepared, policy, resources, keys, rates = prepare_concurrent_plan(
        plan(stop_on_error=mode == "soft-cleanup"),
        ConcurrentExecutionPolicy(2, 1600),
        {"a": ("rack-a",)},
    )
    artifact = artifact_from_result(
        result_from_iperf_json(
            {
                "start": {"test_start": {"protocol": "TCP"}},
                "end": {"sum_received": {"bytes": 100, "seconds": 1, "bits_per_second": 800}},
            }
        )
    )
    stop = "stop_on_error" if mode == "soft-cleanup" else mode
    hard = "cleanup_failed" if mode == "soft-cleanup" else mode
    records = []
    for index, spec in enumerate(prepared.trials):
        if mode is not None and index in (1, 3):
            records.append(
                ConcurrentTrialRecord(spec, "not_run", keys[index], rates[index], reason=stop)
            )
            continue
        start, finish, admission = [(0, 2, 1), (2, 3, 4), (0.5, 1.5, 2), (1.5, 2.5, 3)][index]
        status, reason, exception, current_artifact, events = (
            "completed",
            None,
            None,
            copy.deepcopy(artifact),
            (),
        )
        if mode:
            finish = 2
            status = "timed_out" if mode == "timeout" else "cancelled"
            if mode == "cleanup_failed" and index == 0 or mode == "soft-cleanup" and index == 2:
                status = "cleanup_failed"
            reason = hard
            exception = TrialException("example.Interrupted", "stopped")
            current_artifact = None
            events = (NativeEvent("interval", {"bytes": 20}, 1, 101),)
        if mode == "soft-cleanup" and index == 0:
            finish, status, reason, events = 1, "exception", None, ()
        records.append(
            ConcurrentTrialRecord(
                spec,
                status,
                keys[index],
                rates[index],
                admission,
                start,
                finish,
                None if status == "cleanup_failed" else finish,
                artifact=current_artifact,
                exception=exception,
                reason=reason,
                started_at_seconds=100 + start,
                completed_at_seconds=100 + finish,
                elapsed_seconds=finish - start,
                cleanup_confirmed=status != "cleanup_failed",
                partial_events=events,
                events_observed=len(events),
            )
        )
    return ConcurrentPlanResult(
        prepared,
        policy,
        resources,
        tuple(records),
        100,
        99,
        3,
        stop,
        1.1 if mode == "soft-cleanup" else 0.75 if mode else None,
        hard,
        1.5 if mode == "soft-cleanup" else 0.75 if mode else None,
        0.75 if mode == "timeout" else None,
    )


def report(mode=None):
    """Keep report-writer identity separate from retained artifact provenance."""
    return ConcurrentPlanReport(
        execution(mode),
        ArtifactProducer("example.fixture", "3"),
        extensions={"example.synthetic": True},
    )


def exception_execution(*, returned_evidence=False):
    """Retain delivered intervals when no valid final result artifact exists."""
    current = execution()
    failed = replace(
        current.trials[0],
        status="exception",
        artifact=None,
        exception=TrialException("example.WorkerError", "no final artifact"),
        partial_events=(NativeEvent("interval", {"sum": {"bytes": 20}}, 1, 101),),
        events_observed=2,
        events_dropped=1,
        returned_result_evidence={"raw": {"end": {}}} if returned_evidence else None,
        diagnostics=("Result could not be encoded",) if returned_evidence else (),
    )
    return replace(current, trials=(failed, *current.trials[1:]))


@pytest.mark.parametrize("mode", [None, "cancelled", "timeout", "cleanup_failed", "soft-cleanup"])
def test_round_trip_overlap_partial_holes_and_unreleased_owners(mode):
    """Declared order survives independent admission order and interrupted peers."""
    original = report(mode)
    restored = loads_concurrent_report(dumps_concurrent_report(original))
    assert restored == original
    assert restored.execution.execution_success is (mode is None)
    assert restored.execution.cleanup_confirmed is (mode not in ("cleanup_failed", "soft-cleanup"))
    if mode != "soft-cleanup":
        assert sum(record.elapsed_seconds or 0 for record in original.execution.trials) > 3
    if mode:
        assert restored.execution.trials[1].status == "not_run"
        assert restored.execution.trials[2].status != "not_run"
    else:
        assert [record.admission_index for record in restored.execution.trials] == [1, 4, 2, 3]


def test_snapshots_and_exceptions_detach_evidence_and_preserve_all_owners():
    """Live controls remain retained while portable report evidence is copied."""
    original = execution("cancelled")
    token = object()
    error = ConcurrentPlanCancelledError(original, "first", token)
    assert isinstance(error, asyncio.CancelledError)
    assert error.args == ("first", token)
    original.resources["a"] = ("changed",)
    original.plan.trials[0].config.rate = 1
    original.trials[0].partial_events[0].data["bytes"] = 999
    assert error.partial_result.resources["a"] == ("rack-a",)
    assert error.partial_result.plan.trials[0].config.rate == 800
    assert error.partial_result.trials[0].partial_events[0].data["bytes"] == 20
    assert isinstance(ConcurrentPlanTimeoutError(execution("timeout")), TimeoutError)
    controls = tuple(IperfCleanupError("live", control=_ExecutionControl()) for _ in range(2))
    cause = asyncio.CancelledError("first")
    controls[0].__cause__ = cause
    cleanup = ConcurrentPlanCleanupError(execution("cleanup_failed"), controls)
    assert cleanup.cleanup_errors == controls
    assert cleanup.cleanup_errors[0] is controls[0]
    assert cleanup.cleanup_errors[0].__cause__ is cause
    with pytest.raises(ValueError):
        ConcurrentPlanCleanupError(execution("cleanup_failed"), ())


@pytest.mark.parametrize("values", [(True, 1), (1, False), (0, 1), (1, 0), (-1, 2), (1, 1.5)])
def test_policy_requires_explicit_positive_integer_ceilings(values):
    """Boolean and fractional caps cannot silently admit work."""
    with pytest.raises(ValueError, match="positive integer"):
        ConcurrentExecutionPolicy(*values)


@pytest.mark.parametrize(
    "resources",
    [
        {"unknown": ()},
        {"a": ["rack"]},
        {"a": ("",)},
        {"a": ("  ",)},
        {"a": ("rack", "rack")},
        {"a": (3,)},
    ],
)
def test_resource_inputs_reject_unknown_cells_ambiguous_values_and_duplicates(resources):
    """Every user reservation has an explicit known owner before any execution."""
    with pytest.raises(ValueError):
        prepare_concurrent_plan(plan(), ConcurrentExecutionPolicy(2, 1600), resources)


@pytest.mark.parametrize(
    "servers", [("EXAMPLE.org.", "example.org"), ("2001:0db8:0:0:0:0:0:1", "2001:db8::1")]
)
def test_endpoint_keys_normalize_spelling_without_dns_lookup(servers):
    """Case, trailing DNS dots and compressed IP forms share one reservation."""
    prepared = plan(configs=[ClientConfig(server, rate=800) for server in servers])
    _, _, extras, keys, _ = prepare_concurrent_plan(
        prepared, ConcurrentExecutionPolicy(2, 1600), {"a": ("endpoint:fake",)}
    )
    assert keys[0][0] == keys[2][0]
    assert keys[0][1] == "user:endpoint:fake"
    assert extras == {"a": ("endpoint:fake",)}


@pytest.mark.parametrize(
    "config",
    [
        ClientConfig("host", rate=None),
        ClientConfig("host", rate=0),
        ClientConfig("host", rate=1601),
        ClientConfig("host", rate=800, client_port=5555),
    ],
)
def test_unsupported_or_unbounded_admission_inputs_are_rejected(config):
    """All rate and source-port constraints are checked before native work."""
    with pytest.raises(ValueError):
        prepare_concurrent_plan(plan(configs=[config, config]), ConcurrentExecutionPolicy(2, 1600))


def test_preparation_rejects_pause_and_detaches_configuration_resources_and_intent():
    """Reservations use resolved streams and directions without mutating caller intent."""
    with pytest.raises(ValueError, match="zero pause"):
        prepare_concurrent_plan(plan(pause=1), ConcurrentExecutionPolicy(2, 1600))
    config = ClientConfig("host", duration=1, parallel=3, bidirectional=True)
    specs = (
        TrialSpec("one", "a", "measured", 0, config, RateIntent(aggregate_bps_per_direction=1000)),
    )
    prepared = prepare_plan(specs, policy=TrialPolicy(repetitions=1), budget=PlanBudget(None, None))
    resources = {"a": ("rack",)}
    detached, _, extras, _, targets = prepare_concurrent_plan(
        prepared, ConcurrentExecutionPolicy(1, 1998), resources
    )
    assert targets == (1998,)
    assert detached.trials[0].config is not prepared.trials[0].config
    assert detached.trials[0].rate_intent is not prepared.trials[0].rate_intent
    resources["a"] = ("changed",)
    assert extras == {"a": ("rack",)}
    udp = ClientConfig("host", protocol=Protocol.UDP)
    assert (
        prepare_concurrent_plan(plan(configs=[udp, udp]), ConcurrentExecutionPolicy(1, 2**20))[4]
        == (2**20,) * 4
    )


@pytest.mark.parametrize(
    "annotation",
    [
        Union[Literal["cancelled", "timeout"], None],  # noqa: UP007 - test 3.12 union representation
        Literal["cancelled", "timeout"] | None,
    ],
)
@pytest.mark.parametrize("encoding", [True, False])
def test_optional_literals_support_python312_and_newer_union_representations(annotation, encoding):
    """Cross-interpreter schema semantics do not depend on a union implementation."""
    assert _field(annotation, None, "/x", encoding=encoding) is None
    assert _field(annotation, "cancelled", "/x", encoding=encoding) == "cancelled"
    with pytest.raises(ReportValidationError):
        _field(annotation, True, "/x", encoding=encoding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("admission_index", 2),
        ("admission_index", 0),
        ("admission_index", True),
        ("admitted_offset_seconds", -1),
        ("finished_offset_seconds", 4),
        ("elapsed_seconds", 0.1),
        ("released_offset_seconds", 1.9),
        ("target_bps", 799),
        ("resource_keys", ["user:fake"]),
        ("cleanup_confirmed", False),
        ("events_observed", 1),
    ],
)
def test_malformed_record_reservation_or_evidence_is_rejected(field, value):
    """A persisted ledger cannot rewrite its admitted limits or cleanup evidence."""
    data = concurrent_report_to_dict(report())
    data["execution"]["trials"][0][field] = value
    with pytest.raises(ReportValidationError):
        concurrent_report_from_dict(data)


@pytest.mark.parametrize(
    "policy", [ConcurrentExecutionPolicy(1, 1600), ConcurrentExecutionPolicy(2, 1599)]
)
def test_recomputed_sweep_rejects_overlapping_worker_and_target_caps(policy):
    """Individually feasible trials must also fit their overlapping reservations."""
    with pytest.raises(ReportValidationError, match="exceed worker or target-rate"):
        snapshot_concurrent_execution(replace(execution(), policy=policy))


@pytest.mark.parametrize("shared", ["endpoint", "user"])
def test_recomputed_sweep_rejects_resource_conflicts(shared):
    """Automatic endpoints and user infrastructure keys both enforce exclusion."""
    current = execution()
    prepared = (
        plan(configs=[ClientConfig("same", rate=800)] * 2) if shared == "endpoint" else current.plan
    )
    resources = {"a": ("shared",), "b": ("shared",)} if shared == "user" else {}
    prepared, _, resources, keys, _ = prepare_concurrent_plan(prepared, current.policy, resources)
    records = tuple(
        replace(record, spec=spec, resource_keys=key)
        for record, spec, key in zip(current.trials, prepared.trials, keys, strict=True)
    )
    with pytest.raises(ReportValidationError, match="resource reservations overlap"):
        snapshot_concurrent_execution(
            replace(current, plan=prepared, resources=resources, trials=records)
        )


def test_equal_offsets_allow_release_before_admission_and_zero_duration_trials():
    """Clock-resolution ties neither invent overlap nor underflow the ledger."""
    current = execution()
    records = tuple(
        replace(
            record,
            admitted_offset_seconds=0,
            finished_offset_seconds=0,
            released_offset_seconds=0,
            elapsed_seconds=0,
            admission_index=index + 1,
        )
        for index, record in enumerate(current.trials)
    )
    assert snapshot_concurrent_execution(
        replace(current, policy=ConcurrentExecutionPolicy(1, 800), trials=records)
    ).execution_success
    assert (
        snapshot_concurrent_execution(current).trials[1].admitted_offset_seconds
        == current.trials[0].released_offset_seconds
    )


def test_unreleased_owner_at_snapshot_end_still_reserves_capacity():
    """Open reservations at the final instant cannot be treated as already freed."""
    current = execution("cleanup_failed")
    a, pending_a, b, pending_b = current.trials
    a = replace(a, admitted_offset_seconds=3, finished_offset_seconds=3, elapsed_seconds=0)
    b = replace(
        b,
        admission_index=1,
        admitted_offset_seconds=0,
        finished_offset_seconds=3,
        released_offset_seconds=None,
        elapsed_seconds=3,
        status="cleanup_failed",
        cleanup_confirmed=False,
    )
    a = replace(a, admission_index=2)
    current = replace(
        current,
        trials=(a, pending_a, b, pending_b),
        stop_reason="cleanup_failed",
        stopped_offset_seconds=3,
        termination_offset_seconds=3,
        policy=ConcurrentExecutionPolicy(1, 1600),
    )
    with pytest.raises(ReportValidationError, match="exceed worker"):
        snapshot_concurrent_execution(current)


def test_same_cell_admission_requires_released_predecessor():
    """Independent cells may finish out of order while each cell remains ordered."""
    current = execution()
    a, next_a, b, next_b = current.trials
    next_a = replace(
        next_a,
        admitted_offset_seconds=1,
        finished_offset_seconds=2,
        released_offset_seconds=2,
        elapsed_seconds=1,
        admission_index=3,
    )
    next_b = replace(next_b, admission_index=4)
    with pytest.raises(ReportValidationError, match="same-cell predecessors"):
        snapshot_concurrent_execution(replace(current, trials=(a, next_a, b, next_b)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("stopped_offset_seconds", None),
        ("stopped_offset_seconds", -0.1),
        ("termination_reason", None),
        ("termination_reason", "timeout"),
        ("termination_offset_seconds", 0.5),
        ("termination_offset_seconds", 4),
    ],
)
def test_stop_decision_pairs_cannot_be_rewritten(field, value):
    """The first admission stop and first hard stop retain distinct consistent evidence."""
    with pytest.raises(ReportValidationError):
        snapshot_concurrent_execution(replace(execution("cancelled"), **{field: value}))


def test_soft_limit_then_hard_cancellation_and_completed_delivery_race_are_valid():
    """A soft admission stop may precede termination even after all outcomes return."""
    current = execution()
    prepared = plan(limit=0.5)
    current = replace(
        current,
        plan=prepared,
        trials=tuple(
            replace(record, spec=spec)
            for record, spec in zip(current.trials, prepared.trials, strict=True)
        ),
        stop_reason="elapsed_admission_limit",
        stopped_offset_seconds=2,
        termination_reason="cancelled",
        termination_offset_seconds=2.8,
    )
    assert not snapshot_concurrent_execution(current).execution_success
    with pytest.raises(ReportValidationError, match="admitted after"):
        snapshot_concurrent_execution(replace(current, stopped_offset_seconds=0.5))


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":3,"schema_version":3}',
        '{"schema_version":NaN}',
        '{"schema_version":Infinity}',
        "{",
    ],
)
def test_strict_json_rejects_duplicates_nonfinite_values_and_truncation(text):
    """Malformed transport text never reaches report construction."""
    with pytest.raises(ReportValidationError):
        loads_concurrent_report(text)


def test_schema_boundary_rejects_v1_v2_and_unknown_fields_without_coercion():
    """New concurrency evidence cannot masquerade as historical sequential history."""
    data = concurrent_report_to_dict(report())
    for reader in (plan_report_from_dict, report_from_dict):
        with pytest.raises(UnsupportedReportVersionError):
            reader(data)
    for encode in (snapshot_plan_execution, plan_result_to_dict):
        with pytest.raises(ReportValidationError):
            encode(execution())
    for key, value in (
        ("schema_version", 2),
        ("schema_version", True),
        ("execution_mode", "sequential-owned"),
        ("pause_semantics", "global_completion_to_start"),
        ("unknown", 1),
    ):
        bad = dict(data, **{key: value})
        with pytest.raises(ReportValidationError):
            concurrent_report_from_dict(bad)


def test_renderers_preserve_partial_outcomes_without_assessment():
    """Human and JUnit views retain cancellation errors and explicit skips."""
    current = report("cancelled")
    assert "bounded-concurrent-owned" in render_concurrent_text(current)
    root = ET.fromstring(render_concurrent_junit(current))
    assert root.attrib["errors"] == "3"
    assert root.attrib["skipped"] == "2"
    assert root.attrib["time"] == "3"
    assert len(root.findall("testcase/system-out")) == 3
    assert json.loads(root.findall("testcase/system-out")[0].text)["partial_events"][0]["data"] == {
        "bytes": 20
    }
    assert concurrent_report_from_execution(execution()).producer.name == "iperf3-lib"


@pytest.mark.parametrize("returned_evidence", [False, True])
def test_exception_events_round_trip_and_render_without_becoming_result_artifacts(
    returned_evidence,
):
    """Crash diagnostics and unencodable final evidence both survive strict v3 export."""
    original = ConcurrentPlanReport(
        exception_execution(returned_evidence=returned_evidence), ArtifactProducer("fixture", "3")
    )
    restored = loads_concurrent_report(dumps_concurrent_report(original))
    assert restored == original
    record = restored.execution.trials[0]
    assert record.status == "exception" and record.artifact is None
    assert record.events_observed == 2 and record.events_dropped == 1
    assert restored.execution.cleanup_confirmed and not restored.execution.execution_success
    root = ET.fromstring(render_concurrent_junit(restored))
    first = root.find("testcase")
    assert first.find("error").attrib["type"] == "exception"
    evidence = json.loads(first.find("system-out").text)
    assert evidence["partial_events"][0]["data"] == {"sum": {"bytes": 20}}
    if returned_evidence:
        assert evidence["returned_result_evidence"] == record.returned_result_evidence
    else:
        assert set(evidence) == {"partial_events"}
    original.execution.trials[0].partial_events[0].data["sum"]["bytes"] = 999
    assert record.partial_events[0].data == {"sum": {"bytes": 20}}


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete", "not_run"])
def test_partial_events_cannot_be_added_to_native_or_unstarted_outcomes(status):
    """Exception support does not allow diagnostic events to masquerade as native artifacts."""
    current = execution("cancelled") if status == "not_run" else execution()
    index = 1 if status == "not_run" else 0
    record = replace(
        current.trials[index],
        status=status,
        partial_events=(NativeEvent("interval", {}, 1, 100),),
        events_observed=1,
    )
    records = list(current.trials)
    records[index] = record
    with pytest.raises(ReportValidationError, match="only exception or interrupted trials"):
        snapshot_concurrent_execution(replace(current, trials=tuple(records)))


@pytest.mark.parametrize("status", ["cancelled", "exception"])
@pytest.mark.parametrize(
    "change",
    [
        {"events_observed": 0},
        {"events_dropped": -1},
        {"partial_events": (NativeEvent("", {}, 1, 100),)},
        {"partial_events": (NativeEvent("interval", {}, 0, 100),)},
        {"partial_events": (NativeEvent("interval", {}, 1, float("nan")),)},
        {"partial_events": (NativeEvent("interval", {"data": "x" * 65536}, 1, 100),)},
        {
            "partial_events": tuple(NativeEvent("interval", {}, i + 1, 100) for i in range(65)),
            "events_observed": 65,
        },
        {
            "partial_events": tuple(
                NativeEvent("interval", {"data": "x" * 33000}, i + 1, 100) for i in range(32)
            ),
            "events_observed": 32,
        },
        {
            "partial_events": (
                NativeEvent("interval", {}, 1, 100),
                NativeEvent("interval", {}, 1, 100),
            ),
            "events_observed": 2,
        },
    ],
)
def test_partial_event_counts_prefix_order_and_encoded_limits_are_validated(change, status):
    """Exception and interruption evidence obey the same bounded prefix as execution."""
    current = execution("cancelled") if status == "cancelled" else exception_execution()
    if status == "exception":
        current = replace(
            current,
            trials=(
                replace(current.trials[0], events_observed=1, events_dropped=0),
                *current.trials[1:],
            ),
        )
    record = replace(current.trials[0], **change)
    with pytest.raises(ReportValidationError):
        snapshot_concurrent_execution(replace(current, trials=(record, *current.trials[1:])))


@pytest.mark.parametrize(
    "mode,status,reason",
    [
        ("timeout", "cancelled", "timeout"),
        ("cancelled", "timed_out", "cancelled"),
        ("cancelled", "cancelled", "cleanup_failed"),
    ],
)
def test_interrupted_outcome_status_and_reason_match_first_hard_stop(mode, status, reason):
    """A deadline remains distinguishable from cancellation in archived outcomes."""
    current = execution(mode)
    record = replace(current.trials[0], status=status, reason=reason)
    with pytest.raises(ReportValidationError):
        snapshot_concurrent_execution(replace(current, trials=(record, *current.trials[1:])))


def test_cleanup_failure_cannot_fabricate_release_or_hide_hard_termination():
    """Retained unconfirmed owners force interruption and an open reservation."""
    current = execution("cleanup_failed")
    record = replace(current.trials[0], released_offset_seconds=2)
    with pytest.raises(ReportValidationError):
        snapshot_concurrent_execution(replace(current, trials=(record, *current.trials[1:])))
    with pytest.raises(ReportValidationError):
        snapshot_concurrent_execution(
            replace(current, termination_reason=None, termination_offset_seconds=None)
        )


def test_declared_dependency_hole_cannot_be_filled_by_a_later_same_cell_trial():
    """A skipped predecessor forbids starting its own successor even with free resources."""
    current = execution("cancelled")
    a, pending_a, b, pending_b = current.trials
    records = (replace(pending_a, spec=a.spec), replace(a, spec=pending_a.spec), b, pending_b)
    with pytest.raises(ReportValidationError, match="same-cell predecessors"):
        snapshot_concurrent_execution(replace(current, trials=records))


def test_schema_three_frozen_fixture_preserves_partial_reservations():
    """A stored v3 document remains readable independently of current construction helpers."""
    stored = (
        Path(__file__)
        .with_name("fixtures")
        .joinpath("concurrent-plan-v3.json")
        .read_text(encoding="utf-8")
    )
    restored = loads_concurrent_report(stored)
    assert restored.execution.stop_reason == "stop_on_error"
    assert restored.execution.termination_reason == "cleanup_failed"
    assert [record.status for record in restored.execution.trials] == [
        "exception",
        "not_run",
        "cleanup_failed",
        "not_run",
    ]
    assert restored.execution.trials[2].released_offset_seconds is None
    assert json.loads(dumps_concurrent_report(restored)) == json.loads(stored)


@pytest.mark.parametrize("stop,offset", [(None, None), ("stop_on_error", 2.5)])
def test_stop_on_error_rejects_missing_stop_and_admission_after_observed_failure(stop, offset):
    """A delayed or absent stop marker cannot conceal admission after consumed failure."""
    current = execution()
    prepared = plan(stop_on_error=True)
    records = list(current.trials)
    records[2] = replace(
        records[2],
        status="exception",
        artifact=None,
        exception=TrialException("example.Error", "failed"),
    )
    with pytest.raises(ReportValidationError, match="stop admission|after an observed failure"):
        snapshot_concurrent_execution(
            replace(
                current,
                plan=prepared,
                trials=tuple(records),
                stop_reason=stop,
                stopped_offset_seconds=offset,
            )
        )


def test_stop_on_error_preserves_equal_clock_resolution_admissions():
    """Equal timestamps do not invent an ordering beyond recorded clock resolution."""
    current = execution()
    prepared = plan(stop_on_error=True)
    records = list(current.trials)
    records[2] = replace(
        records[2],
        status="exception",
        artifact=None,
        exception=TrialException("example.Error", "failed"),
        finished_offset_seconds=2,
        released_offset_seconds=2,
        elapsed_seconds=1.5,
    )
    records[3] = replace(records[3], admitted_offset_seconds=2, elapsed_seconds=0.5)
    restored = snapshot_concurrent_execution(
        replace(
            current,
            plan=prepared,
            trials=tuple(records),
            stop_reason="stop_on_error",
            stopped_offset_seconds=2,
        )
    )
    assert restored.trials[1].admitted_offset_seconds == restored.trials[2].finished_offset_seconds


@pytest.mark.parametrize("stop", ["cancelled", "timeout", "cleanup_failed"])
def test_stop_on_error_preserves_an_earlier_hard_stop(stop):
    """Unsuccessful interrupted outcomes do not replace the first forced termination."""
    current = execution(stop)
    restored = snapshot_concurrent_execution(replace(current, plan=plan(stop_on_error=True)))
    assert restored.stop_reason == restored.termination_reason == stop
