"""Tests for the package's public import surface."""


def test_public_exceptions_are_exported():
    """Expose catchable package exceptions from the package root."""
    import iperf3_lib
    from iperf3_lib.exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError

    assert iperf3_lib.IperfError is IperfError
    assert iperf3_lib.IperfLibraryError is IperfLibraryError
    assert iperf3_lib.UnsupportedFeatureError is UnsupportedFeatureError
