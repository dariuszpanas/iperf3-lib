"""Assert and record the isolated Python 3.15 release-candidate test runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath

PYTHON_VERSION = "3.15.0rc3"
UV_VERSION = "0.12.23"
PREVIEW_PREFIX = "/opt/preview-venv"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.12.23@sha256:dd2385ac82b9aff6489344ec2b22916db986eb135454d030bce3038c7598a689"
# Declared download identity from this pinned uv release's catalog. uv checks the
# archive during installation; these fields are not measurements of a live archive.
PYTHON_DOWNLOAD = {
    "catalog": "https://raw.githubusercontent.com/astral-sh/uv/0.12.23/crates/uv-python/download-metadata.json",
    "key": "cpython-3.15.0rc3-linux-x86_64-gnu",
    "build": "20261003",
    "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.15.0rc3%2B20261003-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
    "sha256": "84431c57aa4b65f8643854d93d951d5f3ca9be224734a80ad7289f889ba28f27",
}


def _runtime() -> dict:
    return {
        "python": platform.python_version(),
        "python_version_info": list(sys.version_info),
        "sys_version": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "gil_disabled": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "uv_version": subprocess.check_output(["uv", "--version"], text=True, timeout=10).strip(),
    }


def check_runtime(runtime: dict) -> None:
    """Reject another interpreter, environment, platform, or preview toolchain."""
    if (
        runtime["python"] != PYTHON_VERSION
        or runtime["python_version_info"] != [3, 15, 0, "candidate", 3]
        or runtime["implementation"] != "CPython"
        or runtime["gil_disabled"] is not False
    ):
        raise ValueError("preview requires standard-GIL CPython 3.15.0rc3")
    if runtime["system"] != "Linux" or runtime["machine"] != "x86_64":
        raise ValueError("preview qualification requires Linux x86_64")
    if (
        runtime["prefix"] != PREVIEW_PREFIX
        or runtime["base_prefix"] == PREVIEW_PREFIX
        or str(PurePosixPath(runtime["executable"]).parent) != f"{PREVIEW_PREFIX}/bin"
        or not PurePosixPath(runtime["base_prefix"]).is_relative_to("/opt/preview-python")
    ):
        raise ValueError("preview must run inside its distinct managed-Python environment")
    if (
        re.fullmatch(rf"uv {re.escape(UV_VERSION)}(?: \([^\r\n]*\))?", runtime["uv_version"])
        is None
    ):
        raise ValueError("preview requires pinned uv 0.12.23")


def _native_version() -> str:
    from iperf3_lib.ffi.api import ffi, lib

    return ffi.string(lib.iperf_get_iperf_version()).decode("utf-8")


def _dependencies() -> dict[str, str]:
    return dict(
        sorted(
            (distribution.metadata["Name"], distribution.version)
            for distribution in importlib.metadata.distributions()
        )
    )


def record_runtime(revision: str, native: str, lock: Path) -> dict:
    """Capture actual runtime evidence only after checking the exact candidate."""
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if native not in {"3.19.1", "3.22"}:
        raise ValueError("preview requires a supported native endpoint")
    runtime = _runtime()
    check_runtime(runtime)
    observed_native = _native_version()
    if observed_native not in {native, f"iperf {native}"}:
        raise ValueError("native getter differs from the selected preview endpoint")
    return {
        "kind": "iperf3-lib.python-preview-runtime",
        "schema_version": 1,
        "status": "preview",
        "source_revision": revision,
        "expected_python": PYTHON_VERSION,
        "expected_native_version": native,
        "declared_uv_image": UV_IMAGE,
        "declared_python_download": dict(PYTHON_DOWNLOAD),
        **runtime,
        "native_version": observed_native,
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "package_version": importlib.metadata.version("iperf3-lib"),
        "dependencies": _dependencies(),
    }


def main(argv: list[str] | None = None) -> int:
    """Write provenance for a verified preview runtime, or fail before native tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--native", required=True, choices=("3.19.1", "3.22"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.output.unlink(missing_ok=True)
        receipt = record_runtime(
            args.revision, args.native, Path(__file__).resolve().parents[1] / "uv.lock"
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(receipt, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Python preview runtime rejected: {error}", file=sys.stderr)
        return 1
    print(f"Verified {PYTHON_VERSION} preview with libiperf {args.native}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
