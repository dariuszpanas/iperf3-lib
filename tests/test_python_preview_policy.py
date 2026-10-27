"""Keep candidate-runtime evidence separate from stable publication qualification."""

from pathlib import Path

import pytest
import yaml

from scripts import python_preview as preview

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview_workflow() -> dict:
    """Read workflow policy where GitHub files are available in the host checkout."""
    path = ROOT / ".github/workflows/python-preview.yml"
    if not path.exists():
        pytest.skip("Workflow policy runs in the host gate; native images omit .github")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_preview_has_no_publication_authority(preview_workflow: dict) -> None:
    """A candidate-runtime run cannot gain publishing credentials or tag triggers."""
    workflow = preview_workflow
    assert workflow["permissions"] == {"contents": "read"}
    # PyYAML's YAML 1.1 resolver treats the GitHub `on` key as a boolean.
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert "environment" not in job
        for step in job["steps"]:
            assert "pypi-publish" not in step.get("uses", "")
            if "actions/checkout@" in step.get("uses", ""):
                assert step["with"]["persist-credentials"] is False


def test_preview_toolchain_and_retained_image_identity_agree(preview_workflow: dict) -> None:
    """A Docker dependency bump must update runtime checks and retained provenance."""
    dockerfile = (ROOT / "Dockerfile.preview").read_text(encoding="utf-8")
    assert f"FROM {preview.UV_IMAGE} AS preview-uv" in dockerfile
    assert preview.UV_IMAGE.startswith(f"ghcr.io/astral-sh/uv:{preview.UV_VERSION}@sha256:")
    assert preview.PYTHON_DOWNLOAD["key"] in dockerfile
    runs = "\n".join(step.get("run", "") for step in preview_workflow["jobs"]["native"]["steps"])
    assert f"docker pull {preview.UV_IMAGE}\n" in runs
    assert f"docker image inspect {preview.UV_IMAGE} > preview-evidence/uv-image.json" in runs


def test_preview_binds_both_formats_to_one_build_and_explicit_interpreter(
    preview_workflow: dict,
) -> None:
    """Fail if a matrix cell silently selects stable Python or rebuilds its artifacts."""
    jobs = preview_workflow["jobs"]
    native = jobs["native"]
    assert native["needs"] == "build"
    assert native["strategy"]["matrix"] == {"iperf": ["3.19.1", "3.22"]}
    steps = native["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    combined = "\n".join(runs)
    assert "uv build" not in combined
    # The layer must see the base image loaded into the daemon by Buildx.
    assert "docker build --builder default --load -f Dockerfile.preview" in combined
    assert "/opt/preview-venv/bin/python -m pytest -m 'not integration'" in combined
    assert combined.index("not integration") < combined.index("pytest -m integration")
    assert "/opt/preview-venv/bin/python -I /app/scripts/qualify_lifecycle.py qualify" in combined
    assert '--manifest-sha256 "$MANIFEST_SHA256"' in combined
    assert '--python 3.15 --native "$IPERF3_VERSION"' in combined
    assert 'uv venv --python /opt/preview-venv/bin/python "$venv"' in combined
    assert "for kind in wheel sdist" in combined
    assert 'sys.version_info == (3, 15, 0, \\"candidate\\", 3)' in combined
    lifecycle = next(
        step for step in steps if "qualify_lifecycle.py qualify" in step.get("run", "")
    )
    assert lifecycle["env"]["MANIFEST_SHA256"] == "${{ needs.build.outputs.manifest-sha256 }}"
    assert lifecycle["env"]["SOURCE_REVISION"] == "${{ needs.build.outputs.revision }}"
    assert jobs["preview"]["needs"] == ["build", "native"]
    assert jobs["preview"]["if"] == "always()"


def test_stable_release_matrix_requires_final_python_315(preview_workflow: dict) -> None:
    """Stable publication continues to require final interpreters for its declared matrix."""
    from scripts.verify_release_qualification import PYTHON_VERSIONS, _python

    assert PYTHON_VERSIONS == ("3.12", "3.13", "3.14", "3.15")
    assert _python("3.15.0", "3.15") == "3.15.0"
    with pytest.raises(ValueError):
        _python("3.15.0rc3", "3.15")
    for name in ("ci.yml", "release.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
        matrices = [
            job["strategy"]["matrix"] for job in workflow["jobs"].values() if "strategy" in job
        ]
        assert len(matrices) == 1
        assert matrices[0]["python"] == list(PYTHON_VERSIONS)
        assert matrices[0]["include"] == [
            {"python": "3.15", "python_base": "python:3.14-slim", "python_version": "3.15.0"}
        ]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_stable_matrix_verifies_final_runtime_before_native_tests(
    preview_workflow: dict, name: str
) -> None:
    """A fallback interpreter or candidate cannot silently satisfy the final matrix."""
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    job = next(job for job in workflow["jobs"].values() if "strategy" in job)
    steps = job["steps"]
    build = next(step for step in steps if "docker/build-push-action@" in step.get("uses", ""))
    assert build["with"]["build-args"].splitlines() == [
        "PYTHON_BASE=${{ matrix.python_base || format('python:{0}-slim', matrix.python) }}",
        "PYTHON_RUNTIME=${{ matrix.python_version || 'python' }}",
        "IPERF3_VERSION=${{ matrix.iperf }}",
    ]
    runtime = next(
        step for step in steps if step["name"] == "Verify final standard-GIL CPython runtime"
    )
    assert runtime["env"] == {"EXPECTED_PYTHON": "${{ matrix.python }}"}
    command = runtime["run"]
    assert "-e EXPECTED_PYTHON" in command
    assert 'sys.implementation.name == "cpython"' in command
    assert 'sys.version_info[:2])) == os.environ["EXPECTED_PYTHON"]' in command
    assert 'sys.version_info.releaselevel == "final"' in command
    assert 'not sysconfig.get_config_var("Py_GIL_DISABLED")' in command
    full_suite = next(
        step for step in steps if "pytest -vv --cov=iperf3_lib" in step.get("run", "")
    )
    assert steps.index(runtime) < steps.index(full_suite)
