"""Strict schema-three reports with independently checked admission reservations."""

from __future__ import annotations

import json
import math
import types
import xml.etree.ElementTree as ET
from dataclasses import fields
from importlib.metadata import version
from typing import Any, Union, get_args, get_origin, get_type_hints

from ._ipc import IPCError, encode_frame
from .artifacts import ArtifactProducer, dumps_artifact
from .concurrent_execution import (
    ConcurrentExecutionPolicy,
    ConcurrentPlanReport,
    ConcurrentPlanResult,
    ConcurrentTrialRecord,
    prepare_concurrent_plan,
)
from .events import NativeEvent
from .plan_reports import _NAMESPACE, _STATUSES, _fail, _object
from .plan_reports import _field as _scalar_field
from .reports import UnsupportedReportVersionError, _xml_text
from .trials import _json_evidence

_MODELS = {
    ConcurrentExecutionPolicy,
    ConcurrentPlanReport,
    ConcurrentPlanResult,
    ConcurrentTrialRecord,
}
_HARD = {"cancelled", "timeout", "cleanup_failed"}
_INTERRUPTED = {"cancelled", "timed_out", "cleanup_failed"}
_PARTIAL_EVENTS = _INTERRUPTED | {"exception"}


def _field(spec, value, path, *, encoding):
    if spec in _MODELS:
        return _model(spec, value, path, encoding=encoding)
    args = get_args(spec)
    if get_origin(spec) in (types.UnionType, Union):
        if value is None and type(None) in args:
            return None
        for option in args:
            if option is not type(None):
                return _field(option, value, path, encoding=encoding)
    if get_origin(spec) is tuple and args[0] in _MODELS:
        if type(value) is not (tuple if encoding else list):
            _fail(path, "expected an array")
        result = [
            _model(args[0], item, f"{path}/{index}", encoding=encoding)
            for index, item in enumerate(value)
        ]
        return result if encoding else tuple(result)
    return _scalar_field(spec, value, path, encoding=encoding)


def _model(model, value, path, *, encoding):
    declared, hints = fields(model), get_type_hints(model)
    extra = {"execution_success", "cleanup_confirmed"} if model is ConcurrentPlanResult else set()
    if encoding:
        if not isinstance(value, model):
            _fail(path, f"expected {model.__name__}")
        data = {item.name: getattr(value, item.name) for item in declared}
    else:
        data = _object(value, path)
        if set(data) != {item.name for item in declared} | extra:
            _fail(path, "object must contain exactly the declared plan-v3 fields")
    values = {
        item.name: _field(
            hints[item.name], data[item.name], f"{path}/{item.name}", encoding=encoding
        )
        for item in declared
    }
    if encoding:
        for name in extra:
            values[name] = getattr(value, name)
        return values
    try:
        instance = model(**values)
    except (ValueError, TypeError) as exc:
        _fail(path, str(exc))
    for name in extra:
        stored = _field(bool, data[name], f"{path}/{name}", encoding=False)
        if stored != getattr(instance, name):
            _fail(f"{path}/{name}", "derived outcome disagrees with retained trials")
    return instance


def _events(record: ConcurrentTrialRecord, path: str) -> None:
    if record.events_observed < 0 or record.events_dropped < 0:
        _fail(path, "event delivery counts must be nonnegative")
    if record.events_observed != len(record.partial_events) + record.events_dropped:
        _fail(path, "observed events must equal retained events plus retention drops")
    if record.status not in _PARTIAL_EVENTS:
        if record.partial_events or record.events_observed or record.events_dropped:
            _fail(path, "only exception or interrupted trials retain partial event evidence")
        return
    if len(record.partial_events) > 64:
        _fail(f"{path}/partial_events", "partial event retention exceeds 64 events")
    previous = total = 0
    for index, event in enumerate(record.partial_events):
        event_path = f"{path}/partial_events/{index}"
        if not event.kind.strip() or event.sequence <= previous:
            _fail(event_path, "event kind must be nonempty and sequences strictly increasing")
        previous = event.sequence
        try:
            frame = encode_frame(
                {
                    "kind": event.kind,
                    "data": event.data,
                    "sequence": event.sequence,
                    "received_at_seconds": event.received_at_seconds,
                },
                max_bytes=65536,
            )
        except IPCError as exc:
            _fail(event_path, f"invalid bounded partial event: {exc}")
        if len(frame) > 65536:
            _fail(event_path, "partial event retention exceeds 64 KiB including its header")
        total += len(frame)
    if total > 1024 * 1024:
        _fail(f"{path}/partial_events", "partial event retention exceeds 1 MiB")


