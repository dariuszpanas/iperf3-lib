"""Custom exceptions for iperf3-lib errors and unsupported features."""


class IperfError(RuntimeError):
    """libiperf reported a runtime error (i_errno/iperf_strerror)."""


class IperfLibraryError(RuntimeError):
    """Failed to create or use libiperf objects (allocation/dlopen issues)."""


class UnsupportedFeatureError(RuntimeError):
    """Requested feature is not supported by the loaded libiperf."""
