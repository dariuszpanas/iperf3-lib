"""Validate the canonical uv pin and the installed uv executable."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = PROJECT_ROOT / ".tool-versions"
STABLE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
LITERAL_WORKFLOW_VERSION_PATTERN = re.compile(r'^\s+version:\s*["\']?\d+\.\d+\.\d+', re.MULTILINE)


def pinned_uv_version(pin_file: Path = PIN_FILE) -> str:
    """Return the single stable uv version recorded in ``pin_file``."""
    entries = [
        line.split()
        for raw_line in pin_file.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]
    if len(entries) != 1 or len(entries[0]) != 2 or entries[0][0] != "uv":
        raise ValueError(f"{pin_file} must contain exactly 'uv <version>'")

    version = entries[0][1]
    if STABLE_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(f"{pin_file} must pin a stable uv X.Y.Z release")
    return version


def validate_repository_wiring(version: str, project_root: Path = PROJECT_ROOT) -> None:
    """Ensure every repository-owned uv consumer derives from the canonical pin."""
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    expected_from = "FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv"
    first_from = dockerfile.find("FROM ")
    uv_argument = dockerfile.find("ARG UV_VERSION=pin-required")
    if uv_argument == -1 or first_from == -1 or uv_argument > first_from:
        raise ValueError("Dockerfile must declare the UV_VERSION argument before FROM")
    if expected_from not in dockerfile:
        raise ValueError("Dockerfile must derive the uv image tag from ${UV_VERSION}")

    makefile = (project_root / "Makefile").read_text(encoding="utf-8")
    if "docker-build: check-uv-pin" not in makefile:
        raise ValueError("docker-build must validate the canonical uv pin")
    if "--build-arg UV_VERSION=$(UV_VERSION)" not in makefile:
        raise ValueError("Makefile must pass the canonical uv pin to Docker")

    with (project_root / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)
    uv_settings = project.get("tool", {}).get("uv", {})
    if isinstance(uv_settings, dict) and "required-version" in uv_settings:
        raise ValueError("pyproject.toml must not duplicate the exact uv execution pin")

    workflow_paths = (
        project_root / ".github" / "workflows" / "ci.yml",
        project_root / ".github" / "workflows" / "release.yml",
    )
    for workflow_path in workflow_paths:
        workflow = workflow_path.read_text(encoding="utf-8")
        if LITERAL_WORKFLOW_VERSION_PATTERN.search(workflow):
            raise ValueError(f"{workflow_path} must not contain a literal uv version")
        if 'version-file: ".tool-versions"' not in workflow:
            raise ValueError(f"{workflow_path} must read the canonical uv pin")

    ci_workflow = workflow_paths[0].read_text(encoding="utf-8")
    if "UV_VERSION=${{ needs.lint-and-types.outputs.uv_version }}" not in ci_workflow:
        raise ValueError("CI must pass setup-uv's resolved version to Docker")

    release_workflow = workflow_paths[1].read_text(encoding="utf-8")
    if "version: ${{ needs.build.outputs.uv_version }}" not in release_workflow:
        raise ValueError("release smoke tests must consume the build job's uv version")

    derived_files = (
        project_root / "Dockerfile",
        project_root / "Makefile",
        *workflow_paths,
        project_root / "README.md",
        project_root / "CONTRIBUTING.md",
        project_root / "pyproject.toml",
    )
    duplicates = [
        str(path.relative_to(project_root))
        for path in derived_files
        if version in path.read_text(encoding="utf-8")
    ]
    if duplicates:
        raise ValueError(
            f"uv {version} must be pinned only in {PIN_FILE.name}; duplicated in "
            + ", ".join(duplicates)
        )


def installed_uv_version(command: str) -> str:
    """Return the version reported by ``command --version``."""
    arguments = shlex.split(command, posix=os.name != "nt")
    if not arguments:
        raise ValueError("uv command must not be empty")

    completed = subprocess.run(  # noqa: S603
        [*arguments, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"uv command failed with exit code {completed.returncode}: {detail}")

    match = re.fullmatch(r"uv\s+(\S+)(?:\s+.*)?", completed.stdout.strip())
    if match is None:
        raise ValueError(f"unexpected uv --version output: {completed.stdout.strip()!r}")
    return match.group(1)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv-command", default="uv", help="uv executable or command")
    parser.add_argument(
        "--print-uv-version",
        action="store_true",
        help="print the pinned version without checking an executable",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate repository wiring without checking an executable",
    )
    return parser.parse_args()


def main() -> int:
    """Validate repository wiring and, by default, the installed uv executable."""
    args = parse_args()
    try:
        expected = pinned_uv_version()
        validate_repository_wiring(expected)
        if args.print_uv_version:
            print(expected)
            return 0
        if args.validate_only:
            print(f"validated canonical uv {expected} pin wiring")
            return 0

        actual = installed_uv_version(args.uv_command)
        if actual != expected:
            raise ValueError(
                f"uv {expected} is required by {PIN_FILE.name}, but {actual} is installed"
            )
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"uv pin check failed: {error}", file=sys.stderr)
        return 1

    print(f"validated uv {expected} from {PIN_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