def _validate_outcome(
    record: ConcurrentTrialRecord, execution: ConcurrentPlanResult, path: str
) -> None:
    _events(record, path)
    if record.exception is not None and not record.exception.type_name.strip():
        _fail(f"{path}/exception", "exception type must be nonempty")
    if any(not item.strip() for item in record.diagnostics):
        _fail(f"{path}/diagnostics", "diagnostics must be nonempty strings")
    if record.status == "not_run":
        if not record.reason or record.reason != execution.stop_reason:
            _fail(path, "unstarted trial requires the first admission stop reason")
        if (
            not record.cleanup_confirmed
            or record.diagnostics
            or any(
                item is not None
                for item in (
                    record.artifact,
                    record.exception,
                    record.started_at_seconds,
                    record.completed_at_seconds,
                    record.elapsed_seconds,
                    record.returned_result_evidence,
                    record.admission_index,
                    record.admitted_offset_seconds,
                    record.finished_offset_seconds,
                    record.released_offset_seconds,
                )
            )
        ):
            _fail(path, "unstarted trial cannot contain execution or reservation evidence")
        return
    if any(
        item is None
        for item in (
            record.admission_index,
            record.admitted_offset_seconds,
            record.finished_offset_seconds,
            record.started_at_seconds,
            record.completed_at_seconds,
            record.elapsed_seconds,
        )
    ):
        _fail(path, "admitted trial requires observed timing and admission evidence")
    assert record.admitted_offset_seconds is not None
    assert record.finished_offset_seconds is not None
    assert record.elapsed_seconds is not None
    if (
        not 0
        <= record.admitted_offset_seconds
        <= record.finished_offset_seconds
        <= execution.elapsed_seconds
    ):
        _fail(path, "reservation offsets must be ordered within plan elapsed time")
    if record.elapsed_seconds < 0 or not math.isclose(
        record.elapsed_seconds,
        record.finished_offset_seconds - record.admitted_offset_seconds,
        rel_tol=0,
        abs_tol=1e-9,
    ):
        _fail(path, "trial elapsed time disagrees with its monotonic offsets")
    if record.cleanup_confirmed != (record.status != "cleanup_failed"):
        _fail(path, "cleanup confirmation disagrees with the trial status")
    if record.cleanup_confirmed:
        if record.released_offset_seconds != record.finished_offset_seconds:
            _fail(path, "confirmed cleanup must release its reservation at observed completion")
    elif record.released_offset_seconds is not None:
        _fail(path, "unconfirmed cleanup cannot release its reservation")
    if (
        execution.stopped_offset_seconds is not None
        and record.admitted_offset_seconds > execution.stopped_offset_seconds
    ):
        _fail(path, "trial was admitted after admission stopped")
    if record.status in _INTERRUPTED:
        if execution.termination_reason is None or record.reason != execution.termination_reason:
            _fail(path, "interrupted trial requires the first hard termination reason")
        if record.status == "timed_out" and record.reason != "timeout":
            _fail(path, "timed-out trial requires a deadline termination")
        if record.status == "cancelled" and record.reason == "timeout":
            _fail(path, "deadline interruption must retain a timed-out status")
        assert execution.termination_offset_seconds is not None
        if record.finished_offset_seconds < execution.termination_offset_seconds:
            _fail(path, "interrupted outcome precedes the hard termination")
        if record.artifact is not None or record.returned_result_evidence is not None:
            _fail(path, "interrupted trial cannot fabricate a final native result")
        if record.status == "cleanup_failed" and record.exception is None:
            _fail(path, "unconfirmed cleanup requires retained exception details")
    elif record.status == "exception":
        if record.reason is not None:
            _fail(path, "ordinary execution exceptions cannot claim a stop reason")
        if record.artifact is not None or record.exception is None:
            _fail(path, "exception requires retained details and no fabricated artifact")
        if record.returned_result_evidence is not None and not record.diagnostics:
            _fail(path, "partial returned evidence requires serialization diagnostics")
    elif (
        record.artifact is None
        or record.exception is not None
        or record.returned_result_evidence is not None
        or record.artifact.result.execution is None
        or record.artifact.result.execution.status != record.status
    ):
        _fail(path, "native outcome requires a matching canonical artifact")
    elif record.reason is not None:
        _fail(path, "native outcome cannot claim an interruption reason")


