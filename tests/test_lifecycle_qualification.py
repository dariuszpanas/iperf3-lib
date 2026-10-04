"""Fail-closed identity and evidence checks for installed lifecycle qualification."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import qualify_lifecycle as qualification

REVISION = "a" * 40


@pytest.fixture
def sealed_pair(tmp_path):
    """Build a tiny real wheel/sdist pair and seal the source and harness bytes."""
    source, distributions = tmp_path / "source", tmp_path / "dist"
    source.mkdir()
    distributions.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    package = source / "src/iperf3_lib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""A fixture package."""\n')
    (package / "py.typed").write_bytes(b"")
    for name in qualification.HARNESS:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n")
    with zipfile.ZipFile(distributions / "iperf3_lib-1.2.3-py3-none-any.whl", "w") as archive:
        for path in package.iterdir():
            archive.write(path, f"iperf3_lib/{path.name}")
    with tarfile.open(distributions / "iperf3_lib-1.2.3.tar.gz", "w:gz") as archive:
        for path in package.iterdir():
            archive.add(path, f"iperf3_lib-1.2.3/src/iperf3_lib/{path.name}")
    manifest = qualification.write_manifest(source, distributions, REVISION)
    return source, distributions, manifest


def test_lifecycle_manifest_requires_source_and_both_retained_distributions(sealed_pair):
    """The same package bytes bind source, wheel, sdist and copied harness."""
    source, distributions, manifest = sealed_pair
    assert qualification.verify_manifest(source, distributions, REVISION) == manifest
    assert {entry["kind"] for entry in manifest["distributions"]} == {"wheel", "sdist"}
    assert set(manifest["harness_sha256"]) == set(qualification.HARNESS)
    assert set(manifest["package_sha256"]) == {"__init__.py", "py.typed"}


