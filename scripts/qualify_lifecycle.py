"""Qualify one retained wheel/sdist pair with isolated installed lifecycle tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

CASES = (
    "idle-server",
    "active-server-tcp",
    "active-client-tcp",
    "active-server-udp",
    "active-client-udp",
)
IPC_CASES = (
    "wrong-identity",
    "oversized-header",
    "truncated-frame",
    "saturated-events",
    "partial-frame-cancel",
)
PLAN_CASES = (
    "active-cancel-tcp",
    "pause-cancel-tcp",
    "active-deadline-udp",
    "completed-tcp",
    "stop-on-error-udp",
    "active-worker-crash-tcp",
    "active-worker-crash-udp",
    "active-transport-failure-tcp",
    "active-transport-failure-udp",
)
CONCURRENT_CASES = (
    "overlap-rate-cap-tcp",
    "conflicting-resources-tcp",
    "two-active-cancel-tcp",
    "two-active-deadline-udp",
    "two-active-worker-crash-tcp",
)
RESOURCE_CASES = (
    "normal-tcp",
    "normal-udp",
    "cancel-deadline-tcp",
    "crash-transport-udp",
    "parent-death",
)
ADAPTIVE_CASES = ("clean-forward", "clean-reverse", "impaired-forward")
CASE_GROUPS = {
    "cancellation": CASES,
    "worker_lifetime": CASES,
    "worker_ipc": IPC_CASES,
    "async_plan": PLAN_CASES,
    "concurrent_plan": CONCURRENT_CASES,
    "resource_stress": RESOURCE_CASES,
    "adaptive_udp": ADAPTIVE_CASES,
}
TESTS = {
    "test_cancellation_integration.py": (
        "test_native_cancellation_reaps_worker_releases_listener_and_allows_reuse",
        "cancellation",
    ),
    "test_worker_lifetime_integration.py": (
        "test_parent_death_stops_native_worker_and_releases_listener",
        "worker_lifetime",
    ),
    "test_worker_ipc_integration.py": (
        "test_installed_worker_transport_contract",
        "worker_ipc",
    ),
    "test_async_trials_integration.py": (
        "test_native_async_plan_retains_partial_history_and_releases_workers",
        "async_plan",
    ),
    "test_concurrent_trials_integration.py": (
        "test_native_concurrent_plan_preserves_bounds_and_owned_cleanup",
        "concurrent_plan",
    ),
    "test_resource_stress_integration.py": (
        "test_repeated_native_lifecycles_restore_owned_resources",
        "resource_stress",
    ),
    "test_adaptive_integration.py": (
        "test_native_adaptive_udp_preserves_measured_decisions",
        "adaptive_udp",
    ),
}
HELPERS = ("_native_resource_inventory.py",)
HARNESS = (
    "scripts/qualify_lifecycle.py",
    *(f"tests/{name}" for name in TESTS),
    *(f"tests/{name}" for name in HELPERS),
    "uv.lock",
)


def digest(path: Path) -> str:
    """Hash all bytes of one retained input."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_hashes(directory: Path) -> dict[str, str]:
    """Identify every Python module and the typing marker, excluding bytecode."""
    return {
        path.relative_to(directory).as_posix(): digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    }


def archive_hashes(path: Path, kind: str) -> dict[str, str]:
    """Read package sources directly from a wheel or sdist without extracting it."""
    contents = {}
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith("iperf3_lib/") and name.endswith((".py", "/py.typed")):
                    key = name.removeprefix("iperf3_lib/")
                    if key in contents:
                        raise ValueError("duplicate package archive member")
                    contents[key] = hashlib.sha256(archive.read(name)).hexdigest()
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/", 3)
                if len(parts) == 4 and parts[1:3] == ["src", "iperf3_lib"]:
                    key = parts[3]
                    if key.endswith(".py") or key == "py.typed":
                        stream = archive.extractfile(member) if member.isfile() else None
                        if stream is None or key in contents:
                            raise ValueError("invalid or duplicate package archive member")
                        with stream:
                            contents[key] = hashlib.sha256(stream.read()).hexdigest()
    if not contents or "__init__.py" not in contents:
        raise ValueError("distribution contains no identifiable package sources")
    return contents


def write_manifest(source: Path, distributions: Path, revision: str) -> dict:
    """Bind both distributions and the lifecycle harness to the source revision."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be a full commit SHA")
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    expected = {
        "wheel": f"iperf3_lib-{version}-py3-none-any.whl",
        "sdist": f"iperf3_lib-{version}.tar.gz",
    }
    actual = {path.name for path in distributions.iterdir() if path.name != ".gitignore"}
    if actual != set(expected.values()):
        raise ValueError("expected exactly one newly built wheel and sdist")
    sources = package_hashes(source / "src/iperf3_lib")
    artifacts = []
    for kind, name in expected.items():
        path = distributions / name
        if archive_hashes(path, kind) != sources:
            raise ValueError(f"{kind} package bytes differ from the source checkout")
        artifacts.append(
            {"kind": kind, "filename": name, "sha256": digest(path), "size": path.stat().st_size}
        )
    manifest = {
        "kind": "iperf3-lib.lifecycle-build",
        "schema_version": 1,
        "source_revision": revision,
        "package_version": version,
        "package_sha256": sources,
        "harness_sha256": {name: digest(source / name) for name in HARNESS},
        "distributions": artifacts,
    }
    (distributions / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_manifest(source: Path, distributions: Path, revision: str) -> dict:
    """Reject stale harnesses, substituted archives and mismatched source identity."""
    manifest = json.loads((distributions / "manifest.json").read_text())
    if (
        manifest.get("kind") != "iperf3-lib.lifecycle-build"
        or manifest.get("schema_version") != 1
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or manifest.get("source_revision") != revision
    ):
        raise ValueError("lifecycle manifest source identity mismatch")
    if manifest.get("harness_sha256") != {name: digest(source / name) for name in HARNESS}:
        raise ValueError("lifecycle harness differs from the sealed source")
    sources = package_hashes(source / "src/iperf3_lib")
    if manifest.get("package_sha256") != sources:
        raise ValueError("package source differs from the sealed source")
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if manifest.get("package_version") != version:
        raise ValueError("package version differs from the sealed source")
    entries = manifest.get("distributions", [])
    if len(entries) != 2 or {item.get("kind") for item in entries} != {"wheel", "sdist"}:
        raise ValueError("lifecycle qualification requires both distribution formats")
    for entry in entries:
        expected = (
            f"iperf3_lib-{version}-py3-none-any.whl"
            if entry["kind"] == "wheel"
            else f"iperf3_lib-{version}.tar.gz"
        )
        if entry.get("filename") != expected:
            raise ValueError("unexpected distribution filename")
        path = distributions / expected
        if digest(path) != entry.get("sha256") or path.stat().st_size != entry.get("size"):
            raise ValueError("distribution identity differs from the sealed artifact")
        if archive_hashes(path, entry["kind"]) != sources:
            raise ValueError("distribution package bytes differ from the sealed source")
    return manifest


def pinned_requirements() -> list[str]:
    """Reuse exact harness/runtime dependency versions from the frozen interpreter."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = ["pytest", "pytest-asyncio", "cffi"]
    versions = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in versions:
            continue
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        for value in distribution.requires or ():
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return [f"{name}=={version}" for name, version in sorted(versions.items())]


