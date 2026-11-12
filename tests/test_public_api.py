"""Tests for the package's public import surface."""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_public_exceptions_are_exported() -> None:
    """Expose catchable package exceptions from the package root."""
    import iperf3_lib
    from iperf3_lib.exceptions import (
        IperfCleanupError,
        IperfError,
        IperfLibraryError,
        UnsupportedFeatureError,
    )

    assert iperf3_lib.IperfError is IperfError
    assert iperf3_lib.IperfCleanupError is IperfCleanupError
    assert issubclass(IperfCleanupError, IperfLibraryError)
    assert iperf3_lib.IperfLibraryError is IperfLibraryError
    assert iperf3_lib.UnsupportedFeatureError is UnsupportedFeatureError


def test_documented_public_submodules_match_importable_surface() -> None:
    """Keep compatibility.md's public submodule inventory synchronized with package modules."""
    import iperf3_lib

    doc_path = ROOT / "docs/reference/compatibility.md"
    if not doc_path.is_file():
        pytest.skip("Documentation checks run in the host gate; native images omit docs")
    section = doc_path.read_text(encoding="utf-8").split("### Public API boundary", 1)[1].lstrip()
    paragraph = section.split("\n\n", 1)[0]
    match = re.search(r"public submodules \((.*?)\)\.", paragraph, re.DOTALL)
    assert match is not None
    documented = set(re.findall(r"`([a-z0-9_.]+)`", match.group(1)))
    for name in sorted(documented):
        module = importlib.import_module(f"iperf3_lib.{name}")
        assert hasattr(module, "__all__")

    discovered = {
        info.name.removeprefix("iperf3_lib.")
        for info in pkgutil.walk_packages([str(Path(iperf3_lib.__file__).parent)], "iperf3_lib.")
        if not any(part.startswith("_") for part in info.name.split("."))
        and not info.name.startswith("iperf3_lib.ffi")
        and info.name != "iperf3_lib.exporters"
    }
    assert documented == discovered
