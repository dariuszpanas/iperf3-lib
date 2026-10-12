"""Route documentation changes without weakening the required CI result."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

ROOT_DOCUMENTS = frozenset({"README.md", "CONTRIBUTING.md", "SECURITY.md"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"})
DOC_SUFFIXES = IMAGE_SUFFIXES | {".md", ".css"}
OVERRIDE_SUFFIXES = IMAGE_SUFFIXES | {".html", ".css"}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class Change:
    """One changed path, retaining modes and deletions from Git's raw diff."""

    path: str
    old_mode: str
    new_mode: str
    status: str


@dataclass(frozen=True)
class Scope:
    """An explicit native-test decision bound to the event's checked-out commit."""

    native_required: bool
    reason: str
    head: str
    comparison_base: str | None = None
    changes: tuple[Change, ...] = ()


def documentation_path(path: str) -> bool:
    """Recognize only documentation text, styles, images and HTML templates."""
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or "\\" in path:
        return False
    if path in ROOT_DOCUMENTS:
        return True
    if len(value.parts) < 2:
        return False
    if value.parts[0] in {"docs", "development"}:
        return value.suffix in DOC_SUFFIXES
    return value.parts[0] == "overrides" and value.suffix in OVERRIDE_SUFFIXES


def parse_changes(raw: bytes) -> tuple[Change, ...]:
    """Parse NUL-delimited raw records without losing unusual names or old paths."""
    if not raw:
        return ()
    fields = raw.split(b"\0")
    if fields.pop() != b"" or len(fields) % 2:
        raise ValueError("Git returned a malformed NUL-delimited change list")
    changes = []
    for position in range(0, len(fields), 2):
        header = fields[position].decode("ascii").split()
        if len(header) != 5 or not header[0].startswith(":") or len(header[4]) != 1:
            raise ValueError("Git returned an unexpected raw diff record")
        changes.append(
            Change(
                path=fields[position + 1].decode("utf-8", errors="surrogateescape"),
                old_mode=header[0][1:],
                new_mode=header[1],
                status=header[4],
            )
        )
    return tuple(changes)


def docs_only(changes: tuple[Change, ...]) -> bool:
    """Require a nonempty change set containing only ordinary documentation files."""
    return bool(changes) and all(
        change.status in {"A", "M", "D"}
        and change.old_mode in {"000000", "100644"}
        and change.new_mode in {"000000", "100644"}
        and documentation_path(change.path)
        for change in changes
    )


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def detect_scope(repository: Path, *, event: str, base: str, head: str) -> Scope:
    """Use exact event SHAs; ambiguous history takes the full native route."""
    if not SHA_PATTERN.fullmatch(head) or head == ZERO_SHA:
        raise ValueError("the event head must be a full nonzero commit SHA")
    actual = _git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    if actual != head:
        raise ValueError("the checkout does not match the exact event head")
    if event not in {"pull_request", "push"}:
        return Scope(True, "Manual, scheduled and other events require native qualification", head)
    if not SHA_PATTERN.fullmatch(base) or base == ZERO_SHA:
        return Scope(True, "No usable event base; require native qualification", head)
    try:
        _git(repository, "cat-file", "-e", f"{base}^{{commit}}")
        if event == "pull_request":
            comparison = _git(repository, "merge-base", base, head).decode("ascii").strip()
        else:
            _git(repository, "merge-base", "--is-ancestor", base, head)
            comparison = base
    except subprocess.CalledProcessError:
        return Scope(True, "Comparison history is unavailable; require native qualification", head)
    changes = parse_changes(
        _git(
            repository,
            "diff",
            "--raw",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-renames",
            "--ignore-submodules=none",
            "-z",
            comparison,
            head,
            "--",
        )
    )
    only_docs = docs_only(changes)
    reason = (
        "Every changed path is an ordinary documentation file"
        if only_docs
        else "Changed paths or file modes require native qualification"
    )
    return Scope(not only_docs, reason, head, comparison, changes)


def verify_gate(results: dict[str, str]) -> None:
    """Reject failures and accept a skipped matrix only for an explicit docs route."""
    for key in ("SCOPE_RESULT", "LINT_RESULT"):
        if results[key] != "success":
            raise ValueError(f"{key} must succeed")
    required = results["NATIVE_REQUIRED"]
    if required not in {"true", "false"}:
        raise ValueError("native requirement must be explicitly true or false")
    event = results["EVENT_NAME"]
    if required == "false" and event not in {"pull_request", "push"}:
        raise ValueError("this event requires native qualification")
    expected_native = "success" if required == "true" else "skipped"
    if results["NATIVE_RESULT"] != expected_native:
        raise ValueError(f"native matrix must report {expected_native}")
    expected_commit = "success" if event == "pull_request" else "skipped"
    if results["COMMIT_RESULT"] != expected_commit:
        raise ValueError(f"commit policy must report {expected_commit}")


def main() -> int:
    """Detect workflow scope or validate the final aggregate's upstream results."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    detect = commands.add_parser("detect")
    detect.add_argument("--event", required=True)
    detect.add_argument("--base", default="")
    detect.add_argument("--head", required=True)
    detect.add_argument("--repository", type=Path, default=Path.cwd())
    detect.add_argument("--github-output", type=Path)
    detect.add_argument("--summary", type=Path)
    commands.add_parser("gate")
    args = parser.parse_args()
    try:
        if args.command == "gate":
            verify_gate(dict(os.environ))
            print("Required CI checks passed")
            return 0
        scope = detect_scope(args.repository, event=args.event, base=args.base, head=args.head)
        print(json.dumps(asdict(scope), indent=2))
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                output.write(f"native-required={str(scope.native_required).lower()}\n")
        if args.summary:
            with args.summary.open("a", encoding="utf-8", newline="\n") as summary:
                route = "full native matrix" if scope.native_required else "documentation checks"
                summary.write(
                    f"CI route: **{route}**. {scope.reason}.\n\n"
                    f"Compared {len(scope.changes)} paths at `{scope.head}`.\n"
                )
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"CI scope check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
