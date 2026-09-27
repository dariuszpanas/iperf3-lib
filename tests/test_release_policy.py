"""Regression coverage for release identity, retained artifacts, and workflow gates."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from iperf3_lib.config import config_to_dict
from iperf3_lib.result import VerifiedSetting
from scripts.release_artifacts import (
    check_public_documentation,
    digest,
    distribution_files,
    inspect_distributions,
    metadata_description,
    verify_manifest,
    write_manifest,
)
from scripts.smoke_release import (
    native_server,
    require_installed_package,
    round_trip_artifact,
    verify_native_artifact,
    verify_saved_result_semantics,
)
from scripts.validate_release import (
    project_version,
    validate,
    validate_revision,
    validate_tag_revision,
    write_github_outputs,
)

REVISION = "a" * 40
VERSION = "0.3.0"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", [None, "missing-fragment", "wrong-content-type"])
def test_public_documentation_checks_every_readme_page_and_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    """Validate deployed README docs links, deduplicating requests and ignoring other sites."""
    from scripts.check_docs import SITE

    readme = tmp_path / "README.md"
    readme.write_text(
        f"[Guide]({SITE}guides/artifacts.html#schema)\n"
        f"[Guide again]({SITE}guides/artifacts.html#schema)\n"
        "[External](https://example.invalid/ignored)\n"
        "[Other project](https://dariuszpanas.github.io/other/)\n"
    )
    requested: list[str] = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/plain" if failure == "wrong-content-type" else "text/html"}

    def fetch(url: str, *, timeout: int) -> Response:
        assert timeout == 30
        requested.append(url)
        return Response(
            b"<h1>Guide</h1>" if failure == "missing-fragment" else b'<h1 id="schema">Guide</h1>'
        )

    monkeypatch.setattr("scripts.release_artifacts.urlopen", fetch)
    if failure:
        with pytest.raises(
            ValueError, match="fragment target" if failure == "missing-fragment" else "not HTML"
        ):
            check_public_documentation(readme)
    else:
        check_public_documentation(readme)
        assert sorted(requested) == sorted(
            [SITE, SITE + "changelog.html", SITE + "guides/artifacts.html"]
        )


@pytest.mark.parametrize(
    "version", ["0.3.0", "1.2.3rc1", "1.2.3a0", "1.2.3b2", "1.2.3.dev1", "1.2.3rc1.dev2"]
)
def test_supported_release_versions(version: str, tmp_path: Path) -> None:
    """Accept the documented final and prerelease grammar."""
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[project]\nversion = "{version}"\n')
    assert project_version(project) == version
    validate(version, tag=f"v{version}", requested_version=version)


@pytest.mark.parametrize(
    "version",
    ["", "0.3", "v0.3.0", "01.2.3", "1.2.3+local", "1.2.3.post1", "1.2.3 rc1", "1.2.3\nmode=pypi"],
)
def test_unsupported_versions_fail(version: str) -> None:
    """Reject ambiguous versions and output-file injection."""
    with pytest.raises(ValueError):
        validate(version, tag=None, requested_version=None)


@pytest.mark.parametrize("tag,requested", [("v0.2.0", None), (None, "0.2.0")])
def test_release_inputs_must_match_metadata(tag: str | None, requested: str | None) -> None:
    """Bind tag and explicit manual assertions to the package version."""
    with pytest.raises(ValueError, match="does not match"):
        validate(VERSION, tag=tag, requested_version=requested)


def test_github_outputs_include_identity_and_safe_default(tmp_path: Path) -> None:
    """Default output is qualification only and contains the exact source identity."""
    output = tmp_path / "output"
    write_github_outputs(output, VERSION, revision=REVISION)
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "version": VERSION,
        "prerelease": "false",
        "revision": REVISION,
        "mode": "qualify",
    }


@pytest.fixture
def git_candidate(tmp_path: Path) -> tuple[Path, str, str]:
    """Create main and an unmerged branch without changing global Git settings."""
    if shutil.which("git") is None:
        pytest.skip("Git ancestry regressions run in the host quality gate; native images omit Git")
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    command = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.invalid",
    ]
    subprocess.run(
        [*command, "commit", "--allow-empty", "-m", "base"], check=True, capture_output=True
    )
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", base], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "switch", "-c", "candidate"], check=True, capture_output=True
    )
    subprocess.run(
        [*command, "commit", "--allow-empty", "-m", "candidate"], check=True, capture_output=True
    )
    candidate = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    return root, base, candidate


def test_unmerged_revision_can_be_qualified_but_not_published(git_candidate) -> None:
    """Allow harmless branch rehearsal while blocking branch publication."""
    root, _, candidate = git_candidate
    assert (
        validate_revision(
            root, expected_revision=candidate, default_branch="main", publication=False
        )
        == candidate
    )
    with pytest.raises(ValueError, match="default-branch history"):
        validate_revision(
            root, expected_revision=candidate, default_branch="main", publication=True
        )


def test_publication_requires_exact_checkout_and_default_branch_ancestry(git_candidate) -> None:
    """Require the selected commit itself, not a different already-green ancestor."""
    root, base, candidate = git_candidate
    with pytest.raises(ValueError, match="does not match"):
        validate_revision(root, expected_revision=base, default_branch="main", publication=False)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--detach", base], check=True, capture_output=True
    )
    assert (
        validate_revision(root, expected_revision=base, default_branch="main", publication=True)
        == base
    )
    with pytest.raises(ValueError, match="full Git"):
        validate_revision(
            root, expected_revision=candidate[:7], default_branch="main", publication=True
        )


def test_actual_tag_must_identify_the_qualified_commit(git_candidate) -> None:
    """Reject a matching version label that actually belongs to another commit."""
    root, base, candidate = git_candidate
    subprocess.run(["git", "-C", str(root), "tag", "v0.3.0", base], check=True)
    validate_tag_revision(root, "v0.3.0", base)
    with pytest.raises(ValueError, match="qualified revision"):
        validate_tag_revision(root, "v0.3.0", candidate)


@pytest.fixture
def artifact_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create minimal valid archives with matching embedded metadata and README."""
    directory = tmp_path / "dist"
    directory.mkdir()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<h1 id="docs">Docs</h1>')
    readme = tmp_path / "README.md"
    text = "# Package\n\n[Docs](https://dariuszpanas.github.io/iperf3-lib/#docs)\n"
    readme.write_text(text)
    metadata = (
        f"Metadata-Version: 2.4\nName: iperf3-lib\nVersion: {VERSION}\n"
        "Requires-Python: >=3.12\nRequires-Dist: cffi<3,>=2.0\n"
        "Description-Content-Type: text/markdown\n\n" + text
    )
    wheel = directory / f"iperf3_lib-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"iperf3_lib-{VERSION}.dist-info/METADATA", metadata)
        archive.writestr("iperf3_lib/py.typed", "")
    with tarfile.open(directory / f"iperf3_lib-{VERSION}.tar.gz", "w:gz") as archive:
        for name, value in {
            "PKG-INFO": metadata,
            "README.md": text,
            "pyproject.toml": f'[project]\nversion = "{VERSION}"\n',
            "src/iperf3_lib/py.typed": "",
        }.items():
            encoded = value.encode()
            member = tarfile.TarInfo(f"iperf3_lib-{VERSION}/{name}")
            member.size = len(encoded)
            archive.addfile(member, io.BytesIO(encoded))
    return directory, readme, site


