"""Prevent documentation routing from hiding changes or failed required jobs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci_scope import Change, detect_scope, docs_only, parse_changes, verify_gate

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/changelog.md",
        "docs/guides/a guide\nwith newline.md",
        "development/releasing.md",
        "development/design/diagram.svg",
        "docs/stylesheets/extra.css",
        "overrides/main.html",
        "overrides/images/logo.png",
    ],
)
def test_only_known_documentation_paths_can_skip_native(path: str) -> None:
    """Recognize prose and rendering assets without using commit labels or titles."""
    assert docs_only((Change(path, "100644", "100644", "M"),))


@pytest.mark.parametrize(
    "path",
    [
        "src/iperf3_lib/client.py",
        "src/README.md",
        "tests/test_client.py",
        "tests/README.md",
        "scripts/ci_scope.py",
        "examples/demo.py",
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        "Makefile",
        "zensical.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        ".yaga/change-policy.toml",
        ".gitignore",
        ".gitattributes",
        "LICENSE",
        "new-file",
        "docs/plugin.py",
        "docs/data.json",
        "docs/run.js",
        "overrides/config.toml",
        "docs/../src/README.md",
        "/docs/README.md",
        "docs\\README.md",
    ],
)
def test_unknown_and_execution_relevant_paths_require_native(path: str) -> None:
    """An allowlist must fail closed for mixed changes and future project files."""
    changes = (Change("docs/index.md", "100644", "100644", "M"),)
    assert not docs_only((*changes, Change(path, "100644", "100644", "M")))


@pytest.mark.parametrize(
    ("old", "new", "status"),
    [("100644", "120000", "T"), ("100644", "100755", "M"), ("160000", "160000", "M")],
)
def test_documentation_names_do_not_hide_mode_or_type_changes(
    old: str, new: str, status: str
) -> None:
    """A symlink, executable or submodule remains outside the documentation route."""
    assert not docs_only((Change("docs/index.md", old, new, status),))


def test_empty_or_large_mixed_changes_do_not_skip_native() -> None:
    """Do not use all(empty), API pagination or a truncated documentation prefix."""
    assert not docs_only(())
    changes = tuple(Change(f"docs/{number}.md", "000000", "100644", "A") for number in range(400))
    assert docs_only(changes)
    assert not docs_only((*changes, Change("src/deleted.py", "100644", "000000", "D")))


@pytest.mark.parametrize("raw", [b"invalid", b"bad\0path\0", b":100644 100644 x y R100\0a\0b\0"])
def test_malformed_diff_records_fail(raw: bytes) -> None:
    """Unexpected diff formats cannot become a documentation-only decision."""
    with pytest.raises(ValueError):
        parse_changes(raw)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=CI Test",
            "-c",
            "user.email=ci@example.invalid",
            *arguments,
        ],
        text=True,
    ).strip()


def _commit(root: Path, changes: dict[str, str | None]) -> str:
    for name, text in changes.items():
        path = root / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test: update fixture")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, str]:
    """Use a real isolated history so comparisons and rename handling are exercised."""
    if shutil.which("git") is None:
        pytest.skip("Git diff contracts run in the host gate; native images omit Git")
    _git(tmp_path, "init", "-q", "-b", "main")
    base = _commit(tmp_path, {"docs/index.md": "original\n", "src/runtime.py": "print(1)\n"})
    return tmp_path, base


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_real_doc_add_modify_and_delete_can_skip(history, event: str) -> None:
    """A complete exact-SHA comparison includes removed documentation too."""
    root, base = history
    head = _commit(root, {"docs/index.md": None, "docs/new.md": "new\n"})
    result = detect_scope(root, event=event, base=base, head=head)
    assert not result.native_required
    assert {change.path for change in result.changes} == {"docs/index.md", "docs/new.md"}
    assert {change.status for change in result.changes} == {"A", "D"}
    assert result.head == head and result.comparison_base == base


def test_runtime_renamed_to_docs_retains_the_deleted_runtime_path(history) -> None:
    """Rename heuristics must not conceal a native-relevant old path."""
    root, base = history
    _git(root, "mv", "src/runtime.py", "docs/runtime.md")
    _git(root, "commit", "-qm", "test: rename runtime")
    head = _git(root, "rev-parse", "HEAD")
    result = detect_scope(root, event="pull_request", base=base, head=head)
    assert result.native_required
    assert {change.path for change in result.changes} == {"src/runtime.py", "docs/runtime.md"}


def test_pr_uses_merge_base_but_push_covers_every_commit(history) -> None:
    """Unrelated base-branch changes do not contaminate the PR's own file set."""
    root, base = history
    _git(root, "switch", "-qc", "docs")
    _commit(root, {"docs/index.md": "first\n"})
    head = _commit(root, {"docs/second.md": "second\n"})
    _git(root, "switch", "-q", "main")
    advanced_base = _commit(root, {"src/runtime.py": "print(2)\n"})
    _git(root, "switch", "-q", "docs")
    result = detect_scope(root, event="pull_request", base=advanced_base, head=head)
    assert not result.native_required and result.comparison_base == base
    assert len(result.changes) == 2
    assert not detect_scope(root, event="push", base=base, head=head).native_required
    assert detect_scope(root, event="push", base=advanced_base, head=head).native_required


