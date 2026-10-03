"""Qualify one retained wheel/sdist pair with isolated installed lifecycle tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
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
)
CASE_GROUPS = {
    "cancellation": CASES,
    "worker_lifetime": CASES,
    "worker_ipc": IPC_CASES,
    "async_plan": PLAN_CASES,
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
}
HARNESS = (
    "scripts/qualify_lifecycle.py",
    *(f"tests/{name}" for name in TESTS),
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
    statuses, stop, admitted = {
        "active-cancel-tcp": (["completed", "cancelled", "not_run"], "cancelled", 2),
        "pause-cancel-tcp": (["completed", "not_run", "not_run"], "cancelled", 1),
        "active-deadline-udp": (["completed", "timed_out", "not_run"], "timeout", 2),
        "completed-tcp": (["completed", "completed", "completed"], None, 3),
        "stop-on-error-udp": (["completed", "failed", "not_run"], "stop_on_error", 2),
    }[case]
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
        if any(workers[0][key] == workers[1][key] for key in ("pid", "request_id", "worker_id")):
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
                for name in TESTS:
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
