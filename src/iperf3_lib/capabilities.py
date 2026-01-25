"""Feature detection for optional libiperf capabilities."""

from __future__ import annotations

from .ffi import api


def has_symbol(name: str) -> bool:
    """Check if the loaded libiperf exposes a given symbol.

    Args:
        name: The symbol name to check.

    Returns:
        True if the symbol exists, False otherwise.
    """
    try:
        return hasattr(api.lib, name)
    except Exception:
        return False


# Common optional features
HAS_BIDIR = has_symbol("iperf_set_test_bidirectional")
HAS_MPTCP = has_symbol("iperf_set_test_mptcp")
HAS_JSON_OUTPUT = has_symbol("iperf_set_test_json_output")

__all__ = ["has_symbol", "HAS_BIDIR", "HAS_MPTCP", "HAS_JSON_OUTPUT"]
