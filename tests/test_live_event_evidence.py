"""Adversarial native-receipt checks for typed progress and final-result identity."""

import asyncio
import copy
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

import pytest

from iperf3_lib._live_event_types import LiveProjector, make_terminal_event
from iperf3_lib.artifacts import artifact_from_result, dumps_artifact
from iperf3_lib.events import LiveEvent, WorkerStatePayload
from iperf3_lib.result import result_from_iperf_json
from scripts import qualify_lifecycle as qualification

PRODUCER = {"package_version": "0.3.0", "python_version": "3.14.2", "native_version": "iperf 3.21"}


def live_receipt(case, *, native_version="iperf 3.21"):
    """Detach a synthetic JSON receipt before a corruption test."""
    return json.loads(_live_receipt_json(case, native_version=native_version))


@lru_cache
def _live_receipt_json(case, *, native_version="iperf 3.21"):
    """Build coherent synthetic ownership around retained native shape fixtures."""
    producer = {**PRODUCER, "native_version": native_version}
    version = native_version.removeprefix("iperf ")
    mode = (
        "native-error"
        if case == "native-error"
        else "close"
        if case.startswith("close-")
        else "matrix"
    )
    protocol, direction = case.split("-", 1) if mode == "matrix" else ("tcp", "forward")
    native_direction = "bidirectional" if direction == "bidir" else direction

    def make(role, index, *, reuse=False):
        fixture = (
            Path(__file__).parent
            / "fixtures/native"
            / version
            / (
                f"{'tcp' if reuse else protocol}-{'forward' if reuse else native_direction}-{role}.json"
            )
        )
        if not fixture.exists() and protocol == "udp" and native_direction == "forward":
            fixture = fixture.with_name(f"udp-{role}.json")
        raw = json.loads(fixture.read_text())
        if not reuse and mode == "native-error":
            raw = {"error": "Connection refused", "end": {}}
        elif not reuse and mode == "close":
            raw["error"] = "The peer ended the active test"
        result = result_from_iperf_json(raw, reporting_role=role)
        worker = {
            "protocol_version": 1,
            "request_id": f"{index:032x}",
            "worker_id": f"{index + 100:032x}",
            "run_index": 1,
            "pid": 1000 + index,
            "producer": {**producer, "library_selector": {"source": "synthetic"}},
        }
        result.extensions["iperf3_lib.worker"] = worker
        capture = {
            "schema_version": 1,
            "callbacks": len(raw.get("intervals", [])) + 2 + (version == "3.21"),
            "copied": len(raw.get("intervals", [])) + 2 + (version == "3.21"),
            "capture_dropped": 0,
            "malformed": 0,
            "retention_dropped": 0,
            "complete_document": version == "3.21",
            "complete_document_source": "callback" if version == "3.21" else None,
            "getter_error": None,
            "diagnostics": [],
            "reconstruction_complete": True,
        }
        result.extensions["iperf3_lib.event_capture"] = capture
        capture["limits"] = {
            "payload_bytes": 8 * 1024 * 1024,
            "pending_bytes": 16 * 1024 * 1024,
            "pending_items": 256,
            "retained_bytes": 12 * 1024 * 1024,
            "diagnostics": 8,
        }
        if version == "3.19.1":
            result.extensions["iperf3_lib.native_json"] = {
                "representation": "reconstructed_events",
                "events": [
                    {"event": "start", "data": raw.get("start", {})},
                    *({"event": "interval", "data": value} for value in raw.get("intervals", [])),
                    {"event": "end", "data": raw.get("end", {})},
                ],
            }
        identity = {key: worker[key] for key in ("request_id", "worker_id", "run_index")}
        events = [
            LiveEvent(
                "worker_state",
                WorkerStatePayload("starting", role),
                **identity,
                delivery_sequence=1,
            ),
            LiveEvent(
                "worker_state",
                WorkerStatePayload("ready", role),
                **identity,
                delivery_sequence=2,
            ),
        ]
        projector = LiveProjector(role)
        payloads = [("native_start", raw["start"])] if "start" in raw else []
        payloads += [("interval", value) for value in raw.get("intervals", [])]
        payloads += [("native_error", raw["error"])] if "error" in raw else []
        payloads += [("native_end", raw.get("end", {}))]
        if version == "3.21":
            payloads += [("native_document", raw)]
        cancelled = not reuse and case == f"close-{role}"
        if cancelled:
            payloads = [
                item
                for item in payloads
                if item[0] not in {"native_error", "native_end", "native_document"}
            ]
        for sequence, (kind, data) in enumerate(payloads, 1):
            events.append(
                projector.project(
                    {
                        "kind": kind,
                        "data": data,
                        **identity,
                        "capture_sequence": sequence,
                        "arrival_offset_seconds": sequence / 10,
                        "time": 1700000000 + sequence / 10,
                    },
                    delivery_sequence=len(events) + 1,
                )
            )
        result.extensions["iperf3_lib.live_delivery"] = {
            "emitted": len(payloads),
            "worker_dropped": 0,
            "parent_dropped": 0,
            "projection_dropped": 0,
            "projection_malformed": 0,
            "queue_capacity": 256,
            "queue_bytes": 8 * 1024 * 1024,
            "event_bytes": 1024 * 1024,
        }
        capture.update(
            callbacks=len(payloads),
            copied=len(payloads),
            live_emitted=len(payloads),
            live_dropped=0,
        )
        events.append(
            make_terminal_event(
                None if cancelled else result,
                asyncio.CancelledError() if cancelled else None,
                delivery_sequence=len(events) + 1,
                identity=events[0],
            )
        )
        artifact = None if cancelled else dumps_artifact(artifact_from_result(result))
        return {
            "events": [asdict(event) for event in events],
            "artifact_json": artifact,
            "error_type": "CancelledError" if cancelled else None,
        }

    def cleanup(indices):
        return {
            "workers": [
                {
                    "pid": 1000 + index,
                    "returncode": 0,
                    "stdin_closed": True,
                    "stdout_closed": True,
                    "ready": {
                        "protocol_version": 1,
                        "request_id": f"{index:032x}",
                        "worker_id": f"{index + 100:032x}",
                        "run_index": 1,
                        "pid": 1000 + index,
                        "producer": {**producer, "library_selector": {"source": "synthetic"}},
                    },
                }
                for index in indices
            ],
            "listener_released": True,
            "server_lock_released": True,
        }

    roles = ("client",) if mode == "native-error" else ("client", "server")
    receipt = {
        "schema_version": 1,
        "case": case,
        "mode": mode,
        "protocol": protocol,
        "direction": direction,
        "port": 5201,
        "producer": producer,
        "support": {
            "status": "supported",
            "family": 2,
            "type": 1,
            "protocol": 132,
            "errno": None,
            "error": None,
            "kernel_release": "synthetic",
        }
        if protocol == "sctp"
        else None,
        "endpoints": {role: make(role, index) for index, role in enumerate(roles, 1)},
        "cleanup": cleanup(range(1, len(roles) + 1)),
        "reuse": {
            "client_artifact_json": make("client", 3, reuse=True)["artifact_json"],
            "server_artifact_json": make("server", 4, reuse=True)["artifact_json"],
            "cleanup": cleanup((3, 4)),
        },
    }
    return json.dumps(receipt)


