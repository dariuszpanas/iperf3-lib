"""Stage local Docker validation at one owned path shared by every worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

OWNER = "iperf3-lib.docker-validation.v1"
MARKER = ".iperf3-lib-docker-validation.json"
CONFIG_KEY = "iperf3-lib.dockerStagingRoot"
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".vault",
        ".venv",
        "venv",
        ".env",
        ".envrc",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".git-credentials",
        ".secrets",
        "secrets",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "__pycache__",
        ".tox",
        ".nox",
        ".idea",
        "build",
        "dist",
        "site",
        "htmlcov",
        "coverage.xml",
    }
)


def _git(source: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source), *arguments])


def repository_root(source: Path) -> Path:
    """Resolve the selected checkout without changing the process working directory."""
    root = Path(os.fsdecode(_git(source, "rev-parse", "--show-toplevel")).strip()).absolute()
    _reject_links(root)
    return root.resolve(strict=True)


def staging_root(source: Path, explicit: Path | None = None) -> Path:
    """Choose an explicit, environment, shared Git-config, or stable user-cache path."""
    selected = explicit or os.environ.get("IPERF3_DOCKER_STAGING_ROOT")
    if not selected:
        setting = subprocess.run(
            ["git", "-C", str(source), "config", "--get", CONFIG_KEY],
            capture_output=True,
            check=False,
        )
        if setting.returncode not in (0, 1):
            raise ValueError("could not read the repository Docker staging setting")
        selected = os.fsdecode(setting.stdout).strip()
    if not selected:
        if sys.platform == "win32":
            cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        elif sys.platform == "darwin":
            cache = Path.home() / "Library/Caches"
        else:
            cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        selected = cache / "iperf3-lib/docker-validation"
    path = Path(selected).expanduser()
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("Docker staging root must be a dedicated absolute directory")
    _reject_links(path)
    return path.resolve()


def _reject_links(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"symlink/reparse paths are not allowed: {item}")


def _within(root: Path, path: Path) -> Path:
    _reject_links(root)
    _reject_links(path)
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path is outside the dedicated staging directory: {path}")
    return resolved


def _ownership(root: Path) -> None:
    _reject_links(root)
    marker = _within(root, root / MARKER)
    expected = {"owner": OWNER, "root": str(root)}
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != expected:
        raise ValueError(f"Docker staging directory has no matching ownership marker: {root}")


def prepare_root(root: Path, source: Path) -> None:
    """Claim an empty dedicated directory; preserve and reject unowned existing contents."""
    _reject_links(root)
    if root == source or source.is_relative_to(root):
        raise ValueError("staging root cannot be the source checkout or one of its parents")
    if root.is_relative_to(source):
        ignored = subprocess.run(
            ["git", "-C", str(source), "check-ignore", "--quiet", "--", str(root)], check=False
        )
        if ignored.returncode != 0:
            raise ValueError("a staging root inside the checkout must be Git-ignored")
    root.mkdir(parents=True, exist_ok=True)
    _reject_links(root)
    if not (root / MARKER).exists():
        if any(root.iterdir()):
            raise ValueError(f"refusing to claim a nonempty unowned staging directory: {root}")
        try:
            with (root / MARKER).open("x", encoding="utf-8") as output:
                json.dump({"owner": OWNER, "root": str(root)}, output)
        except FileExistsError:
            pass  # A simultaneous initializer must still pass ownership validation.
    _ownership(root)


@contextmanager
def exclusive_staging(root: Path) -> Iterator[None]:
    """Hold an operating-system lock through staging, Docker build and optional testing."""
    _ownership(root)
    lock_path = _within(root, root / "staging.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b" ")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(f"Docker staging directory is already in use: {root}") from error
        try:
            lock.seek(0)
            lock.write(json.dumps({"pid": os.getpid()}).encode())
            lock.truncate()
            lock.flush()
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def clear_context(root: Path, context: Path) -> None:
    """Remove only the verified owned context, refusing links and special files."""
    _ownership(root)
    if context != root / "context" or _within(root, context) != root / "context":
        raise ValueError("only the owned context directory can be replaced")
    if not context.exists():
        return
    if not context.is_dir():
        raise ValueError("the owned context path is not a directory")
    files: list[Path] = []
    directories: list[Path] = []
    for current, child_dirs, child_files in os.walk(context, topdown=False, followlinks=False):
        for name in child_files:
            path = _within(context, Path(current) / name)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"refusing to remove a special context file: {path}")
            files.append(path)
        for name in child_dirs:
            directories.append(_within(context, Path(current) / name))
    for path in files:
        _within(context, path).unlink()
    for path in directories:
        _within(context, path).rmdir()
    _within(root, context).rmdir()


def _relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise ValueError(f"unsafe Git source path: {name!r}")
    return path


def excluded_source(name: str) -> bool:
    """Keep repository internals, local state and common credential files out of Docker."""
    parts = _relative_path(name).parts
    return any(
        part.lower() in EXCLUDED_PARTS
        or part.lower().startswith((".env.", ".coverage", "iperf-3."))
        or part.lower().endswith((".egg-info", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx"))
        or part.lower() in {"id_rsa", "id_ed25519", "credentials.json", "credentials"}
        for part in parts
    )


@dataclass(frozen=True)
class SourceState:
    """Git-selected source identity, including dirty and nonignored untracked paths."""

    head: str
    status: str
    files: tuple[str, ...]


def source_state(source: Path) -> SourceState:
    """Enumerate the checkout with Git without entering ignored directories."""
    selected = _git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    deleted = set(_git(source, "ls-files", "-z", "--deleted").split(b"\0"))
    files = sorted(
        {os.fsdecode(name) for name in selected.split(b"\0") if name and name not in deleted}
    )
    for name in files:
        _relative_path(name)
    return SourceState(
        _git(source, "rev-parse", "HEAD").decode().strip(),
        os.fsdecode(_git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all")),
        tuple(files),
    )


def _read_regular(source: Path, relative: str) -> tuple[bytes, os.stat_result]:
    path = _within(source, source.joinpath(*_relative_path(relative).parts))
    expected = path.lstat()
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError(f"source must be a regular file (submodules are unsupported): {path}")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"source changed while opening: {path}")
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"source changed while reading: {path}")
    return data, before


def stage_source(source: Path, root: Path) -> dict:
    """Replace the locked context with a verified snapshot and record its file hashes."""
    _ownership(root)
    context = root / "context"
    initial = source_state(source)
    previous = _within(root, root / "source-manifest.json")
    previous.unlink(missing_ok=True)
    clear_context(root, context)
    context.mkdir()
    records = {}
    excluded = []
    for relative in initial.files:
        if excluded_source(relative):
            excluded.append(relative)
            continue
        data, info = _read_regular(source, relative)
        target = _within(context, context.joinpath(*_relative_path(relative).parts))
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_links(target.parent)
        with target.open("xb") as output:
            output.write(data)
        os.chmod(target, stat.S_IMODE(info.st_mode))
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        records[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if source_state(source) != initial:
        raise ValueError(
            "Git source selection changed during staging; retry from a stable checkout"
        )
    for relative, expected in records.items():
        current, _ = _read_regular(source, relative)
        if hashlib.sha256(current).hexdigest() != expected["sha256"]:
            raise ValueError(f"source content changed during staging: {relative}")
        staged, _ = _read_regular(context, relative)
        if hashlib.sha256(staged).hexdigest() != expected["sha256"]:
            raise ValueError(f"staged content differs from the selected source: {relative}")
    manifest = {
        "schema_version": 1,
        "source": str(source),
        "head": initial.head,
        "dirty": bool(initial.status),
        "git_status": initial.status,
        "files": records,
        "excluded": excluded,
        "context": str(context),
    }
    pending = _within(root, root / "source-manifest.pending")
    destination = _within(root, root / "source-manifest.json")
    with pending.open("w", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(pending, destination)
    return manifest


def build_command(
    root: Path,
    *,
    image: str,
    python_base: str,
    iperf_version: str,
    dockerfile: str,
    build_args: list[str] | None = None,
) -> list[str]:
    """Build flat Docker arguments containing only the fixed staging context paths."""
    if not image or image.startswith("-") or any(character.isspace() for character in image):
        raise ValueError("image must be a nonempty Docker image reference")
    relative = _relative_path(dockerfile)
    context = root / "context"
    recipe = _within(context, context.joinpath(*relative.parts))
    if not recipe.is_file():
        raise ValueError(f"Dockerfile is not in the staged snapshot: {dockerfile}")
    extra = []
    for argument in build_args or []:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\r\n]*", argument):
            raise ValueError("additional build arguments must use NAME=value")
        if argument.split("=", 1)[0] in {"PYTHON_BASE", "IPERF3_VERSION"}:
            raise ValueError("use --python-base or --iperf-version for native compatibility inputs")
        extra.extend(["--build-arg", argument])
    return [
        "docker",
        "build",
        "--file",
        str(recipe),
        "--build-arg",
        f"PYTHON_BASE={python_base}",
        "--build-arg",
        f"IPERF3_VERSION={iperf_version}",
        "--tag",
        image,
        *extra,
        str(context),
    ]


def validate_docker(args: argparse.Namespace) -> None:
    """Stage and optionally build/test while holding the shared staging lock."""
    source = repository_root(args.source)
    root = staging_root(source, args.staging_root)
    prepare_root(root, source)
    with exclusive_staging(root):
        manifest = stage_source(source, root)
        print(
            f"Staged {len(manifest['files'])} files at {root / 'context'}; source={manifest['head']}; dirty={manifest['dirty']}",
            flush=True,
        )
        if args.command == "stage":
            return
        subprocess.run(
            build_command(
                root,
                image=args.image,
                python_base=args.python_base,
                iperf_version=args.iperf_version,
                dockerfile=args.dockerfile,
                build_args=args.build_arg,
            ),
            cwd=root,
            check=True,
        )
        if args.command in ("test", "shell"):
            command = (
                ["docker", "run", "--rm", "-it", args.image, "bash"]
                if args.command == "shell"
                else [
                    "docker",
                    "run",
                    "--rm",
                    args.image,
                    "pytest",
                    "-vv",
                    "--cov=iperf3_lib",
                    "--cov-report=term-missing",
                ]
            )
            subprocess.run(
                command,
                cwd=root,
                check=True,
            )


def main(argv: list[str] | None = None) -> int:
    """Run local Docker validation through a fixed, exclusively owned context."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "build", "test", "shell"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--image", default="iperf3-lib-test:local")
    parser.add_argument("--python-base", default="python:3.12-slim")
    parser.add_argument("--iperf-version", default="3.22")
    parser.add_argument("--dockerfile", default="Dockerfile")
    parser.add_argument("--build-arg", action="append", default=[])
    try:
        validate_docker(parser.parse_args(argv))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Docker validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
