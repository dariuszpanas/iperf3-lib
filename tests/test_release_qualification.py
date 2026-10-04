"""Offline release aggregation over complete semantic receipts and sealed real archives."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import qualify_lifecycle as lifecycle
from scripts import release_artifacts
from scripts import verify_release_qualification as aggregate

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


def remap(value, *, python, native):
    """Adapt explicitly synthetic producer identities while retaining all semantic evidence."""
    if isinstance(value, dict):
        result = {key: remap(item, python=python, native=native) for key, item in value.items()}
        if "report_json" in result and "report_sha256" in result:
            result["report_sha256"] = hashlib.sha256(result["report_json"].encode()).hexdigest()
        return result
    if isinstance(value, list):
        return [remap(item, python=python, native=native) for item in value]
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(remap(parsed, python=python, native=native))
        return value.replace("3.14.2", python).replace("3.21", native)
    return value


def save(path, value):
    """Retain compact strict JSON for the fixture evidence inventory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


@pytest.fixture(scope="module")
def retained_matrix(tmp_path_factory):
    """Create all 24 receipts using existing full semantic factories, with no native load."""
    from test_installed_smoke import installed_receipt
    from test_lifecycle_qualification import _passed_results
    from test_live_event_evidence import live_receipt

    root = tmp_path_factory.mktemp("release-aggregate")
    source, distributions = root / "source", root / "dist"
    source.mkdir()
    distributions.mkdir()
    version = aggregate.project_version(ROOT / "pyproject.toml")
    (source / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    package = source / "src/iperf3_lib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Synthetic sealed package bytes."""\n')
    (package / "py.typed").write_bytes(b"")
    for name in lifecycle.HARNESS:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    with zipfile.ZipFile(distributions / f"iperf3_lib-{version}-py3-none-any.whl", "w") as archive:
        for path in package.iterdir():
            archive.write(path, f"iperf3_lib/{path.name}")
    with tarfile.open(distributions / f"iperf3_lib-{version}.tar.gz", "w:gz") as archive:
        for path in package.iterdir():
            archive.add(path, f"iperf3_lib-{version}/src/iperf3_lib/{path.name}")
    release_manifest = root / "release-evidence/manifest.json"
    lifecycle_manifest = root / "release-evidence/lifecycle-manifest.json"
    release_hash = release_artifacts.write_manifest(
        distributions, release_manifest, version=version, revision=REVISION
    )
    manifest = lifecycle.write_manifest(
        source, distributions, REVISION, manifest_path=lifecycle_manifest
    )
    manifest_hash = lifecycle.digest(lifecycle_manifest)
    with pytest.MonkeyPatch.context() as monkeypatch:
        smoke_base = installed_receipt.__wrapped__(monkeypatch)[0]
    lifecycle_base = _passed_results()
    smoke_root, lifecycle_root = root / "retained/smoke", root / "retained/lifecycle"
    entries = {entry["kind"]: entry for entry in manifest["distributions"]}
    for python, native in aggregate.EXPECTED_MATRIX:
        patch_version = f"{python}.2"
        smoke_dir = smoke_root / f"native-evidence-py{python}-iperf{native}"
        lifecycle_dir = lifecycle_root / f"lifecycle-evidence-py{python}-iperf{native}"
        inner_base = copy.deepcopy(lifecycle_base)
        for report in inner_base["reports"]:
            if "live_events" in report["properties"]:
                case = report["nodeid"].rsplit("[", 1)[1].removesuffix("]")
                report["properties"]["live_events"] = json.dumps(
                    live_receipt(case, native_version=f"iperf {native}")
                )
        inner_base = remap(inner_base, python=patch_version, native=native)
        for kind in aggregate.DISTRIBUTION_KINDS:
            artifact = entries[kind]
            smoke = remap(smoke_base, python=patch_version, native=native)
            smoke.update(
                python=f"{patch_version} (synthetic isolated interpreter)",
                source_revision=REVISION,
                artifact={key: artifact[key] for key in ("filename", "sha256")},
                installed_location=f"/tmp/installed-{kind}/lib/python{python}/site-packages/iperf3_lib/__init__.py",
            )
            save(smoke_dir / f"{kind}.json", smoke)
            working = f"/tmp/iperf3-lifecycle-synthetic-{python}-{native}-{kind}"
            pins = aggregate.locked_pins(source, patch_version)
            inner = {
                **inner_base,
                "status": "passed",
                "platform": "Linux-synthetic",
                "dependency_pins": pins,
                "executable": f"{working}/venv/bin/python",
                "installed_location": f"{working}/venv/lib/python{python}/site-packages/iperf3_lib/__init__.py",
            }
            outer = {
                "kind": "iperf3-lib.installed-lifecycle",
                "schema_version": 1,
                "source_revision": REVISION,
                "distribution": artifact,
                "manifest_sha256": manifest_hash,
                "harness_sha256": manifest["harness_sha256"],
                "dependency_pins": pins,
                "expected_python": python,
                "expected_native_version": native,
                "status": "passed",
                "exit_code": 0,
                "tests": inner,
                "command": [
                    inner["executable"],
                    "-I",
                    f"{working}/harness/qualify_lifecycle.py",
                    "test",
                    "--manifest",
                    "/release-evidence/lifecycle-manifest.json",
                    "--source",
                    "/app",
                    "--config",
                    f"{working}/harness/pytest.ini",
                    "--native",
                    native,
                    "--python",
                    python,
                    "--output",
                    f"/evidence/{kind}-tests.json",
                ],
            }
            save(lifecycle_dir / f"{kind}.json", outer)
            save(lifecycle_dir / f"{kind}-tests.json", inner)
            (lifecycle_dir / f"{kind}.log").write_text(
                "Synthetic retained output; phase receipts supply semantic proof.\n"
            )
    return {
        "source": source,
        "distributions": distributions,
        "release_manifest": release_manifest,
        "release_manifest_sha256": release_hash,
        "lifecycle_manifest": lifecycle_manifest,
        "lifecycle_manifest_sha256": manifest_hash,
        "revision": REVISION,
        "version": version,
        "smoke_root": smoke_root,
        "lifecycle_root": lifecycle_root,
    }


@pytest.fixture
def matrix(retained_matrix):
    """Restore only deliberately mutated temporary fixture files after each check."""
    originals = {}
    directories = []

    def change(path, value=None):
        originals.setdefault(path, path.read_bytes() if path.exists() else None)
        if value is None:
            path.unlink()
        elif isinstance(value, bytes):
            path.write_bytes(value)
        elif isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            save(path, value)

    def directory(path):
        path.mkdir()
        directories.append(path)

    args = dict(retained_matrix)
    cell = "py3.12-iperf3.19.1"
    yield SimpleNamespace(
        args=args,
        change=change,
        directory=directory,
        smoke=args["smoke_root"] / f"native-evidence-{cell}",
        lifecycle=args["lifecycle_root"] / f"lifecycle-evidence-{cell}",
    )
    for path, value in originals.items():
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(value)
    for path in directories:
        path.rmdir()


def test_complete_release_matrix_validates_semantics_and_records_retained_hashes(
    matrix, monkeypatch
):
    """Every retained case passes existing validators without native/environment inspection."""

    def forbidden(*args, **kwargs):
        pytest.fail("offline aggregate attempted native loading or a subprocess")

    monkeypatch.setattr("cffi.FFI.dlopen", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    index = aggregate.verify_qualification(**matrix.args)
    assert index["status"] == "passed"
    assert index["totals"] == {
        "smoke_receipts": 12,
        "lifecycle_receipts": 12,
        "lifecycle_cases": 588,
        "lifecycle_phases": 1764,
    }
    assert len(index["lifecycle_selection"]) == 49
    assert len(index["receipts"]) == 12
    first = index["receipts"][0]
    assert first["smoke_sha256"] == lifecycle.digest(matrix.smoke / "wheel.json")
    assert first["lifecycle_tests_sha256"] == lifecycle.digest(
        matrix.lifecycle / "wheel-tests.json"
    )
    assert first["lifecycle_log_sha256"] == lifecycle.digest(matrix.lifecycle / "wheel.log")


@pytest.mark.parametrize(
    "fault",
    [
        "missing-smoke",
        "missing-lifecycle",
        "missing-tests",
        "missing-log",
        "extra-file",
        "extra-cell",
        "swapped-kind",
        "swapped-cell",
    ],
)
def test_matrix_inventory_rejects_missing_extra_or_swapped_receipts(matrix, fault):
    """The expected matrix is policy, never inferred from whatever files arrived."""
    if fault.startswith("missing-"):
        target = {
            "missing-smoke": matrix.smoke / "wheel.json",
            "missing-lifecycle": matrix.lifecycle / "wheel.json",
            "missing-tests": matrix.lifecycle / "wheel-tests.json",
            "missing-log": matrix.lifecycle / "wheel.log",
        }[fault]
        matrix.change(target)
    elif fault == "extra-file":
        matrix.change(matrix.smoke / "duplicate.json", (matrix.smoke / "wheel.json").read_bytes())
    elif fault == "extra-cell":
        matrix.directory(matrix.args["smoke_root"] / "native-evidence-py3.15-iperf3.21")
    elif fault == "swapped-kind":
        matrix.change(matrix.smoke / "wheel.json", (matrix.smoke / "sdist.json").read_bytes())
    else:
        matrix.change(
            matrix.smoke / "wheel.json",
            (
                matrix.args["smoke_root"] / "native-evidence-py3.13-iperf3.19.1/wheel.json"
            ).read_bytes(),
        )
    with pytest.raises(ValueError):
        aggregate.verify_qualification(**matrix.args)


@pytest.mark.parametrize(
    "fault",
    [
        "release-hash",
        "lifecycle-hash",
        "wrong-revision",
        "wrong-version",
        "archive",
        "harness",
        "package",
        "manifest-bool",
    ],
)
def test_independent_manifest_hashes_and_source_bytes_cannot_be_substituted(matrix, fault):
    """Neither self-consistent receipt rewrites nor source drift can change sealed authority."""
    if fault in {"release-hash", "lifecycle-hash"}:
        matrix.args[f"{fault.split('-')[0]}_manifest_sha256"] = "0" * 64
    elif fault == "wrong-revision":
        matrix.args["revision"] = "b" * 40
    elif fault == "wrong-version":
        matrix.args["version"] = "0.0.0"
    elif fault == "archive":
        matrix.change(next(matrix.args["distributions"].glob("*.whl")), b"changed")
    elif fault in {"harness", "package"}:
        path = matrix.args["source"] / (
            lifecycle.HARNESS[0] if fault == "harness" else "src/iperf3_lib/__init__.py"
        )
        matrix.change(path, "changed\n")
    else:
        path = matrix.args["release_manifest"]
        data = json.loads(path.read_text())
        data["schema_version"] = True
        matrix.change(path, data)
        matrix.args["release_manifest_sha256"] = lifecycle.digest(path)
    with pytest.raises(ValueError):
        aggregate.verify_qualification(**matrix.args)


@pytest.mark.parametrize(
    "fault",
    [
        "revision",
        "artifact",
        "native",
        "python",
        "package",
        "source-import",
        "schema",
        "failed",
        "semantic-case",
        "duplicate-key",
        "nonfinite",
    ],
)
def test_smoke_receipts_are_bound_to_artifacts_and_semantically_validated(matrix, fault):
    """A v2 receipt must be both exact-cell evidence and a complete native smoke record."""
    path = matrix.smoke / "wheel.json"
    receipt = json.loads(path.read_text())
    if fault == "revision":
        receipt["source_revision"] = "b" * 40
    elif fault == "artifact":
        receipt["artifact"]["sha256"] = "0" * 64
    elif fault == "native":
        receipt["native_version"] = "3.21"
    elif fault == "python":
        receipt["python"] = "3.13.2"
    elif fault == "package":
        receipt["package_version"] = "0.0.0"
    elif fault == "source-import":
        receipt["installed_location"] = "/app/src/iperf3_lib/__init__.py"
    elif fault == "schema":
        receipt["schema_version"] = 1
    elif fault == "failed":
        receipt["ok"] = False
    elif fault == "semantic-case":
        receipt["cases"].pop()
    elif fault == "duplicate-key":
        matrix.change(path, path.read_text().replace('"ok": true', '"ok": false,"ok": true', 1))
    else:
        matrix.change(path, path.read_text()[:-1] + ',"unexpected":NaN}')
    if fault not in {"duplicate-key", "nonfinite"}:
        matrix.change(path, receipt)
    with pytest.raises(ValueError):
        aggregate.verify_qualification(**matrix.args)


@pytest.mark.parametrize(
    "fault",
    [
        "outer-kind",
        "outer-schema",
        "outer-status",
        "outer-exit",
        "revision",
        "manifest",
        "harness",
        "distribution",
        "cell",
        "inner-copy",
        "inner-status",
        "inner-exit",
        "python",
        "native",
        "package",
        "dependency-mismatch",
        "resealed-dependencies",
        "source-import",
        "command-isolation",
        "skipped",
        "missing-phase",
        "duplicate-case",
        "semantic-proof",
        "unsupported-sctp",
    ],
)
def test_lifecycle_receipts_require_exact_ownership_identity_and_all_native_phases(matrix, fault):
    """Outer pass flags cannot hide inner skips, altered producers or invalid measured evidence."""
    path, inner_path = matrix.lifecycle / "wheel.json", matrix.lifecycle / "wheel-tests.json"
    outer = json.loads(path.read_text())
    inner = outer["tests"]
    if fault == "outer-kind":
        outer["kind"] = "other"
    elif fault == "outer-schema":
        outer["schema_version"] = True
    elif fault == "outer-status":
        outer["status"] = "failed"
    elif fault == "outer-exit":
        outer["exit_code"] = False
    elif fault == "revision":
        outer["source_revision"] = "b" * 40
    elif fault == "manifest":
        outer["manifest_sha256"] = "0" * 64
    elif fault == "harness":
        outer["harness_sha256"][lifecycle.HARNESS[0]] = "0" * 64
    elif fault == "distribution":
        outer["distribution"]["sha256"] = "0" * 64
    elif fault == "cell":
        outer["expected_python"] = "3.13"
    elif fault == "inner-copy":
        inner["python"] = "3.12.9"
    elif fault == "inner-status":
        inner["status"] = "failed"
    elif fault == "inner-exit":
        inner["exit_code"] = 1
    elif fault == "python":
        inner["python"] = "3.13.2"
    elif fault == "native":
        inner["native_version"] = "3.21"
    elif fault == "package":
        inner["package_version"] = "0.0.0"
    elif fault == "dependency-mismatch":
        inner["dependency_pins"].pop()
    elif fault == "resealed-dependencies":
        inner["dependency_pins"] = outer["dependency_pins"] = ["cffi==0.0.0"]
    elif fault == "source-import":
        inner["installed_location"] = "/app/src/iperf3_lib/__init__.py"
    elif fault == "command-isolation":
        outer["command"][1] = "-E"
    elif fault == "skipped":
        inner["reports"][1]["outcome"] = "skipped"
    elif fault == "missing-phase":
        inner["reports"].pop()
    elif fault == "duplicate-case":
        inner["collected"][-1] = inner["collected"][0]
    elif fault == "semantic-proof":
        inner["reports"][1]["properties"] = {}
    else:
        report = next(
            report
            for report in inner["reports"]
            if report["phase"] == "call"
            and "test_live_events_roundtrip[sctp-forward]" in report["nodeid"]
        )
        evidence = json.loads(report["properties"]["live_events"])
        evidence["support"].update(status="kernel_unsupported", errno=93)
        evidence["endpoints"] = {}
        evidence["cleanup"]["workers"] = []
        report["properties"]["live_events"] = json.dumps(evidence)
    matrix.change(path, outer)
    if fault != "inner-copy":
        matrix.change(inner_path, inner)
    with pytest.raises(ValueError):
        aggregate.verify_qualification(**matrix.args)


def test_target_dependency_closure_is_host_independent(retained_matrix):
    """Linux CPython3.12 needs typing extensions; the later supported minors do not."""
    early = aggregate.locked_pins(retained_matrix["source"], "3.12.2")
    later = aggregate.locked_pins(retained_matrix["source"], "3.13.2")
    assert any(pin.startswith("typing-extensions==") for pin in early)
    assert not any(pin.startswith("typing-extensions==") for pin in later)
    assert not any(pin.startswith("colorama==") for pin in early + later)


def test_cli_writes_no_success_index_after_failed_aggregation(matrix, tmp_path):
    """A failed rerun cannot leave an earlier index looking like its successful output."""
    output = tmp_path / "qualification.json"
    output.write_text('{"status":"passed"}')
    matrix.change(matrix.smoke / "wheel.json")
    arguments = [
        part
        for key, value in matrix.args.items()
        for part in (f"--{key.replace('_', '-')}", str(value))
    ]
    assert aggregate.main([*arguments, "--output", str(output)]) == 1
    assert not output.exists()


def test_cli_writes_complete_success_index(matrix, tmp_path):
    """The release command publishes its index only after validating the whole matrix."""
    output = tmp_path / "release-evidence" / "qualification.json"
    arguments = [
        part
        for key, value in matrix.args.items()
        for part in (f"--{key.replace('_', '-')}", str(value))
    ]
    assert aggregate.main([*arguments, "--output", str(output)]) == 0
    index = json.loads(output.read_text())
    assert index["status"] == "passed"
    assert index["source_revision"] == matrix.args["revision"]
    assert index["release_manifest_sha256"] == matrix.args["release_manifest_sha256"]
    assert index["lifecycle_manifest_sha256"] == matrix.args["lifecycle_manifest_sha256"]
    assert index["totals"] == {
        "smoke_receipts": 12,
        "lifecycle_receipts": 12,
        "lifecycle_cases": 588,
        "lifecycle_phases": 1764,
    }
    assert len(index["receipts"]) == 12
    assert list(output.parent.iterdir()) == [output]
