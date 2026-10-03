"""Custom exceptions for iperf3-lib errors and unsupported features."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._cancellation import _ExecutionControl


class IperfError(RuntimeError):
    """libiperf reported a runtime error (i_errno/iperf_strerror)."""


class IperfLibraryError(RuntimeError):
    """Failed to create or use libiperf objects (allocation/dlopen issues)."""


class IperfCleanupError(IperfLibraryError):
    """Worker cleanup failed; cancellation or timeout could not finish safely."""

    def __init__(self, message: str, *, control: _ExecutionControl) -> None:
        """Retain the private process owner until its child is finally reaped."""
        super().__init__(message)
        self._control = control


class UnsupportedFeatureError(RuntimeError):
    """Requested feature is not supported by the loaded libiperf."""
