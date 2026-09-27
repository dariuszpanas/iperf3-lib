"""Strict dataclass validation for server listener and native policy options."""

from dataclasses import asdict

import pytest

from iperf3_lib.server_config import ServerConfig

INTEGER_BOUNDS = {
    "port": (1, 65535),
    "idle_timeout_seconds": (1, 86400),
    "receive_timeout_ms": (100, 86400000),
    "send_timeout_ms": (0, 86400000),
    "bitrate_limit_bps": (0, 2**53 - 1),
    "max_duration_seconds": (0, 86400),
    "affinity": (0, 1024),
    "time_skew_threshold_seconds": (1, 2**31 - 1),
}
TEXT_FIELDS = (
    "bind_address",
    "bind_device",
    "rsa_private_key_path",
    "authorized_users_path",
    "payload_file",
    "extra_data",
)


def _config(**kwargs):
    if "time_skew_threshold_seconds" in kwargs:
        kwargs.update(rsa_private_key_path="private.pem", authorized_users_path="users.csv")
    return ServerConfig(**kwargs)


@pytest.mark.parametrize("name,bounds", INTEGER_BOUNDS.items())
def test_server_integer_bounds_are_inclusive(name, bounds):
    """Accept the documented minimum and maximum native integer values."""
    for value in bounds:
        assert getattr(_config(**{name: value}), name) == value


@pytest.mark.parametrize("name,bounds", INTEGER_BOUNDS.items())
def test_server_integer_bounds_reject_overflow(name, bounds):
    """Reject both out-of-range edges before the parser can wrap or truncate."""
    for value in (bounds[0] - 1, bounds[1] + 1):
        with pytest.raises(ValueError, match=name):
            _config(**{name: value})


@pytest.mark.parametrize("name", INTEGER_BOUNDS)
@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_server_integer_options_reject_coercion(name, value):
    """Booleans, floats and strings do not silently become native integers."""
    with pytest.raises(TypeError, match=name):
        _config(**{name: value})


@pytest.mark.parametrize("name", TEXT_FIELDS)
@pytest.mark.parametrize("value", [False, 0, [], "", " \t", "a\0b"])
def test_server_strings_are_nonempty_and_nul_free(name, value):
    """Reject silent skipping and C-string truncation for every text setting."""
    with pytest.raises((TypeError, ValueError), match=name):
        ServerConfig(**{name: value})


@pytest.mark.parametrize("name", ["interval_seconds", "bitrate_limit_interval_seconds"])
@pytest.mark.parametrize("value", [0, 0.1, 1, 60])
def test_server_intervals_accept_native_bounds(name, value):
    """Permit disabled reporting and both inclusive interval limits."""
    config = ServerConfig(
        **{
            name: value,
            "bitrate_limit_bps": 0 if name == "interval_seconds" and value == 0 else 1000,
        }
    )
    assert getattr(config, name) == value


def test_active_bitrate_limit_requires_periodic_stats():
    """A disabled stats timer cannot enforce the requested native rate policy."""
    with pytest.raises(ValueError, match="nonzero stats interval"):
        ServerConfig(interval_seconds=0, bitrate_limit_bps=1)


@pytest.mark.parametrize("name", ["interval_seconds", "bitrate_limit_interval_seconds"])
@pytest.mark.parametrize("value", [True, "1", -1, 0.01, 61, float("nan"), float("inf")])
def test_server_intervals_reject_invalid_values(name, value):
    """Require finite numbers in the native interval domain."""
    with pytest.raises((TypeError, ValueError), match=name):
        ServerConfig(**{name: value, "bitrate_limit_bps": 1000})


@pytest.mark.parametrize("value", ["", "inet", "IPv4", False])
def test_server_address_family_rejects_unknown_values(value):
    """Do not silently fall back to automatic address selection."""
    with pytest.raises((TypeError, ValueError), match="address_family"):
        ServerConfig(address_family=value)


@pytest.mark.parametrize("name", ["use_pkcs1_padding", "json_stream"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_server_flags_require_booleans(name, value):
    """Keep presence semantics distinct from arbitrary truthy values."""
    with pytest.raises(TypeError, match=name):
        ServerConfig(**{name: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rsa_private_key_path": "private.pem"},
        {"authorized_users_path": "users.csv"},
        {"time_skew_threshold_seconds": 5},
        {"use_pkcs1_padding": True},
    ],
)
def test_server_authentication_combinations_are_explicit(kwargs):
    """Reject partially configured authentication instead of weakening it."""
    with pytest.raises(ValueError, match="authentication|supplied together"):
        ServerConfig(**kwargs)


def test_server_authentication_file_contents_are_not_opened():
    """Construction can remain offline and paths resolve only in the worker."""
    config = ServerConfig(
        rsa_private_key_path="missing-private.pem",
        authorized_users_path="missing-users.csv",
        time_skew_threshold_seconds=30,
        use_pkcs1_padding=True,
    )
    assert asdict(config)["authorized_users_path"] == "missing-users.csv"


def test_server_bitrate_interval_requires_limit():
    """Avoid accepting an interval that cannot apply to any requested limit."""
    with pytest.raises(ValueError, match="requires bitrate_limit_bps"):
        ServerConfig(bitrate_limit_interval_seconds=1)


def test_server_rejects_minimum_native_unsafe_interval_combination():
    """A native bitrate averaging window cannot divide by a disabled stats interval."""
    with pytest.raises(ValueError, match="nonzero stats interval"):
        ServerConfig(interval_seconds=0, bitrate_limit_bps=1000, bitrate_limit_interval_seconds=1)


@pytest.mark.parametrize("value", [(0, 0, 0), (9, 2, 3), (3, 0, 0)])
def test_server_keepalive_tuple_is_retained(value):
    """Preserve explicit kernel-default or custom control keepalive settings."""
    assert ServerConfig(control_keepalive=value).control_keepalive == value


@pytest.mark.parametrize(
    "value",
    [
        [9, 2, 3],
        (),
        (1, 2),
        (1, 2, 3, 4),
        (True, 0, 0),
        ("9", 2, 3),
        (-1, 0, 0),
        (2**31, 0, 0),
        (6, 2, 3),
        (5, 2, 3),
    ],
)
def test_server_keepalive_rejects_invalid_shape_and_relationship(value):
    """Require exactly three bounded integers and a valid timeout relationship."""
    with pytest.raises((TypeError, ValueError), match="control_keepalive"):
        ServerConfig(control_keepalive=value)
