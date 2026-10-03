"""Strict standalone v2 reports for owned sequential plans and global pauses."""

from __future__ import annotations

import json
import math
import re
import types
import xml.etree.ElementTree as ET
from dataclasses import fields
from importlib.metadata import version
from typing import Any, Never, Union, get_args, get_origin, get_type_hints

from ._ipc import IPCError, encode_frame
from .artifacts import ArtifactProducer, dumps_artifact
from .events import NativeEvent
from .plan_execution import PlanExecutionReport, PlanExecutionResult, PlanTrialRecord
from .reports import ReportValidationError, UnsupportedReportVersionError, _convert, _xml_text
from .trials import _json_evidence, prepare_plan

_MODELS = {PlanExecutionReport, PlanExecutionResult, PlanTrialRecord, ArtifactProducer, NativeEvent}
_INTERRUPTED = {"cancelled", "timed_out", "cleanup_failed"}
_NATIVE = {"completed", "failed", "incomplete"}
_STATUSES = (*sorted(_NATIVE), "exception", "cancelled", "timed_out", "cleanup_failed", "not_run")
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+")


def _fail(path: str, message: str) -> Never:
    raise ReportValidationError(path, message)


def _object(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(path, "expected an object with string keys")
    return value


def _field(spec, value, path, *, encoding):
    if spec in _MODELS:
        return _model(spec, value, path, encoding=encoding)
    args = get_args(spec)
    if get_origin(spec) in (types.UnionType, Union):
        # Python 3.12 represents Literal[...] | None as typing.Union, while
        # newer interpreters unify it with types.UnionType. Keep this v2
        # compatibility handling separate from the frozen v1 model codec.
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
    # Reuse the unchanged v1 codecs for admitted configurations, intent,
    # canonical artifacts and ordinary strict scalar/collection fields.
    return _convert(spec, value, path, encoding=encoding)


def _model(model, value, path, *, encoding):
    declared = fields(model)
    hints = get_type_hints(model)
    extra = {"execution_success", "cleanup_confirmed"} if model is PlanExecutionResult else set()
    if encoding:
        if not isinstance(value, model):
            _fail(path, f"expected {model.__name__}")
        data = {item.name: getattr(value, item.name) for item in declared}
    else:
        data = _object(value, path)
        if set(data) != {item.name for item in declared} | extra:
            _fail(path, "object must contain exactly the declared plan-v2 fields")
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
    instance = model(**values)
    for name in extra:
        stored = _convert(bool, data[name], f"{path}/{name}")
        if stored != getattr(instance, name):
            _fail(f"{path}/{name}", "derived outcome disagrees with retained trials")
    return instance


def _events(record: PlanTrialRecord, path: str) -> None:
    if record.events_observed < 0 or record.events_dropped < 0:
        _fail(path, "event delivery counts must be nonnegative")
    if record.events_observed != len(record.partial_events) + record.events_dropped:
        _fail(path, "observed events must equal retained events plus retention drops")
    if record.status not in _INTERRUPTED:
        if record.partial_events or record.events_observed or record.events_dropped:
            _fail(path, "only interrupted trials retain partial event evidence")
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
            # Include the four-byte framing header in both retention limits,
            # matching the scheduler's accounting of the copied prefix.
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


def _validate_history(execution: PlanExecutionResult) -> None:
    plan = execution.plan
    try:
        prepared = prepare_plan(
            plan.trials, policy=plan.policy, budget=plan.budget, order_seed=plan.order_seed
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        _fail("/execution/plan", str(exc))
    if prepared != plan:
        _fail("/execution/plan", "admission estimates or settings disagree with the finite plan")
    if len(execution.trials) != len(plan.trials):
        _fail("/execution/trials", "every declared trial must be retained")
    if execution.elapsed_seconds < 0 or execution.observed_pause_seconds < 0:
        _fail("/execution", "elapsed and pause durations must be nonnegative")
    if execution.timeout_seconds is not None and execution.timeout_seconds <= 0:
        _fail("/execution/timeout_seconds", "timeout must be positive or null")
    stopped = failed = interrupted = False
    for index, (record, spec) in enumerate(zip(execution.trials, plan.trials, strict=True)):
        path = f"/execution/trials/{index}"
        if record.spec != spec:
            _fail(path, "trial settings/order disagree with the admitted plan")
        _events(record, path)
        if record.exception is not None and not record.exception.type_name.strip():
            _fail(f"{path}/exception", "exception type must be nonempty")
        if any(not item.strip() for item in record.diagnostics):
            _fail(f"{path}/diagnostics", "diagnostics must be nonempty strings")
        if record.status == "not_run":
            stopped = True
            if not record.reason or record.reason != execution.stop_reason:
                _fail(path, "unstarted trial requires the recorded plan stop reason")
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
                    )
                )
            ):
                _fail(path, "unstarted trial cannot contain execution evidence or pending cleanup")
            continue
        if stopped or interrupted:
            _fail(path, "executed trial follows a stopped or interrupted trial")
        if failed and plan.policy.stop_on_error:
            _fail(path, "executed trial follows unsuccessful execution under stop_on_error")
        if (
            record.elapsed_seconds is None
            or any(
                item is None
                for item in (
                    record.started_at_seconds,
                    record.completed_at_seconds,
                )
            )
            or record.elapsed_seconds < 0
        ):
            _fail(path, "executed trial requires observed timing and nonnegative elapsed duration")
        if record.cleanup_confirmed != (record.status != "cleanup_failed"):
            _fail(path, "cleanup confirmation disagrees with the trial status")
        failed |= record.status != "completed"
        if record.status in _INTERRUPTED:
            interrupted = True
            expected = {
                "cancelled": {"cancelled"},
                "timed_out": {"timeout"},
                "cleanup_failed": {"cleanup_failed", "cancelled", "timeout"},
            }[record.status]
            if execution.stop_reason not in expected or record.reason != execution.stop_reason:
                _fail(path, "interrupted trial requires a matching plan stop reason")
            if record.artifact is not None or record.returned_result_evidence is not None:
                _fail(path, "interrupted trial cannot fabricate a final native result")
            if record.status == "cleanup_failed" and record.exception is None:
                _fail(path, "unconfirmed cleanup requires retained exception details")
        elif record.status == "exception":
            if record.artifact is not None or record.exception is None:
                _fail(path, "exception requires retained error details and no fabricated artifact")
            if record.returned_result_evidence is not None and not record.diagnostics:
                _fail(path, "partial returned evidence requires serialization diagnostics")
        else:
            if (
                record.artifact is None
                or record.exception is not None
                or record.returned_result_evidence is not None
                or record.artifact.result.execution is None
                or record.artifact.result.execution.status != record.status
            ):
                _fail(path, "native outcome requires a matching canonical artifact")
    try:
        duration = math.fsum(record.elapsed_seconds or 0 for record in execution.trials)
        duration += execution.observed_pause_seconds
    except OverflowError:
        _fail("/execution", "recorded sequential duration is not finite")
    if not math.isfinite(duration) or duration > execution.elapsed_seconds + 1e-9:
        _fail(
            "/execution/elapsed_seconds", "recorded work and pauses exceed sequential elapsed time"
        )
    if (
        execution.stop_reason == "elapsed_admission_limit"
        and plan.budget.stop_after_elapsed_seconds is None
    ):
        _fail("/execution/stop_reason", "elapsed admission stop requires an explicit limit")
    if execution.stop_reason == "timeout" and execution.timeout_seconds is None:
        _fail("/execution/stop_reason", "deadline interruption requires an explicit timeout")
    if execution.stop_reason == "stop_on_error" and (not plan.policy.stop_on_error or not failed):
        _fail("/execution/stop_reason", "error stop requires policy and unsuccessful execution")
    if execution.stop_reason == "cleanup_failed" and execution.cleanup_confirmed:
        _fail("/execution/stop_reason", "cleanup failure requires unconfirmed cleanup evidence")