def require_installed_package(manifest: dict, prefix: Path, source: Path) -> str:
    """Reject editable/source imports and require installed bytes from the sealed pair."""
    import iperf3_lib

    location = Path(iperf3_lib.__file__).resolve()
    if not location.is_relative_to(prefix.resolve()) or location.is_relative_to(source.resolve()):
        raise ValueError("qualification imported source instead of the installed distribution")
    distribution = importlib.metadata.distribution("iperf3-lib")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise ValueError("editable distributions cannot qualify")
    if distribution.version != manifest["package_version"]:
        raise ValueError("installed version differs from the sealed distribution")
    if package_hashes(location.parent) != manifest["package_sha256"]:
        raise ValueError("installed package bytes differ from the sealed distribution")
    return str(location)


def expected_tests() -> list[str]:
    """Return the exact required parametrized lifecycle cases."""
    return [
        f"{name}::{test}[{case}]"
        for name, (test, property_name) in TESTS.items()
        for case in CASE_GROUPS[property_name]
    ]


class _Reports:
    def __init__(self):
        self.collected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "duration_seconds": report.duration,
                "properties": dict(report.user_properties),
                "detail": str(report.longrepr) if report.longrepr else None,
            }
        )


def _validate_plan_receipt(evidence: dict, result: dict, nodeid: str) -> None:
    """Validate persisted plan history together with measured ownership evidence."""
    from iperf3_lib.plan_reports import loads_plan_report

    case = nodeid.rsplit("[", 1)[1].removesuffix("]")
    fault_case = case.startswith(("active-worker-crash-", "active-transport-failure-"))
    statuses, stop, admitted = (
        (["completed", "exception", "not_run"], "stop_on_error", 2)
        if fault_case
        else {
            "active-cancel-tcp": (["completed", "cancelled", "not_run"], "cancelled", 2),
            "pause-cancel-tcp": (["completed", "not_run", "not_run"], "cancelled", 1),
            "active-deadline-udp": (["completed", "timed_out", "not_run"], "timeout", 2),
            "completed-tcp": (["completed", "completed", "completed"], None, 3),
            "stop-on-error-udp": (["completed", "failed", "not_run"], "stop_on_error", 2),
        }[case]
    )
    protocol = case.rsplit("-", 1)[1]
    active = case.startswith("active-")
    try:
        if (
            evidence["case"] != case
            or evidence["protocol"] != protocol
            or evidence["trial_statuses"] != statuses
            or evidence["stop_reason"] != stop
            or any(
                evidence[name] is not True
                for name in (
                    "workers_reaped",
                    "pipes_closed",
                    "listener_released",
                    "reused",
                    "report_roundtrip",
                    "detached",
                )
            )
            or any(
                type(evidence[name]) is not int or evidence[name] != expected
                for name, expected in (
                    ("admitted_trials", admitted),
                    ("plan_client_workers", admitted),
                    ("total_workers_before_reuse", admitted + 1),
                )
            )
            or any(
                type(evidence[name]) is not int or evidence[name] <= 0
                for name in ("completed_trial_bytes", "reuse_bytes")
            )
            or any(
                type(evidence[name]) is not int
                or (evidence[name] <= 0 if active else evidence[name] < 0)
                for name in ("active_bytes_before_stop", "retained_interrupted_bytes")
            )
            or not isinstance(evidence["report_json"], str)
            or hashlib.sha256(evidence["report_json"].encode("utf-8")).hexdigest()
            != evidence["report_sha256"]
        ):
            raise ValueError("missing measured ownership or persisted report identity")
        report = loads_plan_report(evidence["report_json"])
        execution = report.execution
        if (
            report.producer.name != "iperf3-lib"
            or report.producer.version != result["package_version"]
            or [trial.status for trial in execution.trials] != statuses
            or [trial.spec.trial_id for trial in execution.trials]
            != [f"trial-{i}" for i in range(3)]
            or execution.stop_reason != stop
            or execution.timeout_seconds != (6 if case == "active-deadline-udp" else None)
            or execution.cleanup_confirmed is not True
            or execution.execution_success is not (case == "completed-tcp")
            or sum(trial.started_at_seconds is not None for trial in execution.trials) != admitted
        ):
            raise ValueError("persisted execution disagrees with the required scenario")
        first_artifact = execution.trials[0].artifact
        if first_artifact is None:
            raise ValueError("completed trial has no native artifact")
        first = first_artifact.result
        if (
            first.raw["end"]["sum_received"]["bytes"] != evidence["completed_trial_bytes"]
            or first.raw["start"]["test_start"]["protocol"] != protocol.upper()
            or first.extensions["iperf3_lib.worker"] != evidence["completed_worker"]
        ):
            raise ValueError("completed native measurement or provenance differs from report")
        workers = [evidence[name] for name in ("completed_worker", "reuse_worker")]
        if fault_case:
            _validate_plan_fault(evidence, execution)
            workers.append(evidence["fault"]["worker"])
        elif evidence.get("fault") is not None:
            raise ValueError("non-fault plan has unexpected worker fault evidence")
        for worker in workers:
            if (
                type(worker["protocol_version"]) is not int
                or worker["protocol_version"] != 1
                or type(worker["run_index"]) is not int
                or worker["run_index"] != 1
                or type(worker["pid"]) is not int
                or worker["pid"] <= 0
                or any(
                    not isinstance(worker[key], str)
                    or not re.fullmatch("[0-9a-f]{32}", worker[key])
                    for key in ("request_id", "worker_id")
                )
                or worker["producer"]["package_version"] != result["package_version"]
                or worker["producer"]["python_version"] != result["python"]
                or worker["producer"]["native_version"] != result["native_version"]
                or not isinstance(worker["producer"]["library_selector"], dict)
            ):
                raise ValueError("native worker producer differs from installed runtime")
        if any(
            len({worker[key] for worker in workers}) != len(workers)
            for key in ("pid", "request_id", "worker_id")
        ):
            raise ValueError("reuse did not identify a distinct worker")
        retained = 0
        for trial in execution.trials:
            for event in trial.partial_events:
                if event.kind != "interval" or not isinstance(event.data, dict):
                    continue
                summary = event.data.get("sum")
                if isinstance(summary, dict):
                    count = summary.get("bytes", 0)
                    if type(count) is not int or count < 0:
                        raise ValueError("partial interval has invalid native byte evidence")
                    retained += count
        if retained != evidence["retained_interrupted_bytes"]:
            raise ValueError("retained interrupted traffic differs from partial report")
        if case == "pause-cancel-tcp" and execution.observed_pause_seconds <= 0:
            raise ValueError("pause cancellation did not retain observed pause time")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"async plan receipt lacks measured partial-history evidence: {nodeid}"
        ) from exc