def test_embedded_descriptions_and_metadata_are_checked(artifact_bundle) -> None:
    """Render the retained archives' descriptions against built documentation."""
    directory, readme, site = artifact_bundle
    inspect_distributions(directory, VERSION, readme, site)
    readme.write_text("# Different README")
    with pytest.raises(ValueError, match="embedded distribution description"):
        inspect_distributions(directory, VERSION, readme, site)


def test_distribution_set_rejects_stale_and_missing_files(artifact_bundle) -> None:
    """Prevent accidental publication of old distributions left in dist."""
    directory, _, _ = artifact_bundle
    (directory / "old.whl").write_text("stale")
    with pytest.raises(ValueError, match="expected exactly"):
        distribution_files(directory, VERSION)


def test_distribution_set_allows_only_the_uv_output_marker(artifact_bundle) -> None:
    """Accept uv's ignore marker without overlooking unexpected output files."""
    directory, _, _ = artifact_bundle
    marker = directory / ".gitignore"
    marker.write_bytes(b"*")
    assert len(distribution_files(directory, VERSION)) == 2
    marker.write_text("unexpected content")
    with pytest.raises(ValueError, match="expected exactly"):
        distribution_files(directory, VERSION)


@pytest.mark.parametrize(
    "change",
    [
        ("Name: iperf3-lib", "Name: other-project"),
        ("Version: 0.3.0", "Version: 0.2.0"),
        ("Requires-Dist: cffi<3,>=2.0", "Requires-Dist: pydantic>=2"),
        ("Requires-Python: >=3.12", "Requires-Python: >=3.10"),
        ("Description-Content-Type: text/markdown", "Description-Content-Type: text/plain"),
    ],
)
def test_distribution_policy_rejects_metadata_drift(artifact_bundle, change) -> None:
    """Reject version, identity, runtime dependency, Python, or rendering drift."""
    directory, _, _ = artifact_bundle
    with zipfile.ZipFile(directory / f"iperf3_lib-{VERSION}-py3-none-any.whl") as archive:
        text = archive.read(f"iperf3_lib-{VERSION}.dist-info/METADATA").decode()
    with pytest.raises(ValueError):
        metadata_description(text.replace(*change), VERSION)


