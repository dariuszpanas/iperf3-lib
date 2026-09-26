"""Check fixed-context Docker staging without invoking Docker or host bind mounts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import docker_validate as staging

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_and_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a deterministic Git selection around real source and staging files."""
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "Dockerfile").write_bytes(b"FROM scratch\n")
    (source / "payload.bin").write_bytes(b"dirty\x00bytes\r\n")
    root = tmp_path / "fixed-context"
    selected = staging.SourceState("a" * 40, " M payload.bin\0", ("Dockerfile", "payload.bin"))
    monkeypatch.setattr(staging, "source_state", lambda _: selected)
    staging.prepare_root(root, source)
    return source, root, selected


def test_snapshot_preserves_dirty_bytes_and_replaces_only_context(source_and_root) -> None:
    """Keep source bytes and hashes while preserving unrelated owned-root files."""
    source, root, _ = source_and_root
    (root / "keep-user-note.txt").write_text("preserve")
    with staging.exclusive_staging(root):
        manifest = staging.stage_source(source, root)
    assert (root / "context/payload.bin").read_bytes() == b"dirty\x00bytes\r\n"
    assert manifest["dirty"] and manifest["head"] == "a" * 40
    assert manifest["files"]["payload.bin"] == {
        "sha256": hashlib.sha256(b"dirty\x00bytes\r\n").hexdigest(),
        "size": 13,
    }
    assert json.loads((root / "source-manifest.json").read_text()) == manifest
    (root / "context/stale.txt").write_text("stale")
    (root / "context/old/nested").mkdir(parents=True)
    (root / "context/old/nested/obsolete").write_text("old")
    with staging.exclusive_staging(root):
        staging.stage_source(source, root)
    assert not (root / "context/stale.txt").exists()
    assert not (root / "context/old").exists()
    assert (root / "keep-user-note.txt").read_text() == "preserve"
    assert not (root / "context/source-manifest.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        ".git/config",
        ".vault/notes",
        ".env.local",
        ".aws/credentials",
        "credentials.json",
        "private.pem",
        ".cache/file",
        "src/__pycache__/module.pyc",
        "dist/archive",
        "secrets/token",
    ],
)
def test_sensitive_and_local_files_are_excluded_even_if_tracked(source_and_root, monkeypatch, name):
    """Git selection cannot override mandatory local-state and credential exclusions."""
    source, root, selected = source_and_root
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("do not send")
    monkeypatch.setattr(
        staging,
        "source_state",
        lambda _: staging.SourceState(selected.head, selected.status, selected.files + (name,)),
    )
    with staging.exclusive_staging(root):
        manifest = staging.stage_source(source, root)
    assert name in manifest["excluded"]
    assert not (root / "context" / name).exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "C:/drive", "file:stream", "path\\escape", "line\nbreak"]
)
def test_unsafe_git_paths_are_rejected(name: str) -> None:
    """Reject traversal, drive paths, alternate streams and ambiguous separators."""
    with pytest.raises(ValueError, match="unsafe Git source path"):
        staging.excluded_source(name)


def test_nonempty_unowned_directory_is_untouched(tmp_path: Path) -> None:
    """Do not turn an arbitrary user directory into a disposable Docker context."""
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "existing"
    root.mkdir()
    (root / "context").mkdir()
    (root / "context/valuable.txt").write_text("keep")
    with pytest.raises(ValueError, match="nonempty unowned"):
        staging.prepare_root(root, source)
    assert (root / "context/valuable.txt").read_text() == "keep"
    assert not (root / staging.MARKER).exists()
    (root / "source-manifest.json").write_text("user-owned receipt")
    with pytest.raises(ValueError, match="ownership marker"):
        staging.stage_source(source, root)
    assert (root / "source-manifest.json").read_text() == "user-owned receipt"


def test_ownership_and_cleanup_boundaries_are_enforced(source_and_root, tmp_path) -> None:
    """Refuse alternate cleanup targets and damaged ownership markers."""
    source, root, _ = source_and_root
    with pytest.raises(ValueError, match="owned context"):
        staging.clear_context(root, source)
    marker = root / staging.MARKER
    marker.write_text(json.dumps({"owner": staging.OWNER, "root": str(tmp_path)}))
    with pytest.raises(ValueError, match="ownership marker"):
        staging.clear_context(root, root / "context")
    assert (source / "payload.bin").is_file()


