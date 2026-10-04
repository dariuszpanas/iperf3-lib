"""Keep preview provenance tied to the exact candidate and native endpoint."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from scripts import python_preview as preview


@pytest.fixture
def runtime() -> dict:
    """Describe the intended candidate environment without loading libiperf."""
    return {
        "python": "3.15.0rc3",
        "python_version_info": [3, 15, 0, "candidate", 3],
        "sys_version": "3.15.0rc3 (preview)",
        "executable": "/opt/preview-venv/bin/python",
        "prefix": "/opt/preview-venv",
        "base_prefix": "/opt/preview-python/cpython-3.15.0rc3-linux-x86_64-gnu",
        "implementation": "CPython",
        "system": "Linux",
        "machine": "x86_64",
        "platform": "Linux-preview",
        "gil_disabled": False,
        "uv_version": "uv 0.12.23 (build 2026-10-03)",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python", "3.15.0rc2"),
        ("python_version_info", [3, 15, 0, "candidate", 2]),
        ("python_version_info", [3, 15, 0, "final", 0]),
        ("implementation", "PyPy"),
        ("gil_disabled", True),
        ("system", "Windows"),
        ("machine", "aarch64"),
        ("prefix", "/opt/venv"),
        ("executable", "/usr/local/bin/python"),
        ("base_prefix", "/opt/preview-venv"),
        ("base_prefix", "/usr/local"),
        ("uv_version", "uv 0.12.17"),
        ("uv_version", "uv 0.12.230"),
    ],
)
def test_wrong_runtime_fails_before_native_import(monkeypatch, runtime, tmp_path, field, value):
    """A base-image fallback or wrong candidate cannot produce a preview receipt."""
    runtime[field] = value
    monkeypatch.setattr(preview, "_runtime", lambda: runtime)

    def unexpected_native():
        pytest.fail("native must not load before interpreter validation")

    monkeypatch.setattr(preview, "_native_version", unexpected_native)
    with pytest.raises(ValueError):
        preview.record_runtime("a" * 40, "3.21", tmp_path / "missing-lock")


def test_receipt_preserves_actual_evidence(monkeypatch, runtime, tmp_path):
    """Record getter, lock bytes and full interpreter identity after exact checks."""
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"frozen-lock\n")
    original = deepcopy(runtime)
    monkeypatch.setattr(preview, "_runtime", lambda: runtime)
    monkeypatch.setattr(preview, "_native_version", lambda: "iperf 3.21")
    monkeypatch.setattr(preview.importlib.metadata, "version", lambda _: "0.3.0")
    monkeypatch.setattr(preview, "_dependencies", lambda: {"cffi": "2.1.1"})
    receipt = preview.record_runtime("a" * 40, "3.21", lock)
    assert receipt["native_version"] == "iperf 3.21"
    assert receipt["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert receipt["source_revision"] == "a" * 40
    assert receipt["dependencies"] == {"cffi": "2.1.1"}
    assert {key: receipt[key] for key in runtime} == original
    assert runtime == original


@pytest.mark.parametrize("native", ["iperf 3.19.1", "3.20", ""])
def test_native_getter_must_match(monkeypatch, runtime, tmp_path, native):
    """A selected image tag cannot substitute for observed native identity."""
    monkeypatch.setattr(preview, "_runtime", lambda: runtime)
    monkeypatch.setattr(preview, "_native_version", lambda: native)
    with pytest.raises(ValueError, match="native getter"):
        preview.record_runtime("a" * 40, "3.21", tmp_path / "missing-lock")


def test_cli_writes_only_a_valid_receipt(monkeypatch, tmp_path):
    """A rejected runtime must not leave success-shaped provenance behind."""
    output = tmp_path / "evidence/runtime.json"
    output.parent.mkdir()
    output.write_text('{"status": "preview", "source_revision": "stale"}')

    def reject(*_):
        raise ValueError("wrong runtime")

    monkeypatch.setattr(preview, "record_runtime", reject)
    arguments = ["--revision", "a" * 40, "--native", "3.21", "--output", str(output)]
    assert preview.main(arguments) == 1
    assert not output.exists()
    monkeypatch.setattr(preview, "record_runtime", lambda *_: {"status": "preview"})
    assert preview.main(arguments) == 0
    assert json.loads(output.read_text()) == {"status": "preview"}


@pytest.mark.parametrize("revision", ["", "a" * 39, "A" * 40, "main"])
def test_revision_must_be_exact_before_runtime_probe(monkeypatch, tmp_path, revision):
    """Do not attach observed runtime evidence to an ambiguous source label."""

    def unexpected_probe():
        pytest.fail("revision must be validated first")

    monkeypatch.setattr(preview, "_runtime", unexpected_probe)
    with pytest.raises(ValueError, match="source revision"):
        preview.record_runtime(revision, "3.21", tmp_path / "missing-lock")


def test_local_staging_can_build_preview_layer(tmp_path):
    """The existing staging CLI already passes the required two-layer build inputs."""
    from scripts.docker_validate import build_command

    (tmp_path / "context").mkdir()
    (tmp_path / "context/Dockerfile.preview").write_text("ARG NATIVE_IMAGE\nFROM ${NATIVE_IMAGE}\n")
    command = build_command(
        tmp_path,
        image="iperf3-lib-preview:local",
        python_base="python:3.14-slim",
        iperf_version="3.21",
        dockerfile="Dockerfile.preview",
        build_args=["NATIVE_IMAGE=iperf3-lib-preview-base:local"],
    )
    assert command[command.index("--file") + 1] == str(tmp_path / "context/Dockerfile.preview")
    assert "NATIVE_IMAGE=iperf3-lib-preview-base:local" in command
    assert command[-1] == str(tmp_path / "context")