def test_manifest_binds_hashes_sizes_version_and_revision(artifact_bundle) -> None:
    """Detect tampering in either the retained manifest or distributions."""
    directory, _, _ = artifact_bundle
    manifest = directory.parent / "release-evidence/manifest.json"
    checksum = write_manifest(directory, manifest, version=VERSION, revision=REVISION)
    verify_manifest(
        directory, manifest, version=VERSION, revision=REVISION, manifest_sha256=checksum
    )
    with pytest.raises(ValueError, match="identity"):
        verify_manifest(
            directory, manifest, version=VERSION, revision="b" * 40, manifest_sha256=checksum
        )
    artifact = distribution_files(directory, VERSION)[0]
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="changed"):
        verify_manifest(
            directory, manifest, version=VERSION, revision=REVISION, manifest_sha256=checksum
        )
    manifest.write_text("{}")
    with pytest.raises(ValueError, match="manifest hash"):
        verify_manifest(
            directory, manifest, version=VERSION, revision=REVISION, manifest_sha256=checksum
        )


def test_installed_smoke_rejects_the_current_source_interpreter() -> None:
    """Do not allow a normal pytest/source import to qualify an installed artifact."""
    with pytest.raises(ValueError, match="isolated interpreter"):
        require_installed_package("0.2.0")


def _recorded_native_run(version="3.21", profile="tcp-forward"):
    from iperf3_lib import ClientConfig
    from iperf3_lib.result import result_from_iperf_json

    raw = json.loads(
        (ROOT / "tests/fixtures/native" / version / f"{profile}-client.json").read_text()
    )
    native = raw["start"]["test_start"]
    config = ClientConfig(
        "127.0.0.1",
        duration=native["duration"],
        protocol=native["protocol"].lower(),
        parallel=native["num_streams"],
        rate=native["target_bitrate"],
        reverse=bool(native["reverse"]),
        bidirectional=bool(native["bidir"]),
    )
    result = result_from_iperf_json(raw, reporting_role="client")
    assert result.execution is not None
    timing = result.execution.timing
    timing.started_at_seconds = raw["start"]["timestamp"]["timesecs"] - 0.25
    timing.completed_at_seconds = timing.started_at_seconds + config.duration + 0.5
    timing.elapsed_seconds = config.duration + 0.5
    result.completed_at_seconds = timing.completed_at_seconds
    requested = config_to_dict(config)
    requested["protocol"] = config.protocol.value
    requested["server"] = str(config.server)
    result.execution.configuration.requested = requested
    for name in requested:
        result.execution.configuration.effective.setdefault(name, VerifiedSetting())
    result.execution.python_version = "3.14.7"
    result.execution.platform = "recorded qualification environment"
    return result, config


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
@pytest.mark.parametrize(
    "profile", ["tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp", "sctp-forward"]
)
def test_installed_artifact_helper_preserves_native_profiles(version: str, profile: str) -> None:
    """Exercise each smoke method's native fixture through the retained v1 envelope helper."""
    result, config = _recorded_native_run(version, profile)
    envelope = verify_native_artifact(result, config)
    assert envelope["schema_version"] == 1
    assert envelope["result"] == result.to_dict()
    assert envelope["result"]["execution"]["configuration"]["requested"]["rate"] == config.rate