def test_lock_excludes_other_processes_and_releases_after_failure(source_and_root) -> None:
    """The lock spans processes and is automatically released on an exception."""
    _, root, _ = source_and_root
    code = (
        "from pathlib import Path; from scripts.docker_validate import exclusive_staging; "
        "import sys\nwith exclusive_staging(Path(sys.argv[1])): pass\n"
    )
    with pytest.raises(RuntimeError, match="caller failed"), staging.exclusive_staging(root):
        contender = subprocess.run(
            [sys.executable, "-c", code, str(root)],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )
        assert contender.returncode != 0 and "already in use" in contender.stderr
        raise RuntimeError("caller failed")
    with staging.exclusive_staging(root):
        pass


def test_changed_source_cannot_leave_a_valid_manifest(source_and_root, monkeypatch) -> None:
    """Fail closed if any selected source changes while its snapshot is copied."""
    source, root, _ = source_and_root
    (root / "source-manifest.json").write_text("old receipt")
    original = staging._read_regular
    reads = 0

    def changed_read(directory, relative):
        nonlocal reads
        reads += 1
        if reads == 3:
            (source / "payload.bin").write_bytes(b"changed mid-copy")
        return original(directory, relative)

    monkeypatch.setattr(staging, "_read_regular", changed_read)
    with staging.exclusive_staging(root), pytest.raises(ValueError, match="content changed"):
        staging.stage_source(source, root)
    assert not (root / "source-manifest.json").exists()


def test_git_selection_change_aborts_snapshot(source_and_root, monkeypatch) -> None:
    """Branch, index and untracked-file changes invalidate a mixed snapshot."""
    source, root, selected = source_and_root
    states = iter([selected, staging.SourceState("b" * 40, "", selected.files)])
    monkeypatch.setattr(staging, "source_state", lambda _: next(states))
    with staging.exclusive_staging(root), pytest.raises(ValueError, match="selection changed"):
        staging.stage_source(source, root)


def test_copy_failure_prevents_docker_execution(source_and_root, monkeypatch) -> None:
    """An unreadable source never falls back to the previous context or starts Docker."""
    source, root, _ = source_and_root
    monkeypatch.setattr(staging, "repository_root", lambda _: source)
    monkeypatch.setattr(
        staging, "_read_regular", lambda *_: (_ for _ in ()).throw(OSError("cannot read"))
    )
    monkeypatch.setattr(
        staging.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Docker ran after copy failure"),
    )
    assert staging.main(["build", "--source", str(source), "--staging-root", str(root)]) == 1


@pytest.mark.parametrize("place", ["source", "context", "root"])
def test_symbolic_links_never_escape_staging(source_and_root, tmp_path, place) -> None:
    """Reject source links, context links and a redirected staging root."""
    source, root, _ = source_and_root
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve outside bytes")
    link = (
        source / "payload.bin"
        if place == "source"
        else root / "context/escape"
        if place == "context"
        else tmp_path / "redirected-root"
    )
    link.parent.mkdir(parents=True, exist_ok=True)
    if place == "source":
        link.unlink()
    try:
        link.symlink_to(root if place == "root" else outside, target_is_directory=place == "root")
    except OSError:
        pytest.skip(
            "host does not permit creating symbolic links; reparse rejection has a deterministic regression"
        )
    with pytest.raises(ValueError, match="symlink/reparse"):
        if place == "root":
            staging.prepare_root(link, source)
        else:
            with staging.exclusive_staging(root):
                staging.stage_source(source, root)
    assert outside.read_text() == "preserve outside bytes"


def test_windows_reparse_attribute_is_rejected_without_following(tmp_path, monkeypatch) -> None:
    """Detect Windows junction/reparse attributes even when is_symlink would be false."""
    target = tmp_path / "junction"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="symlink/reparse"):
        staging._reject_links(target)