def _validate_plan_fault(evidence: dict, execution) -> None:
    """Bind a real active-worker fault to copied observations and its final cleanup."""
    fault = evidence["fault"]
    crash = evidence["case"].startswith("active-worker-crash-")
    if set(fault) != {
        "kind",
        "worker",
        "before",
        "measurements_before_fault",
        "returncode",
        "identity_absent_after",
        "pipe_closed",
        "measurements",
    }:
        raise ValueError("incomplete active-worker fault evidence")
    birth = _resource_inventory(fault["before"])
    trial = execution.trials[1]
    if (
        fault["kind"] != ("sigkill" if crash else "stdout-close")
        or fault["identity_absent_after"] is not True
        or fault["pipe_closed"] is not True
        or type(fault["returncode"]) is not int
        or fault["returncode"] not in ((-9,) if crash else (-15, -9, 1))
        or birth[0] != fault["worker"]["pid"]
        or not fault["before"]["socket_fds"]
        or trial.status != "exception"
        or trial.artifact is not None
        or trial.exception is None
        or trial.exception.type_name != "iperf3_lib._ipc.IPCError"
    ):
        raise ValueError("worker fault lacks actual native identity, exception or cleanup")
    observations, before = fault["measurements"], fault["measurements_before_fault"]
    if (
        not isinstance(observations, list)
        or not observations
        or not isinstance(before, list)
        or not before
        or before != observations[: len(before)]
    ):
        raise ValueError("worker fault did not follow observed positive native traffic")
    previous = 0
    for observation in observations:
        if (
            set(observation) != {"sequence", "received_at_seconds", "bytes"}
            or not _positive_integer(observation["sequence"])
            or observation["sequence"] <= previous
            or not _positive_integer(observation["bytes"])
            or not _finite_number(observation["received_at_seconds"])
            or not trial.started_at_seconds
            <= observation["received_at_seconds"]
            <= trial.completed_at_seconds
        ):
            raise ValueError("invalid native observation for faulted worker")
        previous = observation["sequence"]
    retained = []
    for event in trial.partial_events:
        if event.kind == "interval" and isinstance(event.data, dict):
            summary = event.data.get("sum")
            if isinstance(summary, dict) and summary.get("bytes", 0) > 0:
                retained.append(
                    {
                        "sequence": event.sequence,
                        "received_at_seconds": event.received_at_seconds,
                        "bytes": summary["bytes"],
                    }
                )
    if (
        not retained
        or any(item not in observations for item in retained)
        or not any(item in retained for item in before)
    ):
        raise ValueError("faulted trial did not preserve its observed pre-fault interval evidence")