def check(receipt):
    """Validate exactly one selected native receipt with its observed producer."""
    producer = receipt["producer"]
    qualification._validate_live_event_receipt(
        receipt,
        {
            "package_version": producer["package_version"],
            "python": producer["python_version"],
            "native_version": producer["native_version"],
        },
        f"test_live_events_integration.py::test_live_events_roundtrip[{receipt['case']}]",
    )


@pytest.mark.parametrize("case", qualification.LIVE_EVENT_CASES)
@pytest.mark.parametrize("version", ["iperf 3.19.1", "iperf 3.21"])
def test_native_event_receipts_cover_every_mode_role_and_representation(case, version):
    """Both native representations must preserve actual shape and ownership evidence."""
    check(live_receipt(case, native_version=version))


@pytest.mark.parametrize(
    "fault",
    [
        "missing-endpoint",
        "wrong-producer",
        "unreaped",
        "open-pipe",
        "listener",
        "worker-identity",
        "native-time",
        "capture-order",
        "terminal-order",
        "false-native-status",
        "capture-loss",
        "measurement",
        "native-payload",
        "missing-document",
        "reuse-identity",
        "protocol",
        "direction",
        "sctp-permission",
    ],
)
def test_native_event_receipts_reject_contradictory_evidence(fault):
    """Presence of a successful pytest phase cannot conceal invalid native evidence."""
    receipt = live_receipt("sctp-forward" if fault == "sctp-permission" else "tcp-forward")
    endpoint = receipt["endpoints"]["client"]
    events = endpoint["events"]
    interval = next(event for event in events if event["kind"] == "interval")
    terminal = events[-1]
    if fault == "missing-endpoint":
        receipt["endpoints"].pop("server")
    elif fault == "wrong-producer":
        receipt["producer"]["python_version"] = "wrong"
    elif fault == "unreaped":
        receipt["cleanup"]["workers"][0]["returncode"] = None
    elif fault == "open-pipe":
        receipt["cleanup"]["workers"][0]["stdout_closed"] = False
    elif fault == "listener":
        receipt["cleanup"]["listener_released"] = False
    elif fault == "worker-identity":
        interval["worker_id"] = "0" * 32
    elif fault == "native-time":
        interval["arrival_offset_seconds"] = float("nan")
    elif fault == "capture-order":
        interval["capture_sequence"] = 1
    elif fault == "terminal-order":
        events.insert(1, events.pop())
    elif fault == "false-native-status":
        terminal["payload"]["native_status"] = "failed"
    elif fault == "capture-loss":
        artifact = json.loads(endpoint["artifact_json"])
        metadata = artifact["result"]["extensions"]["iperf3_lib.event_capture"]
        metadata["callbacks"] += 1
        metadata["capture_dropped"] = 1
        terminal["payload"]["capture"] = copy.deepcopy(metadata)
        endpoint["artifact_json"] = json.dumps(artifact)
    elif fault == "measurement":
        interval["payload"]["measurements"][0]["bytes"] += 1
    elif fault == "native-payload":
        interval["payload"]["raw"]["sum"]["bytes"] += 1
    elif fault == "missing-document":
        events[:] = [event for event in events if event["kind"] != "native_document"]
        for index, event in enumerate(events, 1):
            event["delivery_sequence"] = index
    elif fault == "reuse-identity":
        receipt["reuse"]["client_artifact_json"] = endpoint["artifact_json"]
    elif fault == "protocol":
        receipt["protocol"] = "udp"
    elif fault == "direction":
        interval["payload"]["measurements"][0]["direction"] = "server_to_client"
    else:
        receipt["support"].update(status="kernel_unsupported", errno=1)
    with pytest.raises(ValueError, match="live-event native receipt"):
        check(receipt)