@pytest.mark.parametrize("change", ["revision", "harness", "source", "archive", "missing-format"])
def test_lifecycle_manifest_rejects_stale_or_substituted_inputs(sealed_pair, change):
    """A green receipt cannot be attached to another checkout, harness or distribution."""
    source, distributions, manifest = sealed_pair
    revision = REVISION
    if change == "revision":
        revision = "b" * 40
    elif change == "harness":
        (source / qualification.HARNESS[0]).write_text("changed harness\n")
    elif change == "source":
        (source / "src/iperf3_lib/__init__.py").write_text("changed package\n")
    elif change == "archive":
        (distributions / manifest["distributions"][0]["filename"]).write_bytes(b"substituted")
    else:
        manifest["distributions"].pop()
        (distributions / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        qualification.verify_manifest(source, distributions, revision)


def test_lifecycle_manifest_rejects_resealed_wrong_package_bytes(sealed_pair):
    """Even an updated archive hash cannot disguise bytes that differ from the source."""
    source, distributions, manifest = sealed_pair
    entry = manifest["distributions"][0]
    path = distributions / entry["filename"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("iperf3_lib/__init__.py", "different contents\n")
        archive.writestr("iperf3_lib/py.typed", "")
    entry.update(sha256=qualification.digest(path), size=path.stat().st_size)
    (distributions / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="package bytes"):
        qualification.verify_manifest(source, distributions, REVISION)


def test_lifecycle_archives_reject_duplicate_or_linked_package_members(tmp_path):
    """Ambiguous archive entries cannot establish installed package provenance."""
    archive_path = tmp_path / "linked.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("package/src/iperf3_lib/__init__.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/unrelated/source.py"
        archive.addfile(member)
    with pytest.raises(ValueError, match="invalid or duplicate"):
        qualification.archive_hashes(archive_path, "sdist")
    with tarfile.open(archive_path, "w:gz") as archive:
        for _ in range(2):
            member = tarfile.TarInfo("package/src/iperf3_lib/__init__.py")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="invalid or duplicate"):
        qualification.archive_hashes(archive_path, "sdist")


@pytest.mark.parametrize("failure", ["source-import", "editable", "wrong-version", "wrong-bytes"])
def test_installed_identity_rejects_source_imports_and_altered_installations(
    sealed_pair, tmp_path, monkeypatch, failure
):
    """Runtime location and bytes must match the actual installed distribution."""
    import iperf3_lib

    source, _, manifest = sealed_pair
    prefix = tmp_path / "venv"
    package = prefix / "lib/site-packages/iperf3_lib"
    package.mkdir(parents=True)
    for name in manifest["package_sha256"]:
        (package / name).write_bytes((source / "src/iperf3_lib" / name).read_bytes())
    location = (
        source / "src/iperf3_lib/__init__.py"
        if failure == "source-import"
        else package / "__init__.py"
    )
    if failure == "wrong-bytes":
        location.write_text("changed installed package")
    monkeypatch.setattr(iperf3_lib, "__file__", str(location))
    metadata = SimpleNamespace(
        version="0.0.0" if failure == "wrong-version" else manifest["package_version"],
        read_text=lambda _: json.dumps({"dir_info": {"editable": failure == "editable"}}),
    )
    monkeypatch.setattr(qualification.importlib.metadata, "distribution", lambda _: metadata)
    with pytest.raises(ValueError):
        qualification.require_installed_package(manifest, prefix, source)


def _passed_results():
    reports = []
    for nodeid in qualification.expected_tests():
        key = qualification.TESTS[nodeid.split("::")[0]][1]
        active = "[active-" in nodeid
        evidence = (
            {
                "reused": True,
                "cancelled_children": 2 if active else 1,
                "measured_bytes_before_cancel": 12 if active else 0,
            }
            if key == "cancellation"
            else {
                "parent_pid": 100,
                "worker_pid": 101,
                "worker_signal": 9,
                "parent_returncode": -9,
                "worker_reaped": True,
                "traffic_bytes": 12 if active else 0,
                "listener_released": True,
                "reused": True,
                "reuse_bytes": 12,
            }
        )
        if key == "worker_ipc":
            evidence = {
                "case": nodeid.rsplit("[", 1)[1].removesuffix("]"),
                "workers_reaped": True,
                "pipes_closed": True,
                "dropped": 1,
                "cancelled": True,
                "rejected": True,
            }
        if key == "async_plan":
            evidence = _async_plan_receipt(nodeid.rsplit("[", 1)[1].removesuffix("]"))
        if key == "concurrent_plan":
            evidence = _concurrent_plan_receipt(nodeid.rsplit("[", 1)[1].removesuffix("]"))
        if key == "resource_stress":
            evidence = _resource_stress_receipt(nodeid.rsplit("[", 1)[1].removesuffix("]"))
        if key == "adaptive_udp":
            evidence = _adaptive_receipt(nodeid.rsplit("[", 1)[1].removesuffix("]"))
        if key == "live_events":
            from test_live_event_evidence import live_receipt

            evidence = live_receipt(nodeid.rsplit("[", 1)[1].removesuffix("]"))
        if key == "cancellation":
            evidence["reuse_bytes"] = 12
            for index, name in enumerate(("reused_client_worker", "reused_server_worker")):
                evidence[name] = {
                    "protocol_version": 1,
                    "run_index": 1,
                    "pid": 200 + index,
                    "request_id": str(index) * 32,
                    "worker_id": str(index + 2) * 32,
                    "producer": {
                        "package_version": "0.3.0",
                        "python_version": "3.14.2",
                        "native_version": "iperf 3.21",
                        "library_selector": {"source": "platform_search", "candidates": []},
                    },
                }
        reports.extend(
            {
                "nodeid": nodeid,
                "phase": phase,
                "outcome": "passed",
                "properties": {key: json.dumps(evidence)},
            }
            for phase in ("setup", "call", "teardown")
        )
    return {
        "exit_code": 0,
        "collected": qualification.expected_tests(),
        "reports": reports,
        "python": "3.14.2",
        "native_version": "iperf 3.21",
        "package_version": "0.3.0",
    }


def _adaptive_receipt(case):
    """Detach synthetic adaptive receipts before each corruption test."""
    return copy.deepcopy(_adaptive_receipt_cached(case))


@lru_cache
def _adaptive_receipt_cached(case):
    """Generate explicitly synthetic UDP observations through the public planner."""
    from sweep_helpers import measured

    from iperf3_lib.adaptive import AdaptiveUDPPolicy, prepare_adaptive_udp
    from iperf3_lib.adaptive_execution import run_adaptive_udp
    from iperf3_lib.adaptive_reports import dumps_adaptive_udp_report, report_from_adaptive_udp
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.result import result_from_iperf_json
    from iperf3_lib.trials import PlanBudget, TrialPolicy

    impaired = case == "impaired-forward"

    def execute(spec):
        result = measured(spec)
        raw = copy.deepcopy(result.raw)
        target = spec.resolved_config.rate * 2
        count = target // 8
        seconds = count * 8 / target
        loss = 50 if impaired and target > 1_000_000 else 0
        raw["end"]["sum_sent"].update(bytes=count, seconds=seconds, bits_per_second=target)
        raw["end"]["sum_received"].update(
            bytes=count * (100 - loss) // 100,
            seconds=seconds,
            lost_packets=loss * 10,
            packets=1000,
            lost_percent=loss,
        )
        observed = result_from_iperf_json(raw)
        observed.extensions = result.extensions
        return observed

    prepared = prepare_adaptive_udp(
        ClientConfig(
            "127.0.0.1",
            protocol="udp",
            duration=1,
            parallel=2,
            blksize=1200,
            reverse=case == "clean-reverse",
        ),
        (250_001, 2_000_001),
        policy=TrialPolicy(repetitions=2, warmup_runs=1, pause_seconds=0.1, max_trials=18),
        budget=PlanBudget(18, 8_000_000),
        adaptive_policy=AdaptiveUDPPolicy(
            min_rate_bps=250_001,
            max_rate_bps=2_000_001,
            max_distinct_rates=3,
            max_refinement_depth=1,
            receiver_loss_percent=5,
            minimum_valid_trials=2,
            minimum_sender_fraction=0.9,
        ),
    )
    result = run_adaptive_udp(prepared, executor=execute)
    persisted = dumps_adaptive_udp_report(report_from_adaptive_udp(result))
    measurements = []
    for batch in result.batches:
        for record in batch.execution.trials:
            raw = record.artifact.result.raw
            measurements.append(
                {
                    "trial_id": record.spec.trial_id,
                    "native_start": raw["start"]["test_start"],
                    "sender": raw["end"]["sum_sent"],
                    "receiver": raw["end"]["sum_received"],
                }
            )
    reuse = copy.deepcopy(result.batches[0].execution.trials[0].artifact.result.raw)
    reuse["start"]["test_start"].update(target_bitrate=250_000, num_streams=1, reverse=0)
    impairment = None
    if impaired:
        root = {"kind": "prio", "handle": "34:"}
        netem = {
            "kind": "netem",
            "handle": "343:",
            "bytes": 12,
            "packets": 8,
            "drops": 4,
            "options": {"limit": 5, "rate": {"rate": 125_000}},
        }
        impairment = {
            "device": "lo",
            "rate_bps": 1_000_000,
            "limit_packets": 5,
            "protocol": "udp",
            "destination_port": 5201,
            "before": [{"kind": "noqueue", "handle": "0:"}],
            "configured": [root, netem],
            "after_traffic": [root, netem],
            "after_cleanup": [{"kind": "noqueue", "handle": "0:"}],
            "filters": [{"kind": "u32", "protocol": "ip"}],
            "filters_text": "match 00110000/00ff0000 at 8\nmatch 00001451/0000ffff at 20\n",
            "commands": [
                ["qdisc", "add", "dev", "lo", "root", "handle", "34:", "prio"],
                [
                    "qdisc",
                    "add",
                    "dev",
                    "lo",
                    "parent",
                    "34:3",
                    "handle",
                    "343:",
                    "netem",
                    "rate",
                    "1000kbit",
                    "limit",
                    "5",
                ],
                [
                    "filter",
                    "add",
                    "dev",
                    "lo",
                    "protocol",
                    "ip",
                    "parent",
                    "34:",
                    "prio",
                    "3",
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    "17",
                    "0xff",
                    "match",
                    "ip",
                    "dport",
                    "5201",
                    "0xffff",
                    "flowid",
                    "34:3",
                ],
            ],
        }
    return {
        "case": case,
        "port": 5201,
        "producer": {
            "package_version": "0.3.0",
            "python_version": "3.14.2",
            "native_version": "iperf 3.21",
        },
        "server_returncode": -15,
        "listener_released": True,
        "reuse_native": reuse,
        "measurements": measurements,
        "impairment": impairment,
        "report_json": persisted,
        "report_sha256": hashlib.sha256(persisted.encode()).hexdigest(),
    }


@pytest.mark.parametrize("case", qualification.ADAPTIVE_CASES)
@pytest.mark.parametrize("native_version", ["3.21", "iperf 3.21"])
def test_adaptive_receipts_require_native_allocation_measurements_and_cleanup(case, native_version):
    """Clean and impaired decisions retain exact native settings, counts and history."""
    evidence = _adaptive_receipt(case)
    evidence["producer"]["native_version"] = native_version
    qualification._validate_adaptive_receipt(
        evidence,
        {"package_version": "0.3.0", "python": "3.14.2", "native_version": native_version},
        f"test_adaptive_integration.py::test_native_adaptive_udp_preserves_measured_decisions[{case}]",
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("producer", "native_version"), "iperf 3.19.1"),
        (("port",), 5202),
        (("listener_released",), False),
        (("server_returncode",), None),
        (("report_sha256",), "0" * 64),
        (("measurements",), []),
        (("measurements", 0, "sender", "bytes"), 0),
        (("measurements", 0, "receiver", "packets"), 0),
        (("measurements", 0, "native_start", "target_bitrate"), 1),
        (("impairment", "rate_bps"), 2_000_000),
        (("impairment", "destination_port"), 5202),
        (("impairment", "commands"), []),
        (("impairment", "filters"), []),
        (("impairment", "filters_text"), "match 00060000/00ff0000 at 8\n"),
        (("impairment", "configured", 1, "options", "limit"), 50),
        (("impairment", "configured", 1, "options", "limit"), 5.0),
        (("impairment", "configured", 1, "options", "rate", "rate"), 250_000),
        (("impairment", "configured", 1, "options", "rate", "rate"), 125_000.0),
        (("impairment", "after_traffic", 1, "drops"), 0),
        (("impairment", "after_traffic", 1, "packets"), False),
        (("impairment", "after_cleanup"), [{"kind": "netem", "handle": "343:"}]),
        (("reuse_native", "end", "sum_received", "bytes"), 0),
        (("reuse_native", "end", "sum_received", "lost_percent"), 50),
    ],
)
def test_adaptive_receipts_reject_missing_or_contradictory_measurements(path, value):
    """Green test phases cannot replace real native load, loss, impairment or cleanup evidence."""
    evidence = _adaptive_receipt("impaired-forward")
    container = evidence
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    with pytest.raises(ValueError, match="adaptive UDP receipt"):
        qualification._validate_adaptive_receipt(
            evidence,
            {"package_version": "0.3.0", "python": "3.14.2", "native_version": "iperf 3.21"},
            "test_adaptive_integration.py::test_native_adaptive_udp_preserves_measured_decisions[impaired-forward]",
        )


