"""Validate a release version and bind qualification to one Git revision."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = ROOT / "pyproject.toml"
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:(?:a|b|rc)(?:0|[1-9]\d*))?(?:\.dev(?:0|[1-9]\d*))?\Z"
)
PRERELEASE_PATTERN = re.compile(r"(?:a|b|rc|\.dev)\d")


def project_version(project_file: Path = PROJECT_FILE) -> str:
    """Read a supported final, alpha, beta, release-candidate, or dev version."""
    with project_file.open("rb") as source:
        version = tomllib.load(source).get("project", {}).get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "project.version must be MAJOR.MINOR.PATCH with optional a/b/rc and .dev suffix"
        )
    return version


def validate(version: str, *, tag: str | None, requested_version: str | None) -> None:
    """Reject unsupported versions and mismatched tag or manual inputs."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("unsupported release version")
    if tag and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match project version v{version}")
    if requested_version and requested_version != version:
        raise ValueError(
            f"requested version {requested_version!r} does not match project version {version}"
        )


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def validate_revision(
    root: Path, *, expected_revision: str, default_branch: str, publication: bool
) -> str:
    """Bind HEAD to an event SHA and require default-branch ancestry for publication."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise ValueError("expected revision must be a full Git object SHA")
    head = _git(root, "rev-parse", "HEAD^{commit}")
    expected = _git(root, "rev-parse", "--verify", f"{expected_revision}^{{commit}}")
    if head != expected:
        raise ValueError("checkout does not match the requested release revision")
    if publication:
        _git(root, "check-ref-format", f"refs/heads/{default_branch}")
        branch = _git(
            root, "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}^{{commit}}"
        )
        result = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", head, branch], check=False
        )
        if result.returncode != 0:
            raise ValueError(
                "publication revision must belong to the remote default-branch history"
            )
    return head


def write_github_outputs(
    output_path: Path, version: str, *, revision: str = "", mode: str = "qualify"
) -> None:
    """Append the validated version, immutable revision, and publication mode."""
    values = {
        "version": version,
        "prerelease": str(bool(PRERELEASE_PATTERN.search(version))).lower(),
        "revision": revision,
        "mode": mode,
    }
    with output_path.open("a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def validate_tag_revision(root: Path, tag: str, revision: str) -> None:
    """Require the actual release tag to resolve to the qualified commit."""
    if _git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != revision:
        raise ValueError("release tag does not identify the qualified revision")


def main() -> int:
    """Validate release metadata and optionally emit GitHub job outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--requested-version")
    parser.add_argument("--mode", choices=("qualify", "testpypi", "pypi"), default="qualify")
    parser.add_argument("--expected-revision")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    output_path = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    try:
        version = project_version()
        validate(version, tag=args.tag, requested_version=args.requested_version)
        if args.mode == "pypi" and not args.tag:
            raise ValueError("PyPI publication requires an exact release tag")
        if args.mode != "qualify" and not args.expected_revision:
            raise ValueError("publication requires an explicit revision")
        revision = (
            validate_revision(
                ROOT,
                expected_revision=args.expected_revision,
                default_branch=args.default_branch,
                publication=args.mode != "qualify",
            )
            if args.expected_revision
            else ""
        )
        if args.mode == "pypi":
            validate_tag_revision(ROOT, args.tag, revision)
        if output_path:
            write_github_outputs(output_path, version, revision=revision, mode=args.mode)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    print(f"validated release version {version}; mode={args.mode}; revision={revision or 'local'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
