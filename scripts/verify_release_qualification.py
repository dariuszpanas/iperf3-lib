"""Verify the complete retained release matrix offline before publication."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from scripts import qualify_lifecycle, release_artifacts
from scripts.smoke_release import validate_smoke_receipt
from scripts.validate_release import project_version

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
NATIVE_VERSIONS = ("3.19.1", "3.22")
DISTRIBUTION_KINDS = ("wheel", "sdist")
EXPECTED_MATRIX = tuple(
    (python, native) for python in PYTHON_VERSIONS for native in NATIVE_VERSIONS
)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate receipt JSON field: {key}")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f"nonfinite receipt JSON value: {value}")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("receipt JSON number exceeds finite range")
    return parsed


def _json(path: Path) -> dict:
    _regular_file(path)
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object,
        parse_constant=_nonfinite,
        parse_float=_float,
    )
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be a JSON object: {path}")
    return value


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"retained evidence must be a regular file: {path}")


def _inventory(root: Path, names: set[str], *, directories: bool = False) -> None:
    if root.is_symlink() or not root.is_dir() or {p.name for p in root.iterdir()} != names:
        raise ValueError(f"retained evidence inventory differs: {root}")
    for name in names:
        path = root / name
        if path.is_symlink() or not (path.is_dir() if directories else path.is_file()):
            raise ValueError(f"unexpected retained evidence entry: {path}")


def _same(actual, expected, message: str) -> None:
    # JSON equality alone treats booleans as integers. Preserve exact receipt types.
    if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(
        expected, sort_keys=True, allow_nan=False
    ):
        raise ValueError(message)


def _python(value, expected: str, *, full: bool = False) -> str:
    pattern = rf"{re.escape(expected)}\.\d+" + (r"(?:\s.*)?" if full else "")
    if not isinstance(value, str) or re.fullmatch(pattern, value, re.DOTALL) is None:
        raise ValueError(f"receipt Python does not identify expected {expected}")
    return value.split()[0]


def _native(value, expected: str) -> None:
    if value not in (expected, f"iperf {expected}"):
        raise ValueError(f"receipt native library does not identify expected {expected}")


def _absolute(value, field: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must identify an absolute Linux path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"{field} must identify a normalized absolute Linux path")
    return path


def locked_pins(source: Path, python: str) -> list[str]:
    """Resolve only the sealed harness dependency closure for the recorded Linux CPython."""
    from packaging.markers import Marker
    from packaging.utils import canonicalize_name

    lock = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))
    packages: dict[str, list[dict]] = {}
    for package in lock["package"]:
        packages.setdefault(canonicalize_name(package["name"]), []).append(package)
    environment = {
        "implementation_name": "cpython",
        "implementation_version": python,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": python,
        "platform_python_implementation": "CPython",
        "python_version": ".".join(python.split(".")[:2]),
        "sys_platform": "linux",
        "extra": "",
    }
    pending, versions = ["pytest", "pytest-asyncio", "cffi"], {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in versions:
            continue
        candidates = packages.get(name, [])
        candidates = [
            package
            for package in candidates
            if not package.get("resolution-markers")
            or any(Marker(marker).evaluate(environment) for marker in package["resolution-markers"])
        ]
        if len(candidates) != 1:
            raise ValueError(f"sealed lock has missing or ambiguous harness dependency: {name}")
        package = candidates[0]
        version = package.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"sealed harness dependency is not pinned: {name}")
        versions[name] = version
        for dependency in package.get("dependencies", []):
            if "marker" not in dependency or Marker(dependency["marker"]).evaluate(environment):
                pending.append(dependency["name"])
    return [f"{name}=={version}" for name, version in sorted(versions.items())]


def _command(
    receipt: dict, inner: dict, *, python: str, native: str, kind: str, manifest_name: str
) -> None:
    command = receipt.get("command")
    if (
        not isinstance(command, list)
        or len(command) != 16
        or any(not isinstance(part, str) for part in command)
        or command[1] != "-I"
        or command[3] != "test"
        or command[4::2]
        != ["--manifest", "--source", "--config", "--native", "--python", "--output"]
        or command[11] != native
        or command[13] != python
    ):
        raise ValueError("lifecycle receipt lacks the exact isolated qualification command")
    executable = _absolute(inner.get("executable"), "installed executable")
    if (
        command[0] != str(executable)
        or executable.name != "python"
        or executable.parent.name != "bin"
    ):
        raise ValueError("lifecycle command differs from its installed interpreter")
    venv = executable.parent.parent
    if venv.name != "venv" or not venv.parent.name.startswith("iperf3-lifecycle-"):
        raise ValueError("lifecycle interpreter is outside its fresh qualification environment")
    runner = _absolute(command[2], "harness runner")
    config = _absolute(command[9], "harness configuration")
    source = _absolute(command[7], "source checkout")
    location = _absolute(inner.get("installed_location"), "installed module")
    expected_location = (
        venv / "lib" / f"python{python}" / "site-packages" / "iperf3_lib" / "__init__.py"
    )
    if (
        runner != venv.parent / "harness" / "qualify_lifecycle.py"
        or config != runner.parent / "pytest.ini"
        or location != expected_location
        or venv.is_relative_to(source)
        or source.is_relative_to(venv.parent)
        or _absolute(command[5], "lifecycle manifest").name != manifest_name
        or _absolute(command[15], "lifecycle test receipt").name != f"{kind}-tests.json"
    ):
        raise ValueError("lifecycle installed import or harness path contradicts isolation")


def _require_supported_sctp(inner: dict) -> None:
    for report in inner["reports"]:
        if report["phase"] != "call" or "test_live_events_roundtrip[sctp-" not in report["nodeid"]:
            continue
        evidence = json.loads(report["properties"]["live_events"])
        if evidence.get("support", {}).get("status") != "supported":
            raise ValueError(
                "release qualification requires executed SCTP traffic, not an unsupported probe"
            )


def verify_qualification(
    *,
    source: Path,
    distributions: Path,
    release_manifest: Path,
    release_manifest_sha256: str,
    lifecycle_manifest: Path,
    lifecycle_manifest_sha256: str,
    revision: str,
    version: str,
    smoke_root: Path,
    lifecycle_root: Path,
) -> dict:
    """Require every retained cell and independently sealed byte identity before success."""
    if (
        re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or project_version(source / "pyproject.toml") != version
    ):
        raise ValueError("release checkout version or expected source revision is invalid")
    for path, expected in (
        (release_manifest, release_manifest_sha256),
        (lifecycle_manifest, lifecycle_manifest_sha256),
    ):
        _regular_file(path)
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or release_artifacts.digest(path) != expected
        ):
            raise ValueError(f"independently retained manifest hash mismatch: {path.name}")
    release = _json(release_manifest)
    lifecycle = _json(lifecycle_manifest)
    _same(release.get("schema_version"), 1, "release manifest schema differs")
    _same(lifecycle.get("schema_version"), 1, "lifecycle manifest schema differs")
    release_artifacts.verify_manifest(
        distributions,
        release_manifest,
        version=version,
        revision=revision,
        manifest_sha256=release_manifest_sha256,
    )
    sealed = qualify_lifecycle.verify_manifest(
        source, distributions, revision, manifest_path=lifecycle_manifest
    )
    _same(lifecycle, sealed, "lifecycle manifest decode differs")
    artifacts = {entry["kind"]: entry for entry in lifecycle["distributions"]}
    for entry in artifacts.values():
        _regular_file(distributions / entry["filename"])
        _same(
            release["files"][entry["filename"]],
            {key: entry[key] for key in ("sha256", "size")},
            "independent manifests disagree about distribution bytes",
        )
        if type(entry["size"]) is not int or entry["size"] < 1:
            raise ValueError("manifest artifact size must be a positive integer")
    cells = EXPECTED_MATRIX
    for root, prefix in ((smoke_root, "native"), (lifecycle_root, "lifecycle")):
        _inventory(
            root,
            {f"{prefix}-evidence-py{python}-iperf{native}" for python, native in cells},
            directories=True,
        )
    selection = qualify_lifecycle.expected_tests()
    if len(selection) != 49 or len(set(selection)) != 49:
        raise ValueError("release lifecycle case policy changed; review the aggregate inventory")
    receipts = []
    for python, native in cells:
        smoke_dir = smoke_root / f"native-evidence-py{python}-iperf{native}"
        lifecycle_dir = lifecycle_root / f"lifecycle-evidence-py{python}-iperf{native}"
        _inventory(smoke_dir, {f"{kind}.json" for kind in DISTRIBUTION_KINDS})
        _inventory(
            lifecycle_dir,
            {
                f"{kind}{suffix}"
                for kind in DISTRIBUTION_KINDS
                for suffix in (".json", "-tests.json", ".log")
            },
        )
        for kind in DISTRIBUTION_KINDS:
            artifact = artifacts[kind]
            smoke_path, outer_path = smoke_dir / f"{kind}.json", lifecycle_dir / f"{kind}.json"
            inner_path, log_path = (
                lifecycle_dir / f"{kind}-tests.json",
                lifecycle_dir / f"{kind}.log",
            )
            smoke, outer, inner = _json(smoke_path), _json(outer_path), _json(inner_path)
            if smoke.get("source_revision") != revision or smoke.get("package_version") != version:
                raise ValueError("smoke receipt source/package identity differs")
            _same(
                smoke.get("artifact"),
                {key: artifact[key] for key in ("filename", "sha256")},
                "smoke receipt belongs to another retained distribution",
            )
            _python(smoke.get("python"), python, full=True)
            _native(smoke.get("native_version"), native)
            smoke_location = _absolute(smoke.get("installed_location"), "smoke installed module")
            if smoke_location != PurePosixPath(
                f"/tmp/installed-{kind}/lib/python{python}/site-packages/iperf3_lib/__init__.py"
            ):
                raise ValueError(
                    "smoke receipt did not import from its isolated installed distribution"
                )
            validate_smoke_receipt(smoke)
            if (
                outer.get("kind") != "iperf3-lib.installed-lifecycle"
                or type(outer.get("schema_version")) is not int
                or outer["schema_version"] != 1
                or outer.get("source_revision") != revision
                or outer.get("manifest_sha256") != lifecycle_manifest_sha256
                or outer.get("expected_python") != python
                or outer.get("expected_native_version") != native
                or outer.get("status") != "passed"
                or type(outer.get("exit_code")) is not int
                or outer["exit_code"] != 0
                or "error" in outer
            ):
                raise ValueError("lifecycle outer receipt identity or outcome differs")
            _same(
                outer.get("distribution"),
                artifact,
                "lifecycle receipt belongs to another retained distribution",
            )
            _same(
                outer.get("harness_sha256"),
                lifecycle["harness_sha256"],
                "lifecycle receipt uses another sealed harness",
            )
            _same(
                outer.get("tests"),
                inner,
                "embedded lifecycle tests differ from the separately retained receipt",
            )
            if (
                inner.get("status") != "passed"
                or type(inner.get("exit_code")) is not int
                or inner["exit_code"] != 0
                or inner.get("package_version") != version
                or "error" in inner
                or not isinstance(inner.get("platform"), str)
                or not inner["platform"].startswith("Linux-")
            ):
                raise ValueError("lifecycle installed outcome or package/platform differs")
            actual_python = _python(inner.get("python"), python)
            _native(inner.get("native_version"), native)
            pins = locked_pins(source, actual_python)
            _same(
                outer.get("dependency_pins"),
                pins,
                "lifecycle outer dependencies differ from the sealed lock",
            )
            _same(
                inner.get("dependency_pins"),
                pins,
                "lifecycle installed dependencies differ from the sealed lock",
            )
            _command(
                outer,
                inner,
                python=python,
                native=native,
                kind=kind,
                manifest_name=lifecycle_manifest.name,
            )
            qualify_lifecycle.validate_results(inner)
            _require_supported_sctp(inner)
            receipts.append(
                {
                    "python": python,
                    "native": native,
                    "distribution": kind,
                    "artifact_sha256": artifact["sha256"],
                    "smoke_sha256": release_artifacts.digest(smoke_path),
                    "lifecycle_sha256": release_artifacts.digest(outer_path),
                    "lifecycle_tests_sha256": release_artifacts.digest(inner_path),
                    "lifecycle_log_sha256": release_artifacts.digest(log_path),
                }
            )
    return {
        "kind": "iperf3-lib.release-qualification",
        "schema_version": 1,
        "status": "passed",
        "source_revision": revision,
        "package_version": version,
        "release_manifest_sha256": release_manifest_sha256,
        "lifecycle_manifest_sha256": lifecycle_manifest_sha256,
        "matrix": {
            "python": list(PYTHON_VERSIONS),
            "native": list(NATIVE_VERSIONS),
            "distribution": list(DISTRIBUTION_KINDS),
        },
        "lifecycle_selection": selection,
        "receipts": receipts,
        "totals": {
            "smoke_receipts": len(receipts),
            "lifecycle_receipts": len(receipts),
            "lifecycle_cases": len(receipts) * len(selection),
            "lifecycle_phases": len(receipts) * len(selection) * 3,
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Write a compact success index only after the entire retained matrix validates."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "source",
        "distributions",
        "release-manifest",
        "lifecycle-manifest",
        "smoke-root",
        "lifecycle-root",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("release-manifest-sha256", "lifecycle-manifest-sha256", "revision", "version"):
        parser.add_argument(f"--{name}", required=True)
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    try:
        resolved = output.resolve()
        if resolved in {
            args[key].resolve() for key in ("release_manifest", "lifecycle_manifest")
        } or any(
            resolved.is_relative_to(args[key].resolve())
            for key in ("distributions", "smoke_root", "lifecycle_root")
        ):
            raise ValueError("qualification index cannot overwrite retained inputs")
        output.unlink(missing_ok=True)
        index = verify_qualification(**args)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(index, stream, indent=2, allow_nan=False)
            stream.write("\n")
        try:
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        print(
            f"release qualification passed: {len(EXPECTED_MATRIX) * len(DISTRIBUTION_KINDS)} smoke and lifecycle receipts ({output})"
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"release qualification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