def test_adaptive_receipt_rejects_resealed_decision_without_matching_native_trials():
    """Rehashing an edited conclusion cannot establish a tested acceptable ceiling."""
    evidence = _adaptive_receipt("impaired-forward")
    report = json.loads(evidence["report_json"])
    report["result"]["highest_eligible_bps"] = 2_000_001
    report["result"]["ceiling_censored"] = True
    evidence["report_json"] = json.dumps(report)
    evidence["report_sha256"] = hashlib.sha256(evidence["report_json"].encode()).hexdigest()
    with pytest.raises(ValueError, match="adaptive UDP receipt"):
        qualification._validate_adaptive_receipt(
            evidence,
            {"package_version": "0.3.0", "python": "3.14.2", "native_version": "iperf 3.21"},
            "test_adaptive_integration.py::test_native_adaptive_udp_preserves_measured_decisions[impaired-forward]",
        )


def _async_plan_receipt(case):
    """Build explicit synthetic persisted evidence for fail-closed validator tests."""
    from iperf3_lib.artifacts import artifact_from_result
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.events import NativeEvent
    from iperf3_lib.plan_execution import PlanExecutionResult, PlanTrialRecord
    from iperf3_lib.plan_reports import dumps_plan_report, plan_report_from_execution
    from iperf3_lib.result import result_from_iperf_json
    from iperf3_lib.trials import PlanBudget, TrialException, TrialPolicy, TrialSpec, prepare_plan

    fault_case = case.startswith(("active-worker-crash-", "active-transport-failure-"))
    statuses, stop = (
        (["completed", "exception", "not_run"], "stop_on_error")
        if fault_case
        else {
            "active-cancel-tcp": (["completed", "cancelled", "not_run"], "cancelled"),
            "pause-cancel-tcp": (["completed", "not_run", "not_run"], "cancelled"),
            "active-deadline-udp": (["completed", "timed_out", "not_run"], "timeout"),
            "completed-tcp": (["completed", "completed", "completed"], None),
            "stop-on-error-udp": (["completed", "failed", "not_run"], "stop_on_error"),
        }[case]
    )
    protocol = case.rsplit("-", 1)[1]
    plan = prepare_plan(
        [
            TrialSpec(
                f"trial-{index}",
                f"cell-{index}",
                "measured",
                0,
                ClientConfig("127.0.0.1", duration=1, protocol=protocol),
            )
            for index in range(3)
        ],
        policy=TrialPolicy(repetitions=1, stop_on_error=True, pause_seconds=0.25),
        budget=PlanBudget(None, None),
    )
    workers = [
        {
            "protocol_version": 1,
            "run_index": 1,
            "pid": 200 + index,
            "request_id": str(index) * 32,
            "worker_id": str(index + 2) * 32,
            "producer": {
                "package_version": "0.3.0",
                "python_version": "3.14.2",
                "native_version": "iperf 3.21",
                "library_selector": {},
            },
        }
        for index in range(3)
    ]
    records = []
    for index, (spec, status) in enumerate(zip(plan.trials, statuses, strict=True)):
        if status == "not_run":
            records.append(PlanTrialRecord(spec, status, reason=stop))
            continue
        artifact = None
        events = ()
        if status in {"completed", "failed"}:
            native = (
                {
                    "start": {"test_start": {"protocol": protocol.upper(), "reverse": 0}},
                    "end": {"sum_received": {"bytes": 12, "seconds": 1}},
                }
                if status == "completed"
                else {"error": "connection refused"}
            )
            result = result_from_iperf_json(native)
            result.extensions["iperf3_lib.worker"] = workers[0]
            artifact = artifact_from_result(result)
        else:
            events = (NativeEvent("interval", {"sum": {"bytes": 12}}, 1, 1001.0),)
        records.append(
            PlanTrialRecord(
                spec,
                status,
                artifact=artifact,
                exception=TrialException(
                    "iperf3_lib._ipc.IPCError", "Native worker transport failed"
                )
                if status == "exception"
                else None,
                reason=stop if events and status != "exception" else None,
                started_at_seconds=1000.0 + index,
                completed_at_seconds=1001.0 + index,
                elapsed_seconds=1.0,
                partial_events=events,
                events_observed=len(events),
            )
        )
    execution = PlanExecutionResult(
        plan,
        tuple(records),
        1000.0,
        1010.0,
        10.0,
        0.25,
        stop,
        6 if case == "active-deadline-udp" else None,
    )
    encoded = dumps_plan_report(plan_report_from_execution(execution))
    admitted = sum(status != "not_run" for status in statuses)
    active = case.startswith("active-")
    fault = None
    if fault_case:
        observation = {"sequence": 1, "received_at_seconds": 1001.0, "bytes": 12}
        fault = {
            "kind": "sigkill" if case.startswith("active-worker-crash-") else "stdout-close",
            "worker": workers[2],
            "before": {
                "identity": {"pid": 202, "ppid": 100, "state": "S", "start_ticks": 302},
                "thread_children": {"202": []},
                "children": [],
                "fds": {"0": "pipe:[10]", "1": "pipe:[11]", "3": "socket:[12]"},
                "socket_fds": {"3": "socket:[12]"},
            },
            "measurements_before_fault": [observation.copy()],
            "returncode": -9 if case.startswith("active-worker-crash-") else -15,
            "identity_absent_after": True,
            "pipe_closed": True,
            "measurements": [observation.copy()],
        }
    return {
        "case": case,
        "protocol": protocol,
        "trial_statuses": statuses,
        "stop_reason": stop,
        "admitted_trials": admitted,
        "plan_client_workers": admitted,
        "total_workers_before_reuse": admitted + 1,
        "workers_reaped": True,
        "pipes_closed": True,
        "listener_released": True,
        "reused": True,
        "report_roundtrip": True,
        "detached": True,
        "completed_trial_bytes": 12,
        "reuse_bytes": 12,
        "active_bytes_before_stop": 12 if active else 0,
        "retained_interrupted_bytes": 12 if active else 0,
        "report_json": encoded,
        "report_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "completed_worker": workers[0],
        "reuse_worker": workers[1],
        "fault": fault,
    }