def snapshot_plan_execution(execution: PlanExecutionResult) -> PlanExecutionResult:
    """Validate and detach a sequential history without loading native code."""
    data = _model(PlanExecutionResult, execution, "/execution", encoding=True)
    snapshot = _model(PlanExecutionResult, data, "/execution", encoding=False)
    _validate_history(snapshot)
    return snapshot


def plan_report_from_execution(execution: PlanExecutionResult) -> PlanExecutionReport:
    """Snapshot owned plan evidence with the current report writer's identity."""
    return PlanExecutionReport(
        snapshot_plan_execution(execution), ArtifactProducer("iperf3-lib", version("iperf3-lib"))
    )


def plan_report_from_dict(mapping: dict[str, Any]) -> PlanExecutionReport:
    """Read explicit v2 execution evidence without assessment or native execution."""
    data = _object(mapping, "")
    if type(data.get("schema_version")) is not int:
        _fail("/schema_version", "expected an explicit integer schema version")
    if data["schema_version"] != 2:
        raise UnsupportedReportVersionError("/schema_version", "unsupported plan report schema")
    try:
        _json_evidence(data)
    except (ValueError, TypeError, RecursionError) as exc:
        _fail("", f"invalid strict JSON evidence: {exc}")
    report = _model(PlanExecutionReport, data, "", encoding=False)
    if not report.producer.name.strip() or not report.producer.version.strip():
        _fail("/producer", "producer name and version must be nonempty")
    if any(_NAMESPACE.fullmatch(key) is None for key in report.extensions):
        _fail("/extensions", "extension keys must be namespaced")
    _validate_history(report.execution)
    return report