def test_root_priority_and_worktree_independent_default(tmp_path, monkeypatch) -> None:
    """Resolve CLI, environment, shared repository setting and per-user cache in order."""
    selected = tmp_path / "configured"
    monkeypatch.delenv("IPERF3_DOCKER_STAGING_ROOT", raising=False)
    monkeypatch.setattr(
        staging.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=os.fsencode(str(selected))),
    )
    assert staging.staging_root(tmp_path) == selected
    monkeypatch.setenv("IPERF3_DOCKER_STAGING_ROOT", str(tmp_path / "environment"))
    assert staging.staging_root(tmp_path) == tmp_path / "environment"
    assert staging.staging_root(tmp_path, tmp_path / "explicit") == tmp_path / "explicit"
    monkeypatch.delenv("IPERF3_DOCKER_STAGING_ROOT")
    monkeypatch.setattr(
        staging.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"")
    )
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    expected = tmp_path / "cache/iperf3-lib/docker-validation"
    assert staging.staging_root(tmp_path / "worktree-a") == expected
    assert staging.staging_root(tmp_path / "worktree-b") == expected
    with pytest.raises(ValueError, match="absolute directory"):
        staging.staging_root(tmp_path, Path("relative"))


@pytest.mark.parametrize("operation", ["test", "shell"])
def test_docker_commands_use_fixed_paths_and_hold_the_lock(
    source_and_root, monkeypatch, operation
) -> None:
    """Keep both Docker cwd/arguments under the fixed root throughout build and test."""
    source, root, _ = source_and_root
    monkeypatch.setattr(staging, "repository_root", lambda _: source)
    commands = []

    def run(command, *, cwd, check):
        assert cwd == root and check is True
        assert str(source) not in " ".join(command)
        assert "--mount" not in command and "--volume" not in command and "-v" not in command
        with pytest.raises(ValueError, match="already in use"), staging.exclusive_staging(root):
            pass
        commands.append(command)

    monkeypatch.setattr(staging.subprocess, "run", run)
    args = argparse.Namespace(
        command=operation,
        source=source,
        staging_root=root,
        image="example:test",
        python_base="python:3.14-slim",
        iperf_version="3.21",
        dockerfile="Dockerfile",
        build_arg=["EXAMPLE=value with spaces"],
    )
    staging.validate_docker(args)
    assert commands[0] == [
        "docker",
        "build",
        "--file",
        str(root / "context/Dockerfile"),
        "--build-arg",
        "PYTHON_BASE=python:3.14-slim",
        "--build-arg",
        "IPERF3_VERSION=3.21",
        "--tag",
        "example:test",
        "--build-arg",
        "EXAMPLE=value with spaces",
        str(root / "context"),
    ]
    expected_test = [
        "docker",
        "run",
        "--rm",
        "example:test",
        "pytest",
        "-vv",
        "--cov=iperf3_lib",
        "--cov-report=term-missing",
    ]
    assert commands[1] == (
        expected_test
        if operation == "test"
        else ["docker", "run", "--rm", "-it", "example:test", "bash"]
    )


def test_real_git_selection_includes_dirty_and_untracked_but_respects_ignores(tmp_path) -> None:
    """Exercise actual Git selection, shared config, ignored staging, and dirty deletion."""
    if shutil.which("git") is None:
        pytest.skip(
            "real Git staging contracts run in the required host gate; native image omits Git"
        )
    source = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    (source / ".gitignore").write_text("ignored/\n.vault/\n")
    (source / "tracked").write_text("base")
    (source / "deleted").write_text("remove from context")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Staging Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    (source / "tracked").write_text("dirty")
    (source / "deleted").unlink()
    (source / "new-file").write_text("new")
    (source / "ignored").mkdir()
    (source / "ignored/secret").write_text("private")
    root = source / ".vault/docker-validation"
    subprocess.run(
        ["git", "-C", str(source), "config", "--local", staging.CONFIG_KEY, str(root)], check=True
    )
    assert staging.staging_root(source) == root
    staging.prepare_root(root, source)
    with staging.exclusive_staging(root):
        manifest = staging.stage_source(source, root)
    assert set(manifest["files"]) == {".gitignore", "tracked", "new-file"}
    assert (root / "context/tracked").read_text() == "dirty"
    assert (root / "context/new-file").read_text() == "new"
    assert manifest["dirty"]
    with pytest.raises(ValueError, match="Git-ignored"):
        staging.prepare_root(source / "not-ignored", source)
