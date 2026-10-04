"""Keep candidate-runtime evidence separate from stable publication qualification."""

from pathlib import Path

import pytest
import yaml

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
    assert set(workflow.get("on", workflow.get(True))) == {"pull_request", "workflow_dispatch"}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert "environment" not in job
        for step in job["steps"]:
            assert "pypi-publish" not in step.get("uses", "")
            if "actions/checkout@" in step.get("uses", ""):
                assert step["with"]["persist-credentials"] is False


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


def test_preview_does_not_expand_stable_release_matrix(preview_workflow: dict) -> None:
    """Stable publication continues to require final interpreters for its declared matrix."""
    from scripts.verify_release_qualification import PYTHON_VERSIONS, _python

    assert PYTHON_VERSIONS == ("3.12", "3.13", "3.14")
    with pytest.raises(ValueError):
        _python("3.15.0rc3", "3.15")
    for name in ("ci.yml", "release.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
        matrices = [
            job["strategy"]["matrix"] for job in workflow["jobs"].values() if "strategy" in job
        ]
        assert len(matrices) == 1
        assert matrices[0]["python"] == list(PYTHON_VERSIONS)
