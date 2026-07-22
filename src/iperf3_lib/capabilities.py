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
HAS_JSON_OUTPUT = has_symbol("iperf_set_test_json_output")
HAS_JSON_CALLBACK = has_symbol("iperf_set_test_json_callback")
HAS_PROTOCOL_SELECTION = has_symbol("set_protocol") and has_symbol("iperf_get_test_protocol_id")
HAS_BIND_ADDRESS = has_symbol("iperf_set_test_bind_address")

# These configuration fields remain for compatibility, but the direct ABI
# backend intentionally rejects them because libiperf exposes no complete,
# stable public API for either feature.
HAS_MPTCP = False
HAS_JSON_STREAM = False

__all__ = [
    "HAS_BIDIR",
    "HAS_BIND_ADDRESS",
    "HAS_JSON_CALLBACK",
    "HAS_JSON_OUTPUT",
    "HAS_JSON_STREAM",
    "HAS_MPTCP",
    "HAS_PROTOCOL_SELECTION",
    "has_symbol",
]