def test_installed_artifact_helper_preserves_wall_clock_correction() -> None:
    """UTC can move backwards while independently measured monotonic elapsed stays valid."""
    result, config = _recorded_native_run()
    timing = result.execution.timing
    timing.completed_at_seconds = timing.started_at_seconds - 1
    result.completed_at_seconds = timing.completed_at_seconds
    envelope = verify_native_artifact(result, config)
    assert envelope["result"]["execution"]["timing"]["elapsed_seconds"] > 0
    assert envelope["result"]["completed_at_seconds"] == timing.completed_at_seconds


@pytest.mark.parametrize(
    "missing", ["execution", "completion", "elapsed", "request", "effective", "environment"]
)
def test_installed_artifact_helper_rejects_missing_run_provenance(missing: str) -> None:
    """A serialization round trip alone cannot qualify missing execution evidence."""
    result, config = _recorded_native_run()
    execution = result.execution
    assert execution is not None
    if missing == "execution":
        result.execution = None
    elif missing == "completion":
        execution.timing.completed_at_seconds = None
    elif missing == "elapsed":
        execution.timing.elapsed_seconds = None
    elif missing == "request":
        execution.configuration.requested = None
    elif missing == "effective":
        execution.configuration.effective["rate"].state = "unavailable"
    else:
        execution.python_version = None
    with pytest.raises(ValueError, match="native artifact"):
        verify_native_artifact(result, config)