def plan_report_to_dict(report: PlanExecutionReport) -> dict[str, Any]:
    """Encode strict v2 evidence; global pauses retain their sequential meaning."""
    data = _model(PlanExecutionReport, report, "", encoding=True)
    plan_report_from_dict(data)
    return data


def dumps_plan_report(report: PlanExecutionReport, *, indent: int | None = None) -> str:
    """Serialize a deterministic standalone report without dropping partial outcomes."""
    return json.dumps(plan_report_to_dict(report), sort_keys=True, indent=indent, allow_nan=False)


def loads_plan_report(text: str | bytes) -> PlanExecutionReport:
    """Reject malformed JSON, duplicate fields and nonfinite numbers before reading v2."""
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
    return plan_report_from_dict(data)


def render_plan_text(report: PlanExecutionReport) -> str:
    """Render execution and cleanup outcomes without making a performance assessment."""
    plan_report_to_dict(report)
    execution = report.execution
    counts = {
        status: sum(record.status == status for record in execution.trials) for status in _STATUSES
    }
    return (
        f"Execution success={execution.execution_success}; cleanup_confirmed={execution.cleanup_confirmed}; stop_reason={execution.stop_reason}\n"
        "Mode: sequential-owned; pauses: global completion to next start\n"
        "Trials: " + ", ".join(f"{status}={count}" for status, count in counts.items()) + "\n"
    )


def render_plan_junit(report: PlanExecutionReport) -> str:
    """Render execution-only JUnit; interrupted plans always retain an error outcome."""
    plan_report_to_dict(report)
    execution = report.execution
    root = ET.Element(
        "testsuite", name="iperf3-lib plan execution", tests=str(len(execution.trials) + 1)
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
                _model(NativeEvent, event, "", encoding=True) for event in record.partial_events
            ]
            ET.SubElement(case, "system-out").text = _xml_text(
                json.dumps({"partial_events": events}, sort_keys=True)
            )
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
            type=execution.stop_reason or "unsuccessful_execution",
            message="Plan execution did not complete successfully",
        )
    ET.SubElement(summary, "system-out").text = _xml_text(render_plan_text(report))
    root.set("errors", str(len(root.findall("testcase/error"))))
    root.set("skipped", str(len(root.findall("testcase/skipped"))))
    root.set("failures", "0")
    root.set("time", str(execution.elapsed_seconds))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