def test_closed_stream_cannot_claim_a_result_or_unmeasured_cancellation():
    """A cancellation must retain observed traffic and must not invent a native summary."""
    receipt = live_receipt("close-client")
    receipt["endpoints"]["client"]["events"][-1]["payload"]["result_available"] = True
    with pytest.raises(ValueError, match="fabricated"):
        check(receipt)


@pytest.mark.parametrize("case", ["tcp-reverse", "native-error", "close-client", "close-server"])
@pytest.mark.parametrize(
    "fault",
    [
        "invented-kind",
        "wrong-role",
        "reversed-states",
        "missing-ready",
        "late-state",
        "state-native-sequence",
    ],
)
def test_native_event_receipts_require_admitted_wrapper_states(case, fault):
    """Every qualified operation reports starting then ready without fabricated native origin."""
    receipt = live_receipt(case)
    role = "server" if case == "close-server" else "client"
    events = receipt["endpoints"][role]["events"]
    if fault == "invented-kind":
        events[0]["kind"] = "fabricated_kind"
    elif fault == "wrong-role":
        events[0]["payload"]["role"] = "server" if role == "client" else "client"
    elif fault == "reversed-states":
        events[0]["payload"]["state"], events[1]["payload"]["state"] = "ready", "starting"
    elif fault == "missing-ready":
        events.pop(1)
    elif fault == "late-state":
        events.insert(3, events.pop(1))
    else:
        events[0].update(
            capture_sequence=1, arrival_offset_seconds=0, received_at_seconds=1700000000
        )
    for index, event in enumerate(events, 1):
        event["delivery_sequence"] = index
    with pytest.raises(ValueError, match="live-event native receipt"):
        check(receipt)


@pytest.mark.parametrize("case", ["tcp-reverse", "udp-bidir", "close-client", "close-server"])
@pytest.mark.parametrize(
    "fault", ["raw-protocol", "typed-protocol", "method", "role", "no-capture", "repeated-start"]
)
def test_native_start_matches_final_or_cancelled_operation_provenance(case, fault):
    """Typed start evidence cannot contradict final measurements or an admitted cancelled run."""
    receipt = live_receipt(case)
    role = "server" if case == "close-server" else "client"
    events = receipt["endpoints"][role]["events"]
    event = next(item for item in events if item["kind"] == "native_start")
    payload = event["payload"]
    if fault == "raw-protocol":
        payload["raw"]["test_start"]["protocol"] = "SCTP"
    elif fault == "typed-protocol":
        payload["protocol"] = "sctp"
    elif fault == "method":
        payload["method"] = "unknown"
    elif fault == "role":
        payload["reporting_role"] = "server" if role == "client" else "client"
    elif fault == "no-capture":
        event.update(capture_sequence=None, arrival_offset_seconds=None, received_at_seconds=None)
    else:
        events.insert(3, copy.deepcopy(event))
    for index, item in enumerate(events, 1):
        item["delivery_sequence"] = index
    with pytest.raises(ValueError, match="live-event native receipt"):
        check(receipt)


@pytest.mark.parametrize("case", ["close-client", "close-server"])
def test_cancelled_start_cannot_relabel_consistent_native_data_as_a_different_configuration(case):
    """Agreement between forged raw and typed start cannot replace the configured scenario."""
    receipt = live_receipt(case)
    role = case.removeprefix("close-")
    payload = next(
        item["payload"]
        for item in receipt["endpoints"][role]["events"]
        if item["kind"] == "native_start"
    )
    payload.update(protocol="udp", method="reverse")
    payload["raw"]["test_start"].update(protocol="UDP", reverse=1)
    with pytest.raises(ValueError, match="configured native provenance"):
        check(receipt)