@pytest.mark.parametrize("base", ["", "0" * 40, "f" * 40, "main; echo skip", "HEAD~1"])
def test_unusable_base_requires_native(history, base: str) -> None:
    """A new push, absent object or non-SHA input cannot skip the matrix."""
    root, head = history
    assert detect_scope(root, event="push", base=base, head=head).native_required


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "unknown"])
def test_manual_scheduled_and_unknown_events_always_require_native(history, event: str) -> None:
    """Full maintenance qualification never depends on a documentation diff."""
    root, base = history
    head = _commit(root, {"docs/index.md": "docs only\n"})
    assert detect_scope(root, event=event, base=base, head=head).native_required


@pytest.mark.parametrize("head", ["HEAD", "0" * 40, "a" * 40, "abc; echo skip"])
def test_wrong_or_nonliteral_head_fails(history, head: str) -> None:
    """Bind the comparison to the actual event checkout before emitting any output."""
    root, base = history
    with pytest.raises(ValueError):
        detect_scope(root, event="pull_request", base=base, head=head)


def _results(native_required: str = "true", event: str = "pull_request") -> dict[str, str]:
    return {
        "SCOPE_RESULT": "success",
        "LINT_RESULT": "success",
        "NATIVE_REQUIRED": native_required,
        "NATIVE_RESULT": "success" if native_required == "true" else "skipped",
        "EVENT_NAME": event,
        "COMMIT_RESULT": "success" if event == "pull_request" else "skipped",
    }


@pytest.mark.parametrize("native", ["true", "false"])
@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_aggregate_accepts_only_the_selected_success_path(native: str, event: str) -> None:
    """Both docs and native routes produce one reliable required CI result."""
    verify_gate(_results(native, event))


@pytest.mark.parametrize("key", ["SCOPE_RESULT", "LINT_RESULT", "COMMIT_RESULT"])
@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", ""])
@pytest.mark.parametrize("native", ["true", "false"])
def test_aggregate_rejects_failed_or_missing_required_jobs(
    key: str, outcome: str, native: str
) -> None:
    """A skipped matrix cannot mask another failed PR prerequisite."""
    with pytest.raises(ValueError):
        verify_gate(_results(native) | {key: outcome})


@pytest.mark.parametrize(
    ("required", "outcome"),
    [
        ("true", "skipped"),
        ("true", "failure"),
        ("true", "cancelled"),
        ("false", "success"),
        ("false", "failure"),
        ("false", "cancelled"),
        ("", "skipped"),
        ("False", "skipped"),
    ],
)
def test_aggregate_rejects_unexpected_matrix_results(required: str, outcome: str) -> None:
    """Only an explicit docs decision permits the intentional skipped result."""
    with pytest.raises(ValueError):
        verify_gate(_results() | {"NATIVE_REQUIRED": required, "NATIVE_RESULT": outcome})


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "unknown"])
def test_aggregate_cannot_skip_manual_or_scheduled_native(event: str) -> None:
    """An erroneous classifier output cannot weaken non-diff native gates."""
    verify_gate(_results("true", event))
    with pytest.raises(ValueError):
        verify_gate(_results("false", event))


def test_ci_wiring_keeps_an_unconditional_required_aggregate() -> None:
    """Bind the real workflow to the tested routing and aggregate contracts."""
    path = ROOT / ".github/workflows/ci.yml"
    if not path.exists():
        pytest.skip("Workflow contracts run in the host gate; native images omit .github")
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    for event in ("pull_request", "push"):
        assert "paths" not in events[event] and "paths-ignore" not in events[event]
    jobs = workflow["jobs"]
    scope = jobs["scope"]
    checkout = scope["steps"][0]["with"]
    assert checkout["fetch-depth"] == 0 and checkout["persist-credentials"] is False
    assert checkout["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    selection = next(step for step in scope["steps"] if step.get("id") == "scope")
    assert "python scripts/ci_scope.py detect" in selection["run"]
    assert (
        selection["env"]["BASE_SHA"]
        == "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    matrix = jobs["docker-matrix-tests"]
    assert set(matrix["needs"]) == {"scope", "lint-and-types"}
    assert matrix["if"] == "needs.scope.outputs.native-required == 'true'"
    gate = jobs["ci"]
    assert gate["name"] == "ci" and gate["if"] == "${{ always() }}"
    assert set(gate["needs"]) == {"scope", "lint-and-types", "commit-policy", "docker-matrix-tests"}
    validation = gate["steps"][-1]
    assert validation["run"] == "python scripts/ci_scope.py gate"
    assert validation["env"] == {
        "SCOPE_RESULT": "${{ needs.scope.result }}",
        "NATIVE_REQUIRED": "${{ needs.scope.outputs.native-required }}",
        "LINT_RESULT": "${{ needs.lint-and-types.result }}",
        "NATIVE_RESULT": "${{ needs.docker-matrix-tests.result }}",
        "COMMIT_RESULT": "${{ needs.commit-policy.result }}",
        "EVENT_NAME": "${{ github.event_name }}",
    }
    assert "if" not in jobs["lint-and-types"]
    assert jobs["commit-policy"]["if"] == "github.event_name == 'pull_request'"