def _concurrent_plan_receipt(case):
    """Create synthetic overlapping native evidence to exercise qualification rejection."""
    from iperf3_lib.artifacts import artifact_from_result
    from iperf3_lib.concurrent_execution import (
        ConcurrentExecutionPolicy,
        ConcurrentPlanResult,
        ConcurrentTrialRecord,
        prepare_concurrent_plan,
    )
    from iperf3_lib.concurrent_reports import (
        concurrent_report_from_execution,
        dumps_concurrent_report,
    )
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.events import NativeEvent
    from iperf3_lib.result import result_from_iperf_json
    from iperf3_lib.trials import PlanBudget, TrialException, TrialPolicy, TrialSpec, prepare_plan

    interrupted, crash = case.startswith("two-active-"), case == "two-active-worker-crash-tcp"
    protocol = case.rsplit("-", 1)[1]
    conflicts = case == "conflicting-resources-tcp"
    statuses, stop, hard, maximum, cap = {
        "overlap-rate-cap-tcp": (["completed"] * 3, None, None, 3, 1_000_000),
        "conflicting-resources-tcp": (["completed"] * 4, None, None, 4, 2_000_000),
        "two-active-cancel-tcp": (
            ["cancelled", "not_run", "cancelled", "not_run"],
            "cancelled",
            "cancelled",
            2,
            1_500_000,
        ),
        "two-active-deadline-udp": (
            ["timed_out", "not_run", "timed_out", "not_run"],
            "timeout",
            "timeout",
            2,
            1_500_000,
        ),
        "two-active-worker-crash-tcp": (
            ["exception", "not_run", "completed", "not_run"],
            "stop_on_error",
            None,
            2,
            1_500_000,
        ),
    }[case]
    ports = [5201, 5202] if interrupted else [5201, 5202, 5203]
    specs = []
    for index in range(len(statuses)):
        endpoint = index // 2 if interrupted else ([0, 0, 1, 2][index] if conflicts else index)
        specs.append(
            TrialSpec(
                f"trial-{index}",
                f"cell-{index // 2 if interrupted else index}",
                ("warmup" if index % 2 == 0 else "measured") if interrupted else "measured",
                0,
                ClientConfig(
                    "127.0.0.1",
                    port=ports[endpoint],
                    protocol=protocol,
                    rate=500_000,
                    duration=30 if interrupted else 2,
                ),
            )
        )
    plan = prepare_plan(
        specs,
        policy=TrialPolicy(repetitions=1, warmup_runs=int(interrupted), stop_on_error=True),
        budget=PlanBudget(None, None),
    )
    policy = ConcurrentExecutionPolicy(maximum, cap)
    resources = {"cell-2": ("shared-link",), "cell-3": ("shared-link",)} if conflicts else {}
    plan, policy, resources, keys, targets = prepare_concurrent_plan(plan, policy, resources)
    workers = [
        {
            "protocol_version": 1,
            "run_index": 1,
            "pid": 300 + index,
            "request_id": f"{index + 10:032x}",
            "worker_id": f"{index + 100:032x}",
            "producer": {
                "package_version": "0.3.0",
                "python_version": "3.14.2",
                "native_version": "iperf 3.21",
                "library_selector": {},
            },
        }
        for index in range(8)
    ]
    timing = {0: (0.0, 2.0), 1: (0.1, 2.1), 2: (2.0, 4.0)}
    if conflicts:
        timing = {0: (0.0, 2.0), 1: (2.0, 4.0), 2: (0.1, 2.1), 3: (2.1, 4.1)}
    if interrupted:
        timing = {
            0: (0.0, 8.2 if protocol == "udp" else 2.0),
            2: (0.1, 8.3 if protocol == "udp" else 4.0),
        }
    order = {
        index: rank + 1
        for rank, index in enumerate(sorted(timing, key=lambda index: timing[index][0]))
    }
    records, owners, native_workers, measurements, completed, retained = [], {}, {}, {}, {}, {}
    for index, (spec, status) in enumerate(zip(plan.trials, statuses, strict=True)):
        if status == "not_run":
            records.append(
                ConcurrentTrialRecord(spec, status, keys[index], targets[index], reason=stop)
            )
            continue
        start, finish = timing[index]
        worker = workers[index]
        owners[spec.trial_id] = {
            "pid": worker["pid"],
            "started": start + 0.1,
            "finished": finish - 0.1,
        }
        native_workers[spec.trial_id] = worker
        measurements[spec.trial_id] = [
            {
                "at": start + 0.5,
                "bytes": 12,
                "worker_event_at_seconds": 1000.75 + int(start),
            }
        ]
        artifact, exception, events = None, None, ()
        if status == "completed":
            native = result_from_iperf_json(
                {
                    "start": {"test_start": {"protocol": protocol.upper(), "reverse": 0}},
                    "end": {"sum_received": {"bytes": 12, "seconds": 1}},
                }
            )
            native.extensions["iperf3_lib.worker"] = worker
            artifact = artifact_from_result(native)
            completed[spec.trial_id] = 12
        else:
            events = (
                NativeEvent(
                    "interval", {"sum": {"bytes": 12, "start": 0.0, "end": 0.25}}, 1, 1000.75
                ),
            )
            retained[spec.trial_id] = 12
            if status == "exception":
                exception = TrialException(
                    "iperf3_lib.exceptions.IperfLibraryError", "worker exited with signal 9"
                )
        records.append(
            ConcurrentTrialRecord(
                spec,
                status,
                keys[index],
                targets[index],
                order[index],
                start,
                finish,
                finish,
                artifact=artifact,
                exception=exception,
                reason=hard if hard else None,
                started_at_seconds=1000.0 + start,
                completed_at_seconds=1000.0 + finish,
                elapsed_seconds=finish - start,
                partial_events=events,
                events_observed=len(events),
            )
        )
    stopped = 8.0 if protocol == "udp" else (2.0 if crash else 1.0)
    execution = ConcurrentPlanResult(
        plan,
        policy,
        resources,
        tuple(records),
        1000.0,
        1010.0,
        10.0,
        stop,
        stopped if stop else None,
        hard,
        stopped if hard else None,
        8 if protocol == "udp" else None,
    )
    encoded = dumps_concurrent_report(concurrent_report_from_execution(execution))
    reuse = [
        {"port": port, "bytes": 12, "worker": workers[5 + index]}
        for index, port in enumerate(ports)
    ]
    return {
        "case": case,
        "protocol": protocol,
        "trial_statuses": statuses,
        "stop_reason": stop,
        "termination_reason": hard,
        "workers_reaped": True,
        "pipes_closed": True,
        "listeners_released": True,
        "report_roundtrip": True,
        "detached": True,
        "plan_client_workers": len(owners),
        "server_workers": len(ports),
        "total_workers_before_reuse": len(owners) + len(ports),
        "server_attempts": [2, 1, 1] if conflicts else [1] * len(ports),
        "ports": ports,
        "ownership": owners,
        "native_workers": native_workers,
        "measurements": measurements,
        "overlaps": [["trial-0", "trial-1"]]
        if case == "overlap-rate-cap-tcp"
        else [["trial-0", "trial-2"]],
        "exclusions": [["trial-0", "trial-1"], ["trial-2", "trial-3"]] if conflicts else [],
        "liveness_snapshots": [
            {
                "at": 0.7,
                "members": [
                    {"trial_id": name, "pid": owners[name]["pid"], "state": "S"}
                    for name in (
                        ("trial-0", "trial-1")
                        if case == "overlap-rate-cap-tcp"
                        else ("trial-0", "trial-2")
                    )
                ],
            }
        ],
        "completed_bytes": completed,
        "retained_bytes": retained,
        "killed_pid": workers[0]["pid"] if crash else None,
        "killed_returncode": -9 if crash else None,
        "reuse": reuse,
        "report_json": encoded,
        "report_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }


def _resource_stress_receipt(case):
    """Build explicit synthetic proc inventories for tamper-detection tests."""

    def identity(pid, parent):
        return {"pid": pid, "ppid": parent, "state": "S", "start_ticks": pid + 100}

    def snapshot(pid, parent, active=False):
        fds = {"0": "/dev/null", "1": f"pipe:[{pid + 1}]", "2": f"pipe:[{pid + 2}]"}
        if active:
            fds["3"] = f"socket:[{pid + 3}]"
        return {
            "identity": identity(pid, parent),
            "thread_children": {str(pid): []},
            "children": [],
            "fds": fds,
            "socket_fds": {key: value for key, value in fds.items() if value.startswith("socket:")},
        }

    baseline = snapshot(100, 99, True)
    modes = {
        "normal-tcp": ("complete", "consumer-error", "complete", "consumer-error"),
        "normal-udp": ("complete",) * 4,
        "cancel-deadline-tcp": ("cancel", "deadline", "cancel", "deadline"),
        "crash-transport-udp": ("crash", "transport", "crash", "transport"),
        "parent-death": ("parent-death",) * 4,
    }[case]
    cycles = []
    for index, mode in enumerate(modes):
        protocol = (
            ("tcp" if index < 2 else "udp") if mode == "parent-death" else case.rsplit("-", 1)[1]
        )
        parent_pid = 500 + index
        workers = []
        roles = (
            ["server", "client", "server", "client"]
            if mode == "parent-death" and index % 2
            else ["client", "server", "server", "client"]
        )
        for number, role in enumerate(roles):
            pid = 1000 + index * 4 + number
            parent = parent_pid if number == 0 and mode == "parent-death" else 100
            returncode = 0
            if number == 0:
                returncode = (
                    -9
                    if mode in ("crash", "parent-death")
                    else (-15 if mode in ("cancel", "deadline", "transport") else 0)
                )
            ready = {
                "protocol_version": 1,
                "run_index": 1,
                "pid": pid,
                "request_id": f"{pid:032x}",
                "worker_id": f"{pid + 10000:032x}",
                "producer": {
                    "package_version": "0.3.0",
                    "python_version": "3.14.2",
                    "native_version": "iperf 3.21",
                    "library_selector": {},
                },
            }
            workers.append(
                {
                    "pid": pid,
                    "role": role,
                    "ready": ready,
                    "snapshots": [
                        {"phase": phase, "inventory": snapshot(pid, parent, phase == "active")}
                        for phase in ("ready", "active")
                    ],
                    "traffic_bytes": 12,
                    "returncode": returncode,
                    "reaping_owner": "subreaper"
                    if number == 0 and mode == "parent-death"
                    else "library",
                    "identity_absent_after": True,
                }
            )
        cycle = {
            "index": index,
            "mode": mode,
            "protocol": protocol,
            "port": 5201 + index,
            "outcome": {
                "complete": "completed",
                "consumer-error": "consumer_error",
                "cancel": "cancelled",
                "deadline": "timeout",
                "crash": "worker_error",
                "transport": "worker_error",
                "parent-death": "parent_killed",
            }[mode],
            "target_pid": workers[0]["pid"],
            "workers": workers,
            "after_operation": copy.deepcopy(baseline),
            "after_reuse": copy.deepcopy(baseline),
            "reuse": {"bytes": 12, "worker": copy.deepcopy(workers[-1]["ready"])},
        }
        if mode == "parent-death":
            cycle.update(
                parent_identity=identity(parent_pid, 100),
                parent_returncode=-9,
                adopted_wait_status=9,
            )
        else:
            cycle.update(
                delivered_bytes=12,
                delivered_intervals=1,
                result_bytes=12 if mode == "complete" else 0,
            )
        cycles.append(cycle)
    return {"case": case, "baseline": baseline, "final": copy.deepcopy(baseline), "cycles": cycles}


def _check_resource_receipt(evidence):
    runtime = {"package_version": "0.3.0", "python": "3.14.2", "native_version": "iperf 3.21"}
    nodeid = f"test_resource_stress_integration.py::test_repeated_native_lifecycles_restore_owned_resources[{evidence['case']}]"
    qualification._validate_resource_receipt(evidence, runtime, nodeid)


@pytest.mark.parametrize("case", qualification.RESOURCE_CASES)
def test_resource_receipts_accept_each_complete_repeated_inventory(case):
    """Every required stress mode retains four cycles of measured cleanup and reuse."""
    _check_resource_receipt(_resource_stress_receipt(case))


@pytest.mark.parametrize(
    "change",
    [
        "missing-cycle",
        "boolean-index",
        "wrong-mode",
        "missing-worker",
        "no-traffic",
        "no-reuse",
        "no-snapshots",
        "zombie",
        "changed-birth",
        "wrong-parent",
        "descendant",
        "thread-child",
        "fd-leak",
        "socket-inventory",
        "reused-birth",
        "producer",
        "unreaped",
        "wrong-reaper",
        "wrong-exit",
        "parent-exit",
        "adoption",
        "consumer-repeated",
        "parent-fd-leak",
    ],
)
def test_resource_receipts_reject_missing_or_fabricated_resource_evidence(change):
    """Boolean success labels cannot hide leaked descriptors, descendants or stale identities."""
    case = (
        "parent-death"
        if change in ("wrong-parent", "parent-exit", "adoption", "parent-fd-leak")
        else "normal-tcp"
    )
    evidence = _resource_stress_receipt(case)
    cycle = evidence["cycles"][0]
    worker = cycle["workers"][0]
    active = worker["snapshots"][1]["inventory"]
    if change == "missing-cycle":
        evidence["cycles"].pop()
    elif change == "boolean-index":
        cycle["index"] = False
    elif change == "wrong-mode":
        cycle["mode"] = "complete-instead"
    elif change == "missing-worker":
        cycle["workers"].pop()
    elif change == "no-traffic":
        worker["traffic_bytes"] = 0
    elif change == "no-reuse":
        cycle["reuse"]["bytes"] = True
    elif change == "no-snapshots":
        worker["snapshots"].pop()
    elif change == "zombie":
        active["identity"]["state"] = "Z"
    elif change == "changed-birth":
        active["identity"]["start_ticks"] += 1
    elif change == "wrong-parent":
        active["identity"]["ppid"] = 100
    elif change == "descendant":
        active["children"] = [{"pid": 9999, "ppid": worker["pid"], "state": "S", "start_ticks": 10}]
    elif change == "thread-child":
        active["thread_children"][str(worker["pid"])] = [9999]
    elif change in ("fd-leak", "parent-fd-leak"):
        cycle["after_operation"]["fds"]["99"] = "pipe:[9999]"
    elif change == "socket-inventory":
        active["socket_fds"] = {}
    elif change == "reused-birth":
        evidence["cycles"][2]["workers"] = copy.deepcopy(cycle["workers"])
    elif change == "producer":
        worker["ready"]["producer"]["native_version"] = "unqualified native"
    elif change == "unreaped":
        worker["identity_absent_after"] = False
    elif change == "wrong-reaper":
        worker["reaping_owner"] = "subreaper"
    elif change == "wrong-exit":
        worker["returncode"] = -9
    elif change == "parent-exit":
        cycle["parent_returncode"] = 0
    elif change == "adoption":
        cycle["adopted_wait_status"] = 0
    else:
        evidence["cycles"][1]["delivered_intervals"] = 2
    with pytest.raises(ValueError, match="resource stress receipt"):
        _check_resource_receipt(evidence)


def test_lifecycle_results_require_every_expected_case_and_native_receipt():
    """The exact positive full selection can qualify."""
    qualification.validate_results(_passed_results())


def _check_concurrent_receipt(evidence):
    runtime = {"package_version": "0.3.0", "python": "3.14.2", "native_version": "iperf 3.21"}
    nodeid = (
        "test_concurrent_trials_integration.py::"
        f"test_native_concurrent_plan_preserves_bounds_and_owned_cleanup[{evidence['case']}]"
    )
    qualification._validate_concurrent_receipt(evidence, runtime, nodeid)


@pytest.mark.parametrize("case", qualification.CONCURRENT_CASES)
def test_concurrent_receipts_accept_measured_overlap_cleanup_and_reservations(case):
    """Each required topology has a valid saved report and matching process observations."""
    _check_concurrent_receipt(_concurrent_plan_receipt(case))


@pytest.mark.parametrize(
    "case,path,value",
    [
        ("two-active-cancel-tcp", ("workers_reaped",), False),
        ("two-active-cancel-tcp", ("pipes_closed",), False),
        ("two-active-cancel-tcp", ("listeners_released",), False),
        ("two-active-cancel-tcp", ("plan_client_workers",), True),
        ("two-active-cancel-tcp", ("termination_reason",), None),
        ("two-active-cancel-tcp", ("ownership", "trial-0", "pid"), True),
        ("two-active-cancel-tcp", ("native_workers", "trial-0", "pid"), 302),
        ("two-active-cancel-tcp", ("native_workers", "trial-0", "protocol_version"), True),
        (
            "two-active-cancel-tcp",
            ("native_workers", "trial-0", "producer", "python_version"),
            "3.12.0",
        ),
        ("two-active-cancel-tcp", ("measurements", "trial-0", 0, "bytes"), True),
        ("two-active-cancel-tcp", ("measurements", "trial-0", 0, "at"), float("nan")),
        (
            "two-active-cancel-tcp",
            ("measurements", "trial-0", 0, "worker_event_at_seconds"),
            1000.74,
        ),
        ("two-active-cancel-tcp", ("liveness_snapshots",), []),
        ("two-active-cancel-tcp", ("liveness_snapshots", 0, "at"), 0.3),
        ("two-active-cancel-tcp", ("liveness_snapshots", 0, "members", 0, "pid"), 302),
        ("two-active-cancel-tcp", ("liveness_snapshots", 0, "members", 1, "state"), "Z"),
        ("two-active-cancel-tcp", ("retained_bytes", "trial-0"), 12.0),
        ("two-active-cancel-tcp", ("ports", 1), 5201),
        ("two-active-cancel-tcp", ("reuse", 0, "bytes"), True),
        ("two-active-cancel-tcp", ("reuse", 0, "worker", "pid"), 300),
        ("two-active-cancel-tcp", ("report_sha256",), "0" * 64),
        ("two-active-worker-crash-tcp", ("killed_returncode",), 0),
        ("two-active-worker-crash-tcp", ("killed_pid",), 302),
        ("two-active-worker-crash-tcp", ("retained_bytes",), {}),
        (
            "two-active-worker-crash-tcp",
            ("measurements", "trial-0", 0, "worker_event_at_seconds"),
            1000.74,
        ),
        ("overlap-rate-cap-tcp", ("ownership", "trial-2", "started"), 0.2),
        ("conflicting-resources-tcp", ("ownership", "trial-1", "started"), 0.5),
    ],
)
def test_concurrent_receipts_reject_missing_or_contradictory_native_observations(case, path, value):
    """Passing phases cannot qualify fabricated overlap, cleanup, process identity or reuse."""
    evidence = _concurrent_plan_receipt(case)
    container = evidence
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    with pytest.raises(ValueError, match="concurrent plan receipt"):
        _check_concurrent_receipt(evidence)


@pytest.mark.parametrize("change", ["missing-events", "zero-bytes", "unobserved-event", "counts"])
def test_concurrent_crash_receipts_require_retained_observed_native_events(change):
    """A SIGKILL after measured traffic must preserve that evidence in its report."""
    evidence = _concurrent_plan_receipt("two-active-worker-crash-tcp")
    report = json.loads(evidence["report_json"])
    crashed = report["execution"]["trials"][0]
    assert crashed["status"] == "exception" and crashed["artifact"] is None
    if change == "missing-events":
        crashed.update(partial_events=[], events_observed=0, events_dropped=0)
        evidence["retained_bytes"] = {}
    elif change == "zero-bytes":
        crashed["partial_events"][0]["data"]["sum"]["bytes"] = 0
        evidence["retained_bytes"]["trial-0"] = 0
    elif change == "unobserved-event":
        crashed["partial_events"][0]["received_at_seconds"] = 1000.74
    else:
        crashed["events_observed"] += 1
    evidence["report_json"] = json.dumps(report)
    evidence["report_sha256"] = hashlib.sha256(evidence["report_json"].encode()).hexdigest()
    with pytest.raises(ValueError, match="concurrent plan receipt"):
        _check_concurrent_receipt(evidence)


@pytest.mark.parametrize(
    "change",
    [
        "worker-cap",
        "rate-cap",
        "resources",
        "pending",
        "release",
        "target",
        "admission",
        "producer",
        "stop",
        "traffic",
    ],
)
def test_concurrent_receipts_reject_resealed_invalid_reservations_and_outcomes(change):
    """Rehashing a report cannot conceal an impossible ledger or substituted native result."""
    case = "two-active-cancel-tcp" if change in ("pending", "stop") else "conflicting-resources-tcp"
    evidence = _concurrent_plan_receipt(case)
    report = json.loads(evidence["report_json"])
    execution = report["execution"]
    first = execution["trials"][0]
    if change == "worker-cap":
        execution["policy"]["max_workers"] = 1
    elif change == "rate-cap":
        execution["policy"]["max_active_target_bps"] = 500_000
    elif change == "resources":
        execution["resources"] = {}
    elif change == "pending":
        execution["trials"][1]["admission_index"] = 3
    elif change == "release":
        first["released_offset_seconds"] = None
    elif change == "target":
        first["target_bps"] = 1
    elif change == "admission":
        execution["trials"][2]["admission_index"] = 1
    elif change == "producer":
        report["producer"]["version"] = "0.0.0"
    elif change == "stop":
        execution["termination_reason"] = None
    else:
        first["artifact"]["result"]["raw"]["end"]["sum_received"]["bytes"] = 99
    evidence["report_json"] = json.dumps(report)
    evidence["report_sha256"] = hashlib.sha256(evidence["report_json"].encode()).hexdigest()
    with pytest.raises(ValueError, match="concurrent plan receipt"):
        _check_concurrent_receipt(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_client_workers", 3),
        ("admitted_trials", True),
        ("workers_reaped", False),
        ("pipes_closed", False),
        ("listener_released", False),
        ("completed_trial_bytes", 0),
        ("active_bytes_before_stop", 0),
        ("retained_interrupted_bytes", 0),
        ("reuse_bytes", True),
        ("trial_statuses", ["completed", "not_run", "not_run"]),
        ("stop_reason", None),
        ("report_sha256", "0" * 64),
        ("report_roundtrip", False),
        ("detached", False),
    ],
)
def test_async_plan_receipts_require_measured_partial_history_and_owned_cleanup(field, value):
    """Passing phase labels cannot replace native cleanup and persisted history evidence."""
    result = _passed_results()
    call = next(
        report
        for report in result["reports"]
        if report["phase"] == "call" and report["nodeid"].endswith("[active-cancel-tcp]")
    )
    evidence = json.loads(call["properties"]["async_plan"])
    evidence[field] = value
    call["properties"]["async_plan"] = json.dumps(evidence)
    with pytest.raises(ValueError, match="async plan receipt"):
        qualification.validate_results(result)