def _concurrent_worker(worker: dict, runtime: dict) -> None:
    """Bind one observed native process identity to the installed interpreter."""
    if (
        type(worker["protocol_version"]) is not int
        or worker["protocol_version"] != 1
        or type(worker["run_index"]) is not int
        or worker["run_index"] != 1
        or type(worker["pid"]) is not int
        or worker["pid"] <= 0
        or any(
            not isinstance(worker[key], str) or not re.fullmatch("[0-9a-f]{32}", worker[key])
            for key in ("request_id", "worker_id")
        )
        or worker["producer"]["package_version"] != runtime["package_version"]
        or worker["producer"]["python_version"] != runtime["python"]
        or worker["producer"]["native_version"] != runtime["native_version"]
        or not isinstance(worker["producer"]["library_selector"], dict)
    ):
        raise ValueError("native process identity differs from the installed runtime")


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _validate_concurrent_receipt(evidence: dict, runtime: dict, nodeid: str) -> None:
    """Cross-check persisted reservations against actual native owners and traffic."""
    from iperf3_lib.concurrent_reports import loads_concurrent_report

    case = nodeid.rsplit("[", 1)[1].removesuffix("]")
    interrupted = case.startswith("two-active-")
    crash = case == "two-active-worker-crash-tcp"
    protocol = case.rsplit("-", 1)[1]
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
    admitted = {f"trial-{index}" for index, status in enumerate(statuses) if status != "not_run"}
    server_count = 2 if interrupted else 3
    expected_pairs = (
        [["trial-0", "trial-1"]] if case == "overlap-rate-cap-tcp" else [["trial-0", "trial-2"]]
    )
    exclusions = (
        [["trial-0", "trial-1"], ["trial-2", "trial-3"]]
        if case == "conflicting-resources-tcp"
        else []
    )
    try:
        if (
            evidence["case"] != case
            or evidence["protocol"] != protocol
            or evidence["trial_statuses"] != statuses
            or evidence["stop_reason"] != stop
            or evidence["termination_reason"] != hard
            or evidence["overlaps"] != expected_pairs
            or evidence["exclusions"] != exclusions
            or any(
                evidence[key] is not True
                for key in (
                    "workers_reaped",
                    "pipes_closed",
                    "listeners_released",
                    "report_roundtrip",
                    "detached",
                )
            )
            or any(
                type(evidence[key]) is not int or evidence[key] != value
                for key, value in (
                    ("plan_client_workers", len(admitted)),
                    ("server_workers", server_count),
                    ("total_workers_before_reuse", len(admitted) + server_count),
                )
            )
            or not isinstance(evidence["report_json"], str)
            or hashlib.sha256(evidence["report_json"].encode()).hexdigest()
            != evidence["report_sha256"]
        ):
            raise ValueError("missing native ownership or persisted report identity")
        report = loads_concurrent_report(evidence["report_json"])
        execution = report.execution
        ports = evidence["ports"]
        if (
            report.schema_version != 3
            or report.producer.name != "iperf3-lib"
            or report.producer.version != runtime["package_version"]
            or [trial.status for trial in execution.trials] != statuses
            or [trial.spec.trial_id for trial in execution.trials]
            != [f"trial-{index}" for index in range(len(statuses))]
            or execution.stop_reason != stop
            or execution.termination_reason != hard
            or execution.timeout_seconds != (8 if protocol == "udp" else None)
            or execution.cleanup_confirmed is not True
            or execution.execution_success is not (not interrupted)
            or execution.policy.max_workers != maximum
            or execution.policy.max_active_target_bps != cap
            or execution.resources
            != ({"cell-2": ("shared-link",), "cell-3": ("shared-link",)} if exclusions else {})
            or len(ports) != server_count
            or any(not _positive_integer(port) or port > 65535 for port in ports)
            or len(set(ports)) != server_count
            or evidence["server_attempts"] != ([2, 1, 1] if exclusions else [1] * server_count)
            or any(type(value) is not int for value in evidence["server_attempts"])
            or set(evidence["ownership"]) != admitted
            or set(evidence["native_workers"]) != admitted
            or set(evidence["measurements"]) != admitted
        ):
            raise ValueError("persisted execution differs from the required scenario")
        workers, observed, completed, retained = [], evidence["ownership"], {}, {}
        for index, record in enumerate(execution.trials):
            name = record.spec.trial_id
            endpoint = index // 2 if interrupted else ([0, 0, 1, 2][index] if exclusions else index)
            if (
                record.spec.config.port != ports[endpoint]
                or record.spec.config.server != "127.0.0.1"
                or record.spec.config.protocol != protocol
                or record.spec.config.rate != 500_000
                or record.target_bps != 500_000
                or record.spec.phase
                != (("warmup" if index % 2 == 0 else "measured") if interrupted else "measured")
            ):
                raise ValueError(
                    "declared endpoint, dependency or rate differs from native scenario"
                )
            if record.status == "not_run":
                continue
            owner, worker = observed[name], evidence["native_workers"][name]
            _concurrent_worker(worker, runtime)
            workers.append(worker)
            if (
                type(owner["pid"]) is not int
                or owner["pid"] != worker["pid"]
                or not _finite_number(owner["started"])
                or not _finite_number(owner["finished"])
                or owner["started"] >= owner["finished"]
                or record.released_offset_seconds != record.finished_offset_seconds
                or record.released_offset_seconds is None
            ):
                raise ValueError("admitted worker lacks confirmed consumption and release")
            measurements = evidence["measurements"][name]
            if not measurements or any(
                not _positive_integer(item["bytes"])
                or not _finite_number(item["at"])
                or not owner["started"] <= item["at"] <= owner["finished"]
                or not _finite_number(item["worker_event_at_seconds"])
                for item in measurements
            ):
                raise ValueError("worker lacks positive live native intervals")
            if record.artifact is not None:
                native = record.artifact.result
                count = native.raw["end"]["sum_received"]["bytes"]
                if (
                    not _positive_integer(count)
                    or native.raw["start"]["test_start"]["protocol"] != protocol.upper()
                    or native.extensions["iperf3_lib.worker"] != worker
                ):
                    raise ValueError("completed native artifact differs from observed worker")
                completed[name] = count
            else:
                count = 0
                for event in record.partial_events:
                    if event.kind == "interval" and isinstance(event.data, dict):
                        summary = event.data.get("sum")
                        if isinstance(summary, dict):
                            value = summary.get("bytes", 0)
                            if type(value) is not int or value < 0:
                                raise ValueError("invalid partial native byte evidence")
                            if value > 0 and not any(
                                item["bytes"] == value
                                and item["worker_event_at_seconds"] == event.received_at_seconds
                                for item in measurements
                            ):
                                raise ValueError(
                                    "retained interval differs from observed native callback"
                                )
                            count += value
                if count <= 0:
                    raise ValueError("unfinished worker has no retained positive measurement")
                retained[name] = count
        if (
            evidence["completed_bytes"] != completed
            or evidence["retained_bytes"] != retained
            or any(
                not _positive_integer(value)
                for key in ("completed_bytes", "retained_bytes")
                for value in evidence[key].values()
            )
        ):
            raise ValueError("persisted native bytes differ from scenario evidence")
        for first, second in expected_pairs:
            start = max(observed[first]["started"], observed[second]["started"])
            finish = min(observed[first]["finished"], observed[second]["finished"])
            if start >= finish or not all(
                any(start <= item["at"] < finish for item in evidence["measurements"][name])
                for name in (first, second)
            ):
                raise ValueError("positive observations do not overlap owned worker lifetimes")
            snapshots = evidence["liveness_snapshots"]
            if not snapshots:
                raise ValueError("positive workers lack an observed live PID snapshot")
            for sample in snapshots:
                if (
                    not _finite_number(sample["at"])
                    or not start <= sample["at"] < finish
                    or [member["trial_id"] for member in sample["members"]] != [first, second]
                    or any(
                        type(member["pid"]) is not int
                        or member["pid"] != observed[member["trial_id"]]["pid"]
                        or member["state"] not in ("R", "S", "D", "T", "t", "W", "K", "P", "I")
                        for member in sample["members"]
                    )
                    or not all(
                        any(
                            start <= item["at"] <= sample["at"]
                            for item in evidence["measurements"][name]
                        )
                        for name in (first, second)
                    )
                ):
                    raise ValueError("positive workers were not both observed alive before cleanup")
        for first, second in exclusions:
            if observed[first]["finished"] > observed[second]["started"]:
                raise ValueError("conflicting actual workers overlapped")
        if case == "overlap-rate-cap-tcp" and observed["trial-2"]["started"] < min(
            observed[name]["finished"] for name in ("trial-0", "trial-1")
        ):
            raise ValueError("third actual worker exceeded admitted target rate")
        if (
            len(evidence["reuse"]) != server_count
            or [item["port"] for item in evidence["reuse"]] != ports
        ):
            raise ValueError("not every affected endpoint was reused")
        for item in evidence["reuse"]:
            if not _positive_integer(item["bytes"]):
                raise ValueError("endpoint reuse lacks positive native measurement")
            _concurrent_worker(item["worker"], runtime)
            workers.append(item["worker"])
        if any(
            len({worker[key] for worker in workers}) != len(workers)
            for key in ("pid", "request_id", "worker_id")
        ):
            raise ValueError("native owners and endpoint reuse must identify distinct workers")
        if crash:
            if (
                type(evidence["killed_pid"]) is not int
                or evidence["killed_pid"] != observed["trial-0"]["pid"]
                or type(evidence["killed_returncode"]) is not int
                or evidence["killed_returncode"] != -9
            ):
                raise ValueError("crash case lacks the killed owner's observed exit")
        elif evidence["killed_pid"] is not None or evidence["killed_returncode"] is not None:
            raise ValueError("non-crash scenario includes a killed worker")
    except (AttributeError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise ValueError(
            f"concurrent plan receipt lacks measured reservation evidence: {nodeid}"
        ) from exc


def _inventory_identity(identity: dict) -> tuple[int, int]:
    if (
        set(identity) != {"pid", "ppid", "state", "start_ticks"}
        or not _positive_integer(identity["pid"])
        or type(identity["ppid"]) is not int
        or identity["ppid"] < 0
        or type(identity["start_ticks"]) is not int
        or identity["start_ticks"] < 0
        or identity["state"] not in ("R", "S", "D", "T", "t", "W", "K", "P", "I")
    ):
        raise ValueError("invalid live process birth identity")
    return identity["pid"], identity["start_ticks"]


def _resource_inventory(snapshot: dict) -> tuple[int, int]:
    if set(snapshot) != {"identity", "thread_children", "children", "fds", "socket_fds"}:
        raise ValueError("incomplete resource inventory")
    identity = _inventory_identity(snapshot["identity"])
    if (
        not isinstance(snapshot["thread_children"], dict)
        or not snapshot["thread_children"]
        or any(
            not isinstance(tid, str)
            or not tid.isascii()
            or not tid.isdigit()
            or int(tid) <= 0
            or children != []
            for tid, children in snapshot["thread_children"].items()
        )
        or snapshot["children"] != []
        or not isinstance(snapshot["fds"], dict)
        or not snapshot["fds"]
        or any(
            not isinstance(fd, str)
            or not fd.isascii()
            or not fd.isdigit()
            or not isinstance(target, str)
            or not target
            for fd, target in snapshot["fds"].items()
        )
        or snapshot["socket_fds"]
        != {
            fd: target
            for fd, target in snapshot["fds"].items()
            if re.fullmatch(r"socket:\[[0-9]+\]", target)
        }
    ):
        raise ValueError(
            "resource inventory contains descendants or inconsistent descriptor targets"
        )
    return identity


def _validate_resource_receipt(evidence: dict, runtime: dict, nodeid: str) -> None:
    """Require repeated native measurements and process/FD inventories for every cycle."""
    case = nodeid.rsplit("[", 1)[1].removesuffix("]")
    modes = {
        "normal-tcp": ("complete", "consumer-error", "complete", "consumer-error"),
        "normal-udp": ("complete",) * 4,
        "cancel-deadline-tcp": ("cancel", "deadline", "cancel", "deadline"),
        "crash-transport-udp": ("crash", "transport", "crash", "transport"),
        "parent-death": ("parent-death",) * 4,
    }[case]
    try:
        if evidence["case"] != case or len(evidence["cycles"]) != 4:
            raise ValueError("resource stress must retain all four cycles")
        baseline = evidence["baseline"]
        owner = _resource_inventory(baseline)
        final = evidence["final"]
        if _resource_inventory(final) != owner or final["fds"] != baseline["fds"]:
            raise ValueError("final process/FD inventory differs from the warmed baseline")
        identities, request_ids, worker_ids = set(), set(), set()
        for index, (cycle, mode) in enumerate(zip(evidence["cycles"], modes, strict=True)):
            protocol = (
                ("tcp" if index < 2 else "udp")
                if mode == "parent-death"
                else case.rsplit("-", 1)[1]
            )
            outcome = {
                "complete": "completed",
                "consumer-error": "consumer_error",
                "cancel": "cancelled",
                "deadline": "timeout",
                "crash": "worker_error",
                "transport": "worker_error",
                "parent-death": "parent_killed",
            }[mode]
            if (
                type(cycle["index"]) is not int
                or cycle["index"] != index
                or cycle["mode"] != mode
                or cycle["protocol"] != protocol
                or cycle["outcome"] != outcome
                or not _positive_integer(cycle["port"])
                or cycle["port"] > 65535
                or not _positive_integer(cycle["target_pid"])
                or len(cycle["workers"]) != 4
                or sorted(worker["role"] for worker in cycle["workers"])
                != ["client", "client", "server", "server"]
            ):
                raise ValueError(
                    "resource stress lifecycle or worker inventory differs from required cycle"
                )
            for name in ("after_operation", "after_reuse"):
                snapshot = cycle[name]
                if _resource_inventory(snapshot) != owner or snapshot["fds"] != baseline["fds"]:
                    raise ValueError(
                        "cycle left a process or descriptor beyond its warmed baseline"
                    )
            parent_pid = owner[0]
            if mode == "parent-death":
                parent_pid, _ = _inventory_identity(cycle["parent_identity"])
                if (
                    parent_pid == owner[0]
                    or cycle["parent_identity"]["ppid"] != owner[0]
                    or type(cycle["parent_returncode"]) is not int
                    or cycle["parent_returncode"] != -9
                    or type(cycle["adopted_wait_status"]) is not int
                    or cycle["adopted_wait_status"] != 9
                ):
                    raise ValueError("parent-death case lacks owned subreaper exit evidence")
            elif (
                not _positive_integer(cycle["delivered_bytes"])
                or not _positive_integer(cycle["delivered_intervals"])
                or type(cycle["result_bytes"]) is not int
                or (
                    cycle["result_bytes"] <= 0 if mode == "complete" else cycle["result_bytes"] != 0
                )
                or (mode == "consumer-error" and cycle["delivered_intervals"] != 1)
            ):
                raise ValueError("resource cycle lacks positive native or consumer-stop evidence")
            matched_target = False
            for worker in cycle["workers"]:
                target = worker["pid"] == cycle["target_pid"]
                matched_target |= target
                _concurrent_worker(worker["ready"], runtime)
                if (
                    type(worker["pid"]) is not int
                    or worker["pid"] != worker["ready"]["pid"]
                    or not _positive_integer(worker["traffic_bytes"])
                    or worker["identity_absent_after"] is not True
                    or type(worker["returncode"]) is not int
                    or worker["reaping_owner"]
                    != ("subreaper" if mode == "parent-death" and target else "library")
                    or [sample["phase"] for sample in worker["snapshots"]] != ["ready", "active"]
                ):
                    raise ValueError("worker lacks measured native activity and owned reaping")
                before, active = (sample["inventory"] for sample in worker["snapshots"])
                identity = _resource_inventory(before)
                if (
                    _resource_inventory(active) != identity
                    or identity[0] != worker["pid"]
                    or before["identity"]["ppid"]
                    != (parent_pid if mode == "parent-death" and target else owner[0])
                    or active["identity"]["ppid"] != before["identity"]["ppid"]
                    or not active["socket_fds"]
                    or identity in identities
                    or worker["ready"]["request_id"] in request_ids
                    or worker["ready"]["worker_id"] in worker_ids
                ):
                    raise ValueError(
                        "worker birth identity, socket evidence or session identity is inconsistent"
                    )
                identities.add(identity)
                request_ids.add(worker["ready"]["request_id"])
                worker_ids.add(worker["ready"]["worker_id"])
                if target and mode in ("crash", "parent-death"):
                    if worker["returncode"] != -9:
                        raise ValueError("killed worker lacks its actual SIGKILL exit")
                elif target and mode in ("cancel", "deadline", "transport"):
                    if worker["returncode"] not in (-15, -9, 1):
                        raise ValueError("forced worker did not record a terminated exit")
                elif worker["returncode"] != 0:
                    raise ValueError("normally completed native worker did not exit cleanly")
            reuse = cycle["reuse"]
            if (
                not matched_target
                or not _positive_integer(reuse["bytes"])
                or reuse["worker"]["pid"] == cycle["target_pid"]
                or sum(
                    worker["ready"] == reuse["worker"] and worker["role"] == "client"
                    for worker in cycle["workers"]
                )
                != 1
            ):
                raise ValueError("cycle lacks a distinct measured endpoint reuse")
    except (AttributeError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise ValueError(
            f"resource stress receipt lacks repeated native inventory evidence: {nodeid}"
        ) from exc


def _validate_adaptive_receipt(evidence: dict, runtime: dict, nodeid: str) -> None:
    """Bind tested UDP decisions to native JSON, owned impairment and measured reuse."""
    from iperf3_lib.adaptive_reports import loads_adaptive_udp_report

    case = nodeid.rsplit("[", 1)[1].removesuffix("]")
    impaired = case == "impaired-forward"
    native_header = "iperf " + runtime["native_version"].removeprefix("iperf ")
    try:
        if (
            evidence["case"] != case
            or type(evidence["port"]) is not int
            or not 1024 <= evidence["port"] <= 65535
            or evidence["listener_released"] is not True
            or type(evidence["server_returncode"]) is not int
            or evidence["server_returncode"] not in (-15, 0, 1)
            or evidence["producer"]
            != {
                "package_version": runtime["package_version"],
                "python_version": runtime["python"],
                "native_version": runtime["native_version"],
            }
            or hashlib.sha256(evidence["report_json"].encode()).hexdigest()
            != evidence["report_sha256"]
        ):
            raise ValueError("adaptive producer, process or persisted report identity differs")
        result = loads_adaptive_udp_report(evidence["report_json"]).result
        prepared = result.prepared
        policy = prepared.adaptive_policy
        if (
            prepared.initial_rates != (250_001, 2_000_001)
            or prepared.config.server != "127.0.0.1"
            or prepared.config.port != evidence["port"]
            or prepared.config.protocol != "udp"
            or prepared.config.reverse != (case == "clean-reverse")
            or prepared.config.bidirectional
            or prepared.config.duration != 1
            or prepared.config.omit != 0
            or prepared.config.parallel != 2
            or prepared.config.blksize != 1200
            or prepared.policy.repetitions != 2
            or prepared.policy.warmup_runs != 1
            or prepared.policy.pause_seconds != 0.1
            or prepared.policy.max_trials != 18
            or prepared.budget.max_active_seconds != 18
            or prepared.budget.max_payload_bytes != 8_000_000
            or prepared.budget.stop_after_elapsed_seconds is not None
            or policy.min_rate_bps != 250_001
            or policy.max_rate_bps != 2_000_001
            or policy.max_distinct_rates != 3
            or policy.max_refinement_depth != 1
            or policy.receiver_loss_percent != 5
            or policy.minimum_valid_trials != 2
            or policy.minimum_sender_fraction != 0.9
            or policy.confirmation_batches != 1
            or result.outcome != "acceptable_tested_rates"
            or result.decision.batch is not None
        ):
            raise ValueError("adaptive report does not describe the selected bounded experiment")
        records = [record for batch in result.batches for record in batch.execution.trials]
        observations = {
            item.trial_id: item for summary in result.summaries for item in summary.observations
        }
        if (
            not records
            or len(records) > 18
            or len(evidence["measurements"]) != len(records)
            or len(observations) != len(records)
            or not any(record.spec.phase == "warmup" for record in records)
            or not any(summary.confirmation_batches == 1 for summary in result.summaries)
        ):
            raise ValueError(
                "adaptive receipt lacks complete bounded trial and confirmation history"
            )
        for record, measured in zip(records, evidence["measurements"], strict=True):
            if record.status != "completed" or record.artifact is None:
                raise ValueError("selected native adaptive trial did not complete")
            raw = record.artifact.result.raw
            start = raw["start"]["test_start"]
            sender, receiver = raw["end"]["sum_sent"], raw["end"]["sum_received"]
            observation = observations[record.spec.trial_id]
            if (
                measured
                != {
                    "trial_id": record.spec.trial_id,
                    "native_start": start,
                    "sender": sender,
                    "receiver": receiver,
                }
                or raw["start"]["version"] != native_header
                or start["protocol"] != "UDP"
                or start["target_bitrate"] != record.spec.resolved_config.rate
                or start["num_streams"] != 2
                or start["duration"] != 1
                or start["omit"] != 0
                or start["blksize"] != 1200
                or start["reverse"] != int(case == "clean-reverse")
                or start["bidir"] != 0
                or observation.allocated_rate_bps != 2 * start["target_bitrate"]
                or observation.unused_rate_bps != 1
                or observation.native_per_stream_bps != start["target_bitrate"]
                or observation.valid != (record.spec.phase == "measured")
                or any(
                    check.state not in ("matched", "native_default")
                    for check in observation.setting_checks
                )
            ):
                raise ValueError(
                    "native target, allocation or evidence does not match adaptive trial"
                )
            for endpoint, native in (("sender", sender), ("receiver", receiver)):
                if (
                    not _positive_integer(native["bytes"])
                    or not _finite_number(native["seconds"])
                    or native["seconds"] <= 0
                    or getattr(observation, f"{endpoint}_bytes") != native["bytes"]
                    or getattr(observation, f"{endpoint}_seconds") != native["seconds"]
                    or getattr(observation, f"{endpoint}_bps")
                    != 8 * native["bytes"] / native["seconds"]
                ):
                    raise ValueError("adaptive rates lack positive byte/time observations")
            if (
                not _positive_integer(receiver["packets"])
                or type(receiver["lost_packets"]) is not int
                or not 0 <= receiver["lost_packets"] <= receiver["packets"]
                or observation.receiver_packets != receiver["packets"]
                or observation.receiver_lost_packets != receiver["lost_packets"]
                or observation.count_loss_percent
                != 100 * receiver["lost_packets"] / receiver["packets"]
                or observation.native_loss_percent != receiver["lost_percent"]
                or observation.sender_bps is None
                or observation.sender_fraction is None
                or observation.sender_fraction
                != observation.sender_bps / observation.requested_rate_bps
                or observation.sender_fraction < 0.9
            ):
                raise ValueError("adaptive loss or achieved offered load lacks native evidence")
        if impaired:
            _validate_adaptive_impairment(evidence, result)
        elif (
            evidence["impairment"] is not None
            or result.highest_eligible_bps != 2_000_001
            or not result.ceiling_censored
        ):
            raise ValueError("clean bounded experiment did not establish its tested ceiling")
        reuse = evidence["reuse_native"]
        start = reuse["start"]["test_start"]
        received = reuse["end"]["sum_received"]
        if (
            reuse["start"]["version"] != native_header
            or start["protocol"] != "UDP"
            or start["target_bitrate"] != 250_000
            or start["duration"] != 1
            or start["num_streams"] != 1
            or start["reverse"] != 0
            or not _positive_integer(received["bytes"])
            or not _positive_integer(received["packets"])
            or not _finite_number(received["seconds"])
            or received["seconds"] <= 0
            or received["lost_percent"] > 5
        ):
            raise ValueError("adaptive cleanup lacks successful measured endpoint reuse")
    except (AttributeError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise ValueError(f"adaptive UDP receipt lacks measured native evidence: {nodeid}") from exc


def _validate_adaptive_impairment(evidence: dict, result) -> None:
    """Require owned UDP-only rate settings, observed drops and restored loopback."""
    impaired = evidence["impairment"]
    port = evidence["port"]
    commands = [
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
            str(port),
            "0xffff",
            "flowid",
            "34:3",
        ],
    ]
    if (
        impaired["device"] != "lo"
        or type(impaired["rate_bps"]) is not int
        or impaired["rate_bps"] != 1_000_000
        or type(impaired["limit_packets"]) is not int
        or impaired["limit_packets"] != 5
        or impaired["protocol"] != "udp"
        or impaired["destination_port"] != port
        or impaired["commands"] != commands
        or not impaired["filters"]
        or not isinstance(impaired["filters_text"], str)
        or re.search(r"match\s+00110000/00ff0000\s+at\s+8\b", impaired["filters_text"]) is None
        or re.search(rf"match\s+{port:08x}/0000ffff\s+at\s+20\b", impaired["filters_text"]) is None
        or not any(
            item["kind"] == "netem"
            and item["handle"] == "343:"
            and type(item["options"]["limit"]) is int
            and item["options"]["limit"] == 5
            and type(item["options"]["rate"]["rate"]) is int
            and item["options"]["rate"]["rate"] == 125_000
            for item in impaired["configured"]
        )
        or not any(
            item["kind"] == "netem"
            and item["handle"] == "343:"
            and _positive_integer(item["drops"])
            and _positive_integer(item["bytes"])
            and _positive_integer(item["packets"])
            and type(item["options"]["limit"]) is int
            and item["options"]["limit"] == 5
            and type(item["options"]["rate"]["rate"]) is int
            and item["options"]["rate"]["rate"] == 125_000
            for item in impaired["after_traffic"]
        )
    ):
        raise ValueError("configured impairment lacks matching native drop observations")
    for key in ("before", "after_cleanup"):
        queue = impaired[key]
        if len(queue) != 1 or queue[0]["kind"] != "noqueue" or queue[0]["handle"] != "0:":
            raise ValueError("owned impairment was not restored to its original default")
    summaries = {summary.rate_bps: summary for summary in result.summaries}
    lower, upper = summaries[250_001], summaries[2_000_001]
    if (
        lower.status != "eligible"
        or upper.status != "rejected"
        or result.highest_eligible_bps is None
        or result.highest_eligible_bps >= 2_000_001
        or result.ceiling_censored
        or not any(item.count_loss_percent > 5 for item in upper.observations)
        or len(summaries) != 3
    ):
        raise ValueError("controlled impairment did not establish measured low/high decisions")


def validate_results(result: dict) -> None:
    """Require every selected case to pass setup, execution, teardown and evidence checks."""
    expected = expected_tests()
    if result.get("exit_code") != 0 or sorted(result.get("collected", [])) != sorted(expected):
        raise ValueError("lifecycle qualification did not execute exactly the required cases")
    reports = result.get("reports", [])
    if len(reports) != len(expected) * 3:
        raise ValueError("lifecycle qualification has missing or duplicate test phases")
    for nodeid in expected:
        phases = [report for report in reports if report.get("nodeid") == nodeid]
        if (
            len(phases) != 3
            or {report.get("phase") for report in phases} != {"setup", "call", "teardown"}
            or any(report.get("outcome") != "passed" for report in phases)
        ):
            raise ValueError(f"lifecycle case failed, skipped or did not finish: {nodeid}")
        name, _ = nodeid.split("::", 1)
        property_name = TESTS[name][1]
        call = next(report for report in phases if report["phase"] == "call")
        try:
            evidence = json.loads(call["properties"][property_name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"lifecycle case has no native receipt: {nodeid}") from exc
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError(f"lifecycle case has an empty native receipt: {nodeid}")
        if property_name == "async_plan":
            _validate_plan_receipt(evidence, result, nodeid)
        elif property_name == "concurrent_plan":
            _validate_concurrent_receipt(evidence, result, nodeid)
        elif property_name == "resource_stress":
            _validate_resource_receipt(evidence, result, nodeid)
        elif property_name == "adaptive_udp":
            _validate_adaptive_receipt(evidence, result, nodeid)
        elif property_name == "cancellation":
            active = "[active-" in nodeid
            if (
                evidence.get("reused") is not True
                or type(evidence.get("cancelled_children")) is not int
                or evidence.get("cancelled_children") != (2 if active else 1)
                or type(evidence.get("measured_bytes_before_cancel")) is not int
                or (
                    evidence["measured_bytes_before_cancel"] <= 0
                    if active
                    else evidence["measured_bytes_before_cancel"] != 0
                )
            ):
                raise ValueError(
                    f"cancellation receipt lacks measured lifecycle evidence: {nodeid}"
                )
            workers = [
                evidence.get(name) for name in ("reused_client_worker", "reused_server_worker")
            ]
            for worker in workers:
                if (
                    not isinstance(worker, dict)
                    or type(worker.get("protocol_version")) is not int
                    or worker.get("protocol_version") != 1
                    or type(worker.get("run_index")) is not int
                    or worker.get("run_index") != 1
                    or type(worker.get("pid")) is not int
                    or worker["pid"] <= 0
                    or any(
                        not isinstance(worker.get(key), str)
                        or re.fullmatch(r"[0-9a-f]{32}", worker[key]) is None
                        for key in ("request_id", "worker_id")
                    )
                    or not isinstance(worker.get("producer"), dict)
                    or any(
                        not isinstance(worker["producer"].get(key), str)
                        or not worker["producer"][key]
                        for key in ("package_version", "python_version", "native_version")
                    )
                    or not isinstance(worker["producer"].get("library_selector"), dict)
                    or worker["producer"]["python_version"] != result.get("python")
                    or worker["producer"]["native_version"] != result.get("native_version")
                    or worker["producer"]["package_version"] != result.get("package_version")
                ):
                    raise ValueError(
                        f"cancellation receipt lacks worker producer evidence: {nodeid}"
                    )
            if (
                any(
                    workers[0][key] == workers[1][key] for key in ("pid", "request_id", "worker_id")
                )
                or type(evidence.get("reuse_bytes")) is not int
                or evidence.get("reuse_bytes", 0) <= 0
            ):
                raise ValueError(f"cancellation receipt lacks distinct measured reuse: {nodeid}")
        elif property_name == "worker_ipc":
            case = nodeid.rsplit("[", 1)[1].removesuffix("]")
            if (
                evidence.get("case") != case
                or evidence.get("workers_reaped") is not True
                or evidence.get("pipes_closed") is not True
                or (
                    case == "saturated-events"
                    and (type(evidence.get("dropped")) is not int or evidence["dropped"] <= 0)
                )
                or (case == "partial-frame-cancel" and evidence.get("cancelled") is not True)
                or (
                    case in {"wrong-identity", "oversized-header", "truncated-frame"}
                    and evidence.get("rejected") is not True
                )
            ):
                raise ValueError(f"worker IPC receipt lacks transport evidence: {nodeid}")
        else:
            active = "[active-" in nodeid
            parent, worker = evidence.get("parent_pid"), evidence.get("worker_pid")
            traffic = evidence.get("traffic_bytes")
            if (
                type(parent) is not int
                or type(worker) is not int
                or parent <= 0
                or worker <= 0
                or parent == worker
                or evidence.get("worker_signal") != 9
                or evidence.get("parent_returncode") != -9
                or evidence.get("worker_reaped") is not True
                or evidence.get("listener_released") is not True
                or evidence.get("reused") is not True
                or type(evidence.get("reuse_bytes")) is not int
                or evidence.get("reuse_bytes", 0) <= 0
                or type(traffic) is not int
                or (traffic <= 0 if active else traffic != 0)
            ):
                raise ValueError(
                    f"parent-death receipt lacks measured lifecycle evidence: {nodeid}"
                )


def _test(args) -> int:
    import pytest

    manifest = json.loads(args.manifest.read_text())
    result = {"collected": [], "reports": [], "exit_code": 1}
    try:
        result["installed_location"] = require_installed_package(
            manifest, Path(sys.prefix), args.source
        )
        from iperf3_lib.ffi.api import ffi, lib

        native = ffi.string(lib.iperf_get_iperf_version()).decode()
        if (
            native not in (args.native, f"iperf {args.native}")
            or f"{sys.version_info.major}.{sys.version_info.minor}" != args.python
        ):
            raise ValueError("installed qualification interpreter/native matrix identity differs")
        result.update(
            package_version=importlib.metadata.version("iperf3-lib"),
            python=platform.python_version(),
            executable=sys.executable,
            native_version=native,
            platform=platform.platform(),
            dependency_pins=pinned_requirements(),
        )
        plugin = _Reports()
        result["exit_code"] = int(
            pytest.main(
                [
                    "-c",
                    str(args.config),
                    "--rootdir",
                    str(args.config.parent),
                    "--confcutdir",
                    str(args.config.parent),
                    "--strict-markers",
                    "-q",
                    "--tb=short",
                    *expected_tests(),
                ],
                plugins=[plugin],
            )
        )
        result.update(collected=plugin.collected, reports=plugin.reports)
        validate_results(result)
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result.get("status") == "passed" else 1


def qualify(args) -> None:
    """Run the sealed harness in fresh isolated environments for both artifacts."""
    source, distributions = args.source.resolve(), args.distributions.resolve()
    if digest(distributions / "manifest.json") != args.manifest_sha256:
        raise ValueError("lifecycle manifest differs from the build job's retained identity")
    manifest = verify_manifest(source, distributions, args.revision)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    pins = pinned_requirements()
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith(("COV_CORE_", "COVERAGE_")):
            environment.pop(name)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    failures = []
    for entry in manifest["distributions"]:
        receipt = {
            "kind": "iperf3-lib.installed-lifecycle",
            "schema_version": 1,
            "source_revision": args.revision,
            "distribution": entry,
            "manifest_sha256": digest(distributions / "manifest.json"),
            "harness_sha256": manifest["harness_sha256"],
            "dependency_pins": pins,
            "expected_python": args.python,
            "expected_native_version": args.native,
            "status": "failed",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="iperf3-lifecycle-") as temporary:
                working = Path(temporary)
                if working.resolve().is_relative_to(source):
                    raise ValueError("installed qualification must run outside the source checkout")
                harness = working / "harness"
                harness.mkdir()
                for name in (*TESTS, *HELPERS):
                    shutil.copyfile(source / "tests" / name, harness / name)
                runner = harness / "qualify_lifecycle.py"
                shutil.copyfile(source / "scripts/qualify_lifecycle.py", runner)
                config = harness / "pytest.ini"
                config.write_text(
                    "[pytest]\nmarkers =\n    integration: native qualification\n    asyncio: async test\n"
                )
                venv = working / "venv"
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                subprocess.run(
                    ["uv", "venv", "--python", sys.executable, str(venv)],
                    check=True,
                    cwd=working,
                    env=environment,
                    timeout=60,
                )
                subprocess.run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        "--no-deps",
                        str(distributions / entry["filename"]),
                        *pins,
                    ],
                    check=True,
                    cwd=working,
                    env=environment,
                    timeout=180,
                )
                subprocess.run(
                    ["uv", "pip", "check", "--python", str(python)],
                    check=True,
                    cwd=working,
                    env=environment,
                    timeout=60,
                )
                result_file = output / f"{entry['kind']}-tests.json"
                result_file.unlink(missing_ok=True)
                command = [
                    str(python),
                    "-I",
                    str(runner),
                    "test",
                    "--manifest",
                    str(distributions / "manifest.json"),
                    "--source",
                    str(source),
                    "--config",
                    str(config),
                    "--native",
                    args.native,
                    "--python",
                    args.python,
                    "--output",
                    str(result_file),
                ]
                receipt["command"] = command
                completed = subprocess.run(
                    command,
                    cwd=harness,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=360,
                    check=False,
                )
                (output / f"{entry['kind']}.log").write_text(completed.stdout + completed.stderr)
                receipt["exit_code"] = completed.returncode
                result = json.loads(result_file.read_text())
                receipt["tests"] = result
                validate_results(result)
                if (
                    completed.returncode != 0
                    or result.get("status") != "passed"
                    or result.get("dependency_pins") != pins
                ):
                    raise ValueError("installed lifecycle subprocess failed qualification")
                receipt["status"] = "passed"
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            failures.append(entry["kind"])
        (output / f"{entry['kind']}.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"{entry['kind']}: {receipt['status']}")
    if failures:
        raise RuntimeError(f"installed lifecycle qualification failed for {', '.join(failures)}")


def main() -> int:
    """Build an identity manifest or run installed lifecycle qualification."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("manifest", "qualify"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, default=Path.cwd())
        command.add_argument("--distributions", type=Path, required=True)
        command.add_argument("--revision", required=True)
        if name == "manifest":
            command.add_argument("--github-output", type=Path)
        if name == "qualify":
            command.add_argument("--manifest-sha256", required=True)
            command.add_argument("--python", required=True)
            command.add_argument("--native", required=True)
            command.add_argument("--output", type=Path, required=True)
    test = commands.add_parser("test")
    for name in ("manifest", "source", "config", "output"):
        test.add_argument(f"--{name}", type=Path, required=True)
    test.add_argument("--python", required=True)
    test.add_argument("--native", required=True)
    args = parser.parse_args()
    if args.command == "test":
        return _test(args)
    if args.command == "manifest":
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.source, text=True
        ).strip()
        if revision != args.revision:
            raise ValueError("checkout does not match the expected source revision")
        if subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "src", "tests", "scripts"],
            cwd=args.source,
            text=True,
        ).strip():
            raise ValueError("untracked qualification inputs are not bound to the commit")
        subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                "HEAD",
                "--",
                "src",
                "tests",
                "scripts",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=args.source,
            check=True,
        )
        write_manifest(args.source, args.distributions, args.revision)
        if args.github_output:
            with args.github_output.open("a") as stream:
                stream.write(f"manifest-sha256={digest(args.distributions / 'manifest.json')}\n")
    else:
        qualify(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
