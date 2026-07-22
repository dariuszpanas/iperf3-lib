"""Validate release inputs against the version in ``pyproject.toml``."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

PROJECT_FILE = Path(__file__).resolve().parents[1] / "pyproject.toml"
PRERELEASE_PATTERN = re.compile(r"(?:a|b|rc|dev)\d", re.IGNORECASE)


def project_version() -> str:
    """Return the static project version from ``pyproject.toml``."""
    with PROJECT_FILE.open("rb") as project_file:
        data = tomllib.load(project_file)

    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version or version.strip() != version:
        raise ValueError("project.version must be a non-empty static string")
    if any(character.isspace() for character in version):
        raise ValueError("project.version must not contain whitespace")
    return version


def validate(version: str, *, tag: str | None, requested_version: str | None) -> None:
    """Validate an optional Git tag and workflow input against ``version``."""
    if tag and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match project version v{version}")
    if requested_version and requested_version != version:
        raise ValueError(
            f"requested version {requested_version!r} does not match project version {version}"
        )


def write_github_outputs(output_path: Path, version: str) -> None:
    """Append validated release metadata to a GitHub Actions output file."""
    prerelease = bool(PRERELEASE_PATTERN.search(version))
    with output_path.open("a", encoding="utf-8", newline="\n") as output_file:
        output_file.write(f"version={version}\n")
        output_file.write(f"prerelease={str(prerelease).lower()}\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Git tag, expected to equal v<project.version>")
    parser.add_argument("--requested-version", help="Manual workflow version input")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GitHub Actions output file (defaults to GITHUB_OUTPUT)",
    )
    return parser.parse_args()


def main() -> int:
    """Validate release metadata and optionally emit GitHub job outputs."""
    args = parse_args()
    output_path = args.github_output
    if output_path is None and os.environ.get("GITHUB_OUTPUT"):
        output_path = Path(os.environ["GITHUB_OUTPUT"])

    try:
        version = project_version()
        validate(version, tag=args.tag, requested_version=args.requested_version)
        if output_path is not None:
            write_github_outputs(output_path, version)
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1

    print(f"validated release version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