@pytest.mark.parametrize(
    "change", ["status", "artifact", "producer", "traffic", "events", "timing"]
)
def test_async_plan_receipts_reject_resealed_contradictory_report_evidence(change):
    """Updating a digest cannot hide a fabricated or contradictory partial report."""
    result = _passed_results()
    call = next(
        report
        for report in result["reports"]
        if report["phase"] == "call" and report["nodeid"].endswith("[active-cancel-tcp]")
    )
    evidence = json.loads(call["properties"]["async_plan"])
    report = json.loads(evidence["report_json"])
    if change == "status":
        report["execution"]["trials"][1]["status"] = "not_run"
    elif change == "artifact":
        report["execution"]["trials"][1]["artifact"] = report["execution"]["trials"][0]["artifact"]
    elif change == "producer":
        report["producer"]["version"] = "0.0.0"
    elif change == "traffic":
        report["execution"]["trials"][0]["artifact"]["result"]["raw"]["end"]["sum_received"][
            "bytes"
        ] = 99
    elif change == "events":
        report["execution"]["trials"][1]["partial_events"][0]["data"]["sum"]["bytes"] = 99
    else:
        report["execution"]["trials"][2]["started_at_seconds"] = 1004.0
    evidence["report_json"] = json.dumps(report)
    evidence["report_sha256"] = hashlib.sha256(evidence["report_json"].encode()).hexdigest()
    call["properties"]["async_plan"] = json.dumps(evidence)
    with pytest.raises(ValueError, match="async plan receipt"):
        qualification.validate_results(result)