def _validate_stops(execution: ConcurrentPlanResult) -> None:
    for reason, offset, name in (
        (execution.stop_reason, execution.stopped_offset_seconds, "stopped_offset_seconds"),
        (
            execution.termination_reason,
            execution.termination_offset_seconds,
            "termination_offset_seconds",
        ),
    ):
        if (reason is None) != (offset is None):
            _fail(f"/execution/{name}", "stop reason and offset must be present together")
        if offset is not None and not 0 <= offset <= execution.elapsed_seconds:
            _fail(f"/execution/{name}", "stop offset must lie within plan elapsed time")
    if execution.termination_reason is not None:
        assert execution.termination_offset_seconds is not None
        if (
            execution.stop_reason is None
            or execution.stopped_offset_seconds is None
            or execution.termination_offset_seconds < execution.stopped_offset_seconds
        ):
            _fail("/execution/termination_reason", "hard termination cannot precede admission stop")
    if execution.stop_reason in _HARD and (
        execution.termination_reason != execution.stop_reason
        or execution.termination_offset_seconds != execution.stopped_offset_seconds
    ):
        _fail("/execution/termination_reason", "a first hard stop must also terminate execution")
    if (
        "timeout" in (execution.stop_reason, execution.termination_reason)
        and execution.timeout_seconds is None
    ):
        _fail("/execution/timeout_seconds", "deadline termination requires an explicit timeout")
    if execution.stop_reason == "elapsed_admission_limit":
        assert execution.stopped_offset_seconds is not None
        limit = execution.plan.budget.stop_after_elapsed_seconds
        if limit is None or execution.stopped_offset_seconds < limit:
            _fail(
                "/execution/stop_reason",
                "elapsed admission stop requires an elapsed explicit limit",
            )
    stopped_offset = execution.stopped_offset_seconds
    if execution.stop_reason == "stop_on_error" and (
        not execution.plan.policy.stop_on_error
        or stopped_offset is None
        or not any(
            record.status not in ("completed", "not_run")
            and record.finished_offset_seconds is not None
            and record.finished_offset_seconds <= stopped_offset
            for record in execution.trials
        )
    ):
        _fail(
            "/execution/stop_reason",
            "error stop requires policy and observed unsuccessful execution",
        )
    if execution.plan.policy.stop_on_error:
        first_failure = min(
            (
                record.finished_offset_seconds
                for record in execution.trials
                if record.status not in ("completed", "not_run")
                and record.finished_offset_seconds is not None
            ),
            default=None,
        )
        if first_failure is not None:
            if execution.stop_reason is None:
                _fail(
                    "/execution/stop_reason",
                    "unsuccessful execution must stop admission under stop_on_error",
                )
            if any(
                record.admitted_offset_seconds is not None
                and record.admitted_offset_seconds > first_failure
                for record in execution.trials
            ):
                _fail(
                    "/execution/trials",
                    "trial was admitted after an observed failure under stop_on_error",
                )
    if (
        "cleanup_failed" in (execution.stop_reason, execution.termination_reason)
        and execution.cleanup_confirmed
    ):
        _fail("/execution/termination_reason", "cleanup failure requires an unreleased owner")