def test_installed_artifact_round_trip_rejects_codec_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect a reader that alters recorded evidence even if its output still validates."""
    import iperf3_lib.artifacts as artifacts

    original_loads = artifacts.loads_artifact

    def _changed_loads(text):
        artifact = original_loads(text)
        artifact.extensions["org.example.changed"] = True
        return artifact

    monkeypatch.setattr(artifacts, "loads_artifact", _changed_loads)
    result, _ = _recorded_native_run()
    with pytest.raises(ValueError, match="changed recorded evidence"):
        round_trip_artifact(result)


def test_installed_saved_artifacts_keep_estimates_and_failure_freshness_separate() -> None:
    """Saved-native estimates and failures survive the codec without manufacturing freshness."""
    from iperf3_lib.artifacts import artifact_from_dict
    from iperf3_lib.exporters.prometheus import render_text

    saved = verify_saved_result_semantics()
    success = artifact_from_dict(saved["saved_native"]).result
    failure = artifact_from_dict(saved["failed_native"]).result
    assert success.execution is not None and failure.execution is not None
    assert success.execution.timing.estimated_completed_at_seconds == 105
    assert success.completed_at_seconds is None
    assert success.execution.configuration.requested is None
    assert "timestamp_seconds" not in render_text(success)
    assert success.flows[0].sender.bits_per_second is None
    assert success.flows[0].receiver.bits_per_second == 0
    assert failure.execution.status == "failed"
    assert failure.raw == {"error": "saved native failure"}
    assert failure.completed_at_seconds is None
    assert "throughput" not in render_text(failure)
    assert "iperf3_last_success_timestamp_seconds 90" in render_text(
        failure, last_success_timestamp_seconds=90
    )


@pytest.mark.parametrize("scenario", ["success", "client-error", "not-ready", "exited"])
@pytest.mark.parametrize("declared_port", [None, 5209])
def test_smoke_server_owns_fresh_process_and_bounded_cleanup(
    monkeypatch: pytest.MonkeyPatch, scenario: str, declared_port: int | None
) -> None:
    """Observe readiness without connecting and reclaim each single-use server."""
    state = {"exit": None}
    calls: list[str] = []

    def _wait(*, timeout):
        assert timeout == 5
        calls.append("wait")
        state["exit"] = 0
        return 0

    def _spawn(command, *, stdout, stderr):
        assert "--one-off" in command and "--forceflush" in command
        if declared_port is not None:
            assert command[command.index("-p") + 1] == str(declared_port)
        assert stderr == subprocess.STDOUT
        calls.append("spawn")
        if scenario == "exited":
            state["exit"] = 1
        if scenario != "not-ready":
            stdout.write(f"Server listening on {command[command.index('-p') + 1]}\n")
            stdout.flush()
        return SimpleNamespace(
            poll=lambda: state["exit"],
            wait=_wait,
            terminate=lambda: calls.append("terminate"),
            kill=lambda: calls.append("kill"),
        )

    def _unexpected_connection(*args, **kwargs):
        pytest.fail("a readiness connection would consume the one-off server")

    monkeypatch.setattr("scripts.smoke_release.subprocess.Popen", _spawn)
    monkeypatch.setattr("scripts.smoke_release.socket.create_connection", _unexpected_connection)
    moments = iter([0, 11])
    monkeypatch.setattr("scripts.smoke_release.time.monotonic", lambda: next(moments))

    def _exercise():
        with native_server("iperf3", port=declared_port) as (host, port):
            assert host == "127.0.0.1" and 0 < port < 65536
            if scenario == "client-error":
                raise RuntimeError("client failed")

    expected = {"client-error": RuntimeError, "not-ready": TimeoutError, "exited": ValueError}
    if scenario in expected:
        with pytest.raises(expected[scenario]):
            _exercise()
    else:
        _exercise()
    assert calls.count("spawn") == 1
    assert ("terminate" in calls) == (scenario in {"client-error", "not-ready"})
    assert ("wait" in calls) == (scenario != "exited")


@pytest.fixture
def release_workflow() -> str:
    """Read workflow contracts in the host gate where repository metadata exists."""
    path = ROOT / ".github/workflows/release.yml"
    if not path.is_file():
        pytest.skip("Workflow contracts run in the host gate; native images omit .github")
    return path.read_text()


def test_workflow_has_safe_dispatch_exact_revision_and_full_matrix(release_workflow: str) -> None:
    """Guard build-only defaults, the exact revision, and complete native qualification."""
    workflow = release_workflow
    assert "options: [qualify, testpypi]" in workflow
    assert "default: qualify" in workflow
    assert "ref: ${{ needs.build.outputs.revision }}" in workflow
    assert 'python: ["3.12", "3.13", "3.14"]' in workflow
    assert 'iperf: ["3.19.1", "3.21"]' in workflow
    assert "pytest -vv --cov=iperf3_lib" in workflow
    assert "for kind in wheel sdist" in workflow
    assert '"$venv/bin/python" -I /app/scripts/smoke_release.py' in workflow
    assert "run: uv build --no-sources" in workflow
    for job, mode in (("publish-testpypi", "testpypi"), ("publish-pypi", "pypi")):
        # Use a job-boundary expression so indented steps remain in the block.
        block = re.split(r"\n  [a-z][a-z-]*:\n", workflow.split(f"  {job}:\n", 1)[1])[0]
        assert "needs: [build, qualified]" in block
        assert f"needs.build.outputs.mode == '{mode}'" in block
        assert "Verify retained distribution hashes" in block
        assert "checkout@" not in block
        assert "uv build" not in block


def test_publication_verifiers_are_identical_and_detect_tampering(
    artifact_bundle, release_workflow: str
) -> None:
    """Exercise the artifact-only verifier actually embedded in every consumer job."""
    directory, _, _ = artifact_bundle
    manifest = directory.parent / "release-evidence/manifest.json"
    checksum = write_manifest(directory, manifest, version=VERSION, revision=REVISION)
    workflow = release_workflow
    snippets = re.findall(r"python - <<'PY'\n(.*?)\n          PY", workflow, re.DOTALL)
    assert len(snippets) == 4
    assert len(set(snippets)) == 1
    code = textwrap.dedent(snippets[0])
    environment = os.environ | {
        "EXPECTED_VERSION": VERSION,
        "EXPECTED_REVISION": REVISION,
        "MANIFEST_SHA256": checksum,
    }
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=directory.parent, env=environment, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    manifest_data = json.loads(manifest.read_text())
    manifest_data["revision"] = "b" * 40
    manifest.write_text(json.dumps(manifest_data))
    environment["MANIFEST_SHA256"] = digest(manifest)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=directory.parent, env=environment, capture_output=True
    )
    assert result.returncode != 0
    assert b"identity mismatch" in result.stderr