def _check_async_plan_receipt(evidence):
    runtime = {"package_version": "0.3.0", "python": "3.14.2", "native_version": "iperf 3.21"}
    nodeid = (
        "test_async_trials_integration.py::"
        f"test_native_async_plan_retains_partial_history_and_releases_workers[{evidence['case']}]"
    )
    qualification._validate_plan_receipt(evidence, runtime, nodeid)


@pytest.mark.parametrize("case", qualification.PLAN_CASES)
def test_async_plan_receipts_accept_every_selected_native_scenario(case):
    """The installed selection includes both protocols and both active-worker fault types."""
    _check_async_plan_receipt(_async_plan_receipt(case))


@pytest.mark.parametrize(
    "path,value",
    [
        (("kind",), "stdout-close"),
        (("returncode",), 0),
        (("identity_absent_after",), False),
        (("pipe_closed",), False),
        (("worker", "pid"), 200),
        (("worker", "producer", "python_version"), "3.12.0"),
        (("before", "identity", "pid"), 999),
        (("before", "identity", "state"), "Z"),
        (("before", "socket_fds"), {}),
        (("before", "thread_children", "202"), [203]),
        (("measurements_before_fault",), []),
        (("measurements",), []),
        (("measurements", 0, "sequence"), True),
        (("measurements", 0, "bytes"), 0),
        (("measurements", 0, "received_at_seconds"), 999.0),
    ],
)
def test_async_plan_crash_receipts_reject_missing_or_contradictory_fault_evidence(path, value):
    """Cleanup labels alone cannot qualify a fault without process and observation evidence."""
    evidence = _async_plan_receipt("active-worker-crash-tcp")
    container = evidence["fault"]
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    with pytest.raises(ValueError, match="async plan receipt"):
        _check_async_plan_receipt(evidence)


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
@pytest.mark.parametrize("returncode", [0, True, -9, -15, 1])
def test_async_plan_transport_receipts_require_an_observed_terminated_exit(protocol, returncode):
    """Pipe interruption must produce a protocol failure and measured worker cleanup."""
    evidence = _async_plan_receipt(f"active-transport-failure-{protocol}")
    evidence["fault"]["returncode"] = returncode
    if type(returncode) is int and returncode in (-9, -15, 1):
        _check_async_plan_receipt(evidence)
    else:
        with pytest.raises(ValueError, match="async plan receipt"):
            _check_async_plan_receipt(evidence)