def _validate_reservations(execution: ConcurrentPlanResult) -> None:
    admitted = sorted(
        (record for record in execution.trials if record.status != "not_run"),
        key=lambda record: record.admission_index or 0,
    )
    if [record.admission_index for record in admitted] != list(range(1, len(admitted) + 1)):
        _fail("/execution/trials", "admission indices must be unique and contiguous from one")
    if any(
        (left.admitted_offset_seconds or 0) > (right.admitted_offset_seconds or 0)
        for left, right in zip(admitted, admitted[1:], strict=False)
    ):
        _fail("/execution/trials", "admission offsets must follow admission index order")
    previous = {}
    for record in execution.trials:
        prior = previous.get(record.spec.cell_id)
        previous[record.spec.cell_id] = record
        if record.status == "not_run" or prior is None:
            continue
        assert record.admission_index is not None
        assert record.admitted_offset_seconds is not None
        if (
            prior.admission_index is None
            or prior.admission_index >= record.admission_index
            or prior.released_offset_seconds is None
            or prior.released_offset_seconds > record.admitted_offset_seconds
        ):
            _fail(
                "/execution/trials",
                "same-cell predecessors must finish and release before admission",
            )
    # Half-open intervals release before admission at equal timestamps. A
    # confirmed zero-length interval occupies no duration; an unreleased owner
    # remains occupied even at the snapshot's final timestamp.
    boundaries = []
    for record in admitted:
        if record.released_offset_seconds == record.admitted_offset_seconds:
            continue
        boundaries.append((record.admitted_offset_seconds, 1, record.admission_index, record))
        if record.released_offset_seconds is not None:
            boundaries.append((record.released_offset_seconds, 0, record.admission_index, record))
    active, rate, keys = set(), 0, set()
    for _, kind, index, record in sorted(boundaries, key=lambda item: item[:3]):
        if kind == 0:
            active.remove(index)
            rate -= record.target_bps
            keys.difference_update(record.resource_keys)
        else:
            if keys.intersection(record.resource_keys):
                _fail("/execution/trials", "resource reservations overlap")
            active.add(index)
            rate += record.target_bps
            keys.update(record.resource_keys)
            if (
                len(active) > execution.policy.max_workers
                or rate > execution.policy.max_active_target_bps
            ):
                _fail(
                    "/execution/trials",
                    "admission reservations exceed worker or target-rate limits",
                )


