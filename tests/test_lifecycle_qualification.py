"""Fail-closed identity and evidence checks for installed lifecycle qualification."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import zipfile
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
        reports.extend(
            {
                "nodeid": nodeid,
                "phase": phase,
                "outcome": "passed",
                "properties": {key: json.dumps(evidence)},
            }
            for phase in ("setup", "call", "teardown")
        )
    return {"exit_code": 0, "collected": qualification.expected_tests(), "reports": reports}


def test_lifecycle_results_require_every_expected_case_and_native_receipt():
    """The exact positive full selection can qualify."""
    qualification.validate_results(_passed_results())


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
    expected = _passed_results()
    for filename, (test, property_name) in qualification.TESTS.items():
        receipts = [
            report["properties"][property_name]
            for report in expected["reports"]
            if report["phase"] == "call" and report["nodeid"].startswith(filename)
        ]
        (tmp_path / filename).write_text(
            textwrap.dedent(f"""
            import pytest
            @pytest.mark.parametrize("index", range(5), ids={qualification.CASES!r})
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
        import pytest
        runner = runpy.run_path(sys.argv[1])
        plugin = runner['_Reports']()
        status = pytest.main(['-c', 'pytest.ini', '-q', *runner['expected_tests']()], plugins=[plugin])
        result = {'exit_code': int(status), 'collected': plugin.collected, 'reports': plugin.reports}
        try:
            runner['validate_results'](result)
        except ValueError:
            print('QUALIFICATION_REJECTED')
        else:
            print('QUALIFICATION_PASSED')
    """)
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program, qualification.__file__],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert ("QUALIFICATION_REJECTED" if skip else "QUALIFICATION_PASSED") in completed.stdout