@pytest.mark.parametrize("change", ["missing-events", "unobserved-event", "counts", "exception"])
def test_async_plan_fault_receipts_reject_resealed_missing_or_fabricated_partial_events(change):
    """A real active fault cannot qualify if the persisted exception loses copied traffic."""
    evidence = _async_plan_receipt("active-transport-failure-udp")
    report = json.loads(evidence["report_json"])
    trial = report["execution"]["trials"][1]
    if change == "missing-events":
        trial.update(partial_events=[], events_observed=0, events_dropped=0)
    elif change == "unobserved-event":
        trial["partial_events"][0]["data"]["sum"]["bytes"] = 99
        evidence["retained_interrupted_bytes"] = 99
    elif change == "counts":
        trial["events_observed"] += 1
    else:
        trial["exception"]["type_name"] = "builtins.TimeoutError"
    evidence["report_json"] = json.dumps(report)
    evidence["report_sha256"] = hashlib.sha256(evidence["report_json"].encode()).hexdigest()
    with pytest.raises(ValueError, match="async plan receipt"):
        _check_async_plan_receipt(evidence)


@pytest.mark.parametrize(
    "path,value",
    [
        (("reused_client_worker", "protocol_version"), True),
        (("reused_client_worker", "run_index"), True),
        (("reused_client_worker", "pid"), True),
        (("cancelled_children",), True),
        (("measured_bytes_before_cancel",), False),
        (("reuse_bytes",), True),
        (("reuse_bytes",), "12"),
        (("reused_client_worker", "producer", "python_version"), "3.12.0"),
        (("reused_client_worker", "producer", "native_version"), "iperf 3.19.1"),
        (("reused_client_worker", "producer", "package_version"), "99.0.0"),
        (("reused_client_worker", "pid"), 201),
        (("reused_client_worker", "request_id"), "1" * 32),
        (("reused_client_worker", "worker_id"), "3" * 32),
    ],
    ids=[
        "boolean-protocol",
        "boolean-run",
        "boolean-pid",
        "boolean-count",
        "boolean-measurement",
        "boolean-reuse",
        "string-reuse",
        "wrong-python",
        "wrong-native",
        "wrong-package",
        "duplicate-pid",
        "duplicate-request",
        "duplicate-worker",
    ],
)
def test_cancellation_receipts_bind_integer_counts_and_distinct_producer_identity(path, value):
    """Receipt coercion or reused identities cannot qualify another measured worker."""
    result = _passed_results()
    report = next(
        report
        for report in result["reports"]
        if report["phase"] == "call" and "cancellation" in report["nodeid"]
    )
    receipt = json.loads(report["properties"]["cancellation"])
    target = receipt
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    report["properties"]["cancellation"] = json.dumps(receipt)
    with pytest.raises(ValueError, match="cancellation receipt"):
        qualification.validate_results(result)