def _validate_history(execution: ConcurrentPlanResult) -> None:
    try:
        prepared, policy, extras, keys, targets = prepare_concurrent_plan(
            execution.plan, execution.policy, execution.resources
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        _fail("/execution", str(exc))
    if prepared != execution.plan or policy != execution.policy or extras != execution.resources:
        _fail("/execution", "stored admission inputs disagree with the prepared plan")
    if len(execution.trials) != len(prepared.trials):
        _fail("/execution/trials", "every declared trial must be retained")
    if execution.elapsed_seconds < 0:
        _fail("/execution/elapsed_seconds", "elapsed duration must be nonnegative")
    if execution.timeout_seconds is not None and execution.timeout_seconds <= 0:
        _fail("/execution/timeout_seconds", "timeout must be positive or null")
    _validate_stops(execution)
    for index, (record, spec) in enumerate(zip(execution.trials, prepared.trials, strict=True)):
        path = f"/execution/trials/{index}"
        if record.spec != spec:
            _fail(path, "trial settings or declared order disagree with the prepared plan")
        if record.resource_keys != keys[index] or record.target_bps != targets[index]:
            _fail(path, "resource keys or rate reservation disagree with admission inputs")
        _validate_outcome(record, execution, path)
    _validate_reservations(execution)


def snapshot_concurrent_execution(execution: ConcurrentPlanResult) -> ConcurrentPlanResult:
    """Validate and detach concurrent outcomes without native execution."""
    data = _model(ConcurrentPlanResult, execution, "/execution", encoding=True)
    snapshot = _model(ConcurrentPlanResult, data, "/execution", encoding=False)
    _validate_history(snapshot)
    return snapshot


def concurrent_report_from_execution(execution: ConcurrentPlanResult) -> ConcurrentPlanReport:
    """Snapshot bounded concurrency evidence with the report writer's identity."""
    return ConcurrentPlanReport(
        snapshot_concurrent_execution(execution),
        ArtifactProducer("iperf3-lib", version("iperf3-lib")),
    )


def concurrent_report_from_dict(mapping: dict[str, Any]) -> ConcurrentPlanReport:
    """Decode only explicit schema-three bounded-concurrency evidence."""
    data = _object(mapping, "")
    if type(data.get("schema_version")) is not int:
        _fail("/schema_version", "expected an explicit integer schema version")
    if data["schema_version"] != 3:
        raise UnsupportedReportVersionError(
            "/schema_version", "unsupported concurrent report schema"
        )
    try:
        _json_evidence(data)
    except (ValueError, TypeError, RecursionError) as exc:
        _fail("", f"invalid strict JSON evidence: {exc}")
    report = _model(ConcurrentPlanReport, data, "", encoding=False)
    if not report.producer.name.strip() or not report.producer.version.strip():
        _fail("/producer", "producer name and version must be nonempty")
    if any(_NAMESPACE.fullmatch(key) is None for key in report.extensions):
        _fail("/extensions", "extension keys must be namespaced")
    _validate_history(report.execution)
    return report


def concurrent_report_to_dict(report: ConcurrentPlanReport) -> dict[str, Any]:
    """Encode strict schema-three evidence with independently validated reservations."""
    data = _model(ConcurrentPlanReport, report, "", encoding=True)
    concurrent_report_from_dict(data)
    return data


def dumps_concurrent_report(report: ConcurrentPlanReport, *, indent: int | None = None) -> str:
    """Serialize deterministic strict JSON without dropping partial outcomes."""
    return json.dumps(
        concurrent_report_to_dict(report), sort_keys=True, indent=indent, allow_nan=False
    )


def loads_concurrent_report(text: str | bytes) -> ConcurrentPlanReport:
    """Reject duplicate fields and nonfinite constants before schema validation."""
    if not isinstance(text, (str, bytes)):
        _fail("", "expected JSON text or bytes")

    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                _fail("", f"duplicate JSON key: {key}")
            data[key] = value
        return data

    def invalid(value):
        _fail("", f"nonfinite JSON constant: {value}")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        _fail("", f"invalid JSON: {exc}")
    return concurrent_report_from_dict(data)


def render_concurrent_text(report: ConcurrentPlanReport) -> str:
    """Summarize execution and admission bounds without performance assessment."""
    concurrent_report_to_dict(report)
    execution = report.execution
    counts = {
        status: sum(record.status == status for record in execution.trials) for status in _STATUSES
    }
    return (
        f"Execution success={execution.execution_success}; cleanup_confirmed={execution.cleanup_confirmed}; stop_reason={execution.stop_reason}; termination_reason={execution.termination_reason}\n"
        f"Mode: bounded-concurrent-owned; pauses: zero-only; max_workers={execution.policy.max_workers}; max_active_target_bps={execution.policy.max_active_target_bps}\n"
        "Trials: " + ", ".join(f"{status}={count}" for status, count in counts.items()) + "\n"
    )


def render_concurrent_junit(report: ConcurrentPlanReport) -> str:
    """Render execution-only JUnit with a plan outcome for partial histories."""
    concurrent_report_to_dict(report)
    execution = report.execution
    root = ET.Element(
        "testsuite",
        name="iperf3-lib concurrent plan execution",
        tests=str(len(execution.trials) + 1),
    )
    for record in execution.trials:
        case = ET.SubElement(
            root,
            "testcase",
            name=_xml_text(record.spec.trial_id),
            classname=f"iperf3_lib.{record.spec.phase}",
            time=str(record.elapsed_seconds or 0),
        )
        if record.status == "not_run":
            ET.SubElement(case, "skipped", message=_xml_text(record.reason or "not_run"))
        elif record.status != "completed":
            message = (
                record.exception.message if record.exception else record.reason or record.status
            )
            ET.SubElement(case, "error", type=record.status, message=_xml_text(message))
        if record.artifact is not None:
            ET.SubElement(case, "system-out").text = _xml_text(dumps_artifact(record.artifact))
        elif record.partial_events:
            events = [
                _scalar_field(NativeEvent, event, "", encoding=True)
                for event in record.partial_events
            ]
            evidence: dict[str, Any] = {"partial_events": events}
            if record.returned_result_evidence is not None:
                evidence["returned_result_evidence"] = record.returned_result_evidence
            ET.SubElement(case, "system-out").text = _xml_text(json.dumps(evidence, sort_keys=True))
        elif record.returned_result_evidence is not None:
            ET.SubElement(case, "system-out").text = _xml_text(
                json.dumps(record.returned_result_evidence, sort_keys=True)
            )
    summary = ET.SubElement(
        root, "testcase", name="plan_execution", classname="iperf3_lib.execution"
    )
    if not execution.execution_success:
        ET.SubElement(
            summary,
            "error",
            type=execution.termination_reason or execution.stop_reason or "unsuccessful_execution",
            message="Concurrent plan execution did not complete successfully",
        )
    ET.SubElement(summary, "system-out").text = _xml_text(render_concurrent_text(report))
    root.set("errors", str(len(root.findall("testcase/error"))))
    root.set("skipped", str(len(root.findall("testcase/skipped"))))
    root.set("failures", "0")
    root.set("time", str(execution.elapsed_seconds))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