@pytest.mark.parametrize(
    "case,field,value",
    [
        ("wrong-identity", "rejected", False),
        ("oversized-header", "case", "wrong-identity"),
        ("truncated-frame", "pipes_closed", False),
        ("saturated-events", "dropped", 0),
        ("saturated-events", "dropped", True),
        ("partial-frame-cancel", "cancelled", False),
        ("partial-frame-cancel", "workers_reaped", False),
    ],
)
def test_transport_receipts_require_observed_rejection_loss_and_cleanup(case, field, value):
    """A passing test status alone cannot qualify transport and process ownership."""
    result = _passed_results()
    report = next(
        report
        for report in result["reports"]
        if report["phase"] == "call"
        and "worker_ipc" in report["nodeid"]
        and report["nodeid"].endswith(f"[{case}]")
    )
    receipt = json.loads(report["properties"]["worker_ipc"])
    receipt[field] = value
    report["properties"]["worker_ipc"] = json.dumps(receipt)
    with pytest.raises(ValueError, match="worker IPC receipt"):
        qualification.validate_results(result)


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "unexpected",
        "skip",
        "xfail",
        "teardown",
        "no-evidence",
        "no-traffic",
        "no-reuse",
        "exit",
    ],
)
def test_lifecycle_results_fail_closed_on_incomplete_or_unmeasured_runs(failure):
    """Collection success and partial test success cannot substitute for lifecycle evidence."""
    result = copy.deepcopy(_passed_results())
    if failure == "missing":
        result["collected"].pop()
    elif failure == "duplicate":
        result["reports"].append(result["reports"][0])
    elif failure == "unexpected":
        result["collected"][0] = "unrelated_test.py::test_success"
    elif failure in {"skip", "xfail"}:
        result["reports"][1]["outcome"] = "skipped"
    elif failure == "teardown":
        result["reports"][2]["outcome"] = "failed"
    elif failure == "no-evidence":
        result["reports"][1]["properties"] = {}
    elif failure in {"no-traffic", "no-reuse"}:
        call = result["reports"][4]
        evidence = json.loads(call["properties"]["cancellation"])
        evidence["measured_bytes_before_cancel" if failure == "no-traffic" else "reused"] = 0
        call["properties"]["cancellation"] = json.dumps(evidence)
    else:
        result["exit_code"] = 1
    with pytest.raises(ValueError):
        qualification.validate_results(result)


def test_harness_dependency_versions_are_exact_frozen_interpreter_versions():
    """Every installed harness dependency is selected without unconstrained resolution."""
    pins = qualification.pinned_requirements()
    assert any(pin.startswith("pytest==") for pin in pins)
    assert any(pin.startswith("cffi==") for pin in pins)
    for pin in pins:
        name, version = pin.split("==")
        assert qualification.importlib.metadata.version(name) == version


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_signal", 15),
        ("worker_pid", 100),
        ("parent_returncode", 0),
        ("worker_reaped", False),
        ("traffic_bytes", 0),
        ("listener_released", False),
        ("reused", False),
        ("reuse_bytes", 0),
    ],
)
def test_parent_death_receipts_require_distinct_worker_death_and_measured_reuse(field, value):
    """A native receipt must substantiate parent death and cleanup, not merely exist."""
    result = _passed_results()
    report = next(
        report
        for report in result["reports"]
        if report["phase"] == "call"
        and "lifetime" in report["nodeid"]
        and "[active-" in report["nodeid"]
    )
    receipt = json.loads(report["properties"]["worker_lifetime"])
    receipt[field] = value
    report["properties"]["worker_lifetime"] = json.dumps(receipt)
    with pytest.raises(ValueError, match="parent-death receipt"):
        qualification.validate_results(result)


@pytest.mark.parametrize("skip", [False, True])
def test_isolated_pytest_hook_retains_actual_case_phases_and_properties(tmp_path, skip):
    """Exercise collection and pytest report hooks in an isolated subprocess."""
    import iperf3_lib

    expected = _passed_results()
    for filename, (test, property_name) in qualification.TESTS.items():
        cases = qualification.CASE_GROUPS[property_name]
        receipts = [
            report["properties"][property_name]
            for report in expected["reports"]
            if report["phase"] == "call" and report["nodeid"].startswith(filename)
        ]
        (tmp_path / filename).write_text(
            textwrap.dedent(f"""
            import pytest
            @pytest.mark.parametrize("index", range({len(cases)}), ids={cases!r})
            def {test}(index, record_property):
                if {skip!r} and index == 0:
                    pytest.skip("simulated unavailable qualification")
                record_property({property_name!r}, {receipts!r}[index])
        """)
        )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    program = textwrap.dedent("""
        import json, runpy, sys
        # This synthetic hook test uses the caller's existing test dependencies
        # explicitly; the production installed qualifier still enforces its venv.
        sys.path[:0] = json.loads(sys.argv[2])
        import pytest
        runner = runpy.run_path(sys.argv[1])
        plugin = runner['_Reports']()
        status = pytest.main(['-c', 'pytest.ini', '-q', *runner['expected_tests']()], plugins=[plugin])
        result = {'exit_code': int(status), 'collected': plugin.collected, 'reports': plugin.reports,
                  'python': '3.14.2', 'native_version': 'iperf 3.21', 'package_version': '0.3.0'}
        try:
            runner['validate_results'](result)
        except ValueError:
            print('QUALIFICATION_REJECTED')
        else:
            print('QUALIFICATION_PASSED')
    """)
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    fixture_imports = json.dumps(
        [
            str(Path(pytest.__file__).resolve().parent.parent),
            str(Path(iperf3_lib.__file__).resolve().parent.parent),
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program, qualification.__file__, fixture_imports],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert ("QUALIFICATION_REJECTED" if skip else "QUALIFICATION_PASSED") in completed.stdout
