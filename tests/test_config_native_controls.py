"""Admission and JSON snapshots for the expanded native client configuration."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from ipaddress import IPv6Address
from typing import Any

import pytest

from iperf3_lib.config import (
    LEGACY_CONFIG_FIELDS,
    ClientConfig,
    Protocol,
    config_from_dict,
    config_to_dict,
    requires_worker,
)

INTEGER_OPTIONS = [
    ("client_port", 1, 65535, {}),
    ("socket_buffer_bytes", 1, 512 * 1024 * 1024, {}),
    ("mss", 1, 32767, {}),
    ("connect_timeout_ms", 1, 2**31 - 1, {}),
    ("bytes_to_send", 1, 2**64 - 1, {"duration": None}),
    ("blocks_to_send", 1, 2**64 - 1, {"duration": None}),
    ("pacing_timer_us", 1, 2**31 - 1, {}),
    ("fq_rate_bps", 0, 2**53 - 1, {}),
    ("burst_packets", 1, 1000, {}),
    ("flow_label", 1, 0xFFFFF, {"address_family": "ipv6"}),
    ("sctp_streams", 1, 65535, {"protocol": "sctp"}),
    ("affinity", 0, 1024, {}),
    ("server_affinity", 0, 1024, {"affinity": 0}),
    ("receive_timeout_ms", 100, 86400000, {"reverse": True}),
    ("send_timeout_ms", 0, 86400000, {}),
]
BOOLEAN_OPTIONS = [
    "no_delay",
    "zerocopy",
    "skip_rx_copy",
    "udp_counters_64bit",
    "dont_fragment",
    "repeating_payload",
    "get_server_output",
    "gsro",
    "use_pkcs1_padding",
]
TEXT_OPTIONS = [
    "bind_address",
    "bind_device",
    "congestion_control",
    "payload_file",
    "title",
    "extra_data",
    "username",
    "rsa_public_key_path",
]


@pytest.mark.parametrize("name,minimum,maximum,context", INTEGER_OPTIONS)
@pytest.mark.parametrize("edge", ["minimum", "maximum"])
def test_native_integer_boundaries_are_exact(name, minimum, maximum, context, edge):
    """Accepted extrema survive JSON without float rounding or boolean coercion."""
    value = minimum if edge == "minimum" else maximum
    config = ClientConfig("localhost", **context, **{name: value})
    restored = config_from_dict(json.loads(json.dumps(config_to_dict(config))))
    assert getattr(restored, name) == value
    assert type(getattr(restored, name)) is int
    assert restored == config


@pytest.mark.parametrize("name,minimum,maximum,context", INTEGER_OPTIONS)
@pytest.mark.parametrize("invalid", ["below", "above", "boolean", "float", "text"])
def test_native_integers_reject_out_of_range_or_coerced_values(
    name, minimum, maximum, context, invalid
):
    """Typed native integer admission rejects coercion and out-of-range values."""
    value = {
        "below": minimum - 1,
        "above": maximum + 1,
        "boolean": True,
        "float": float(minimum),
        "text": str(minimum),
    }[invalid]
    error = ValueError if invalid in {"below", "above"} else TypeError
    with pytest.raises(error, match=name):
        ClientConfig("localhost", **context, **{name: value})


@pytest.mark.parametrize("name", BOOLEAN_OPTIONS)
@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_native_flags_require_actual_booleans(name, value):
    """Optional native flags never accept truthy strings or integer stand-ins."""
    with pytest.raises(TypeError, match=name):
        ClientConfig("localhost", **{name: value})


@pytest.mark.parametrize("name", TEXT_OPTIONS)
@pytest.mark.parametrize("value", ["", " \t ", "unsafe\0tail", 123])
def test_native_text_is_nonempty_and_nul_free(name, value):
    """String boundaries reject truncation before invoking native code."""
    error = TypeError if isinstance(value, int) else ValueError
    with pytest.raises(error, match=name):
        ClientConfig("localhost", **{name: value})


@pytest.mark.parametrize("value", [True, "1", [], float("nan"), float("inf"), -1, 0.01, 61])
def test_reporting_interval_rejects_invalid_values(value):
    """Intervals are finite numbers at native-supported boundaries."""
    with pytest.raises((TypeError, ValueError), match="interval_seconds"):
        ClientConfig("localhost", interval_seconds=value)


@pytest.mark.parametrize("value", [0, 0.1, 1.25, 60])
def test_reporting_interval_accepts_disabled_and_fractional_values(value):
    """Zero disables interval reports while fractional supported values remain exact."""
    config = ClientConfig("localhost", interval_seconds=value)
    assert config_from_dict(config_to_dict(config)) == config
    assert requires_worker(config)


@pytest.mark.parametrize(
    "options",
    [
        {"protocol": "udp", "mptcp": True},
        {"protocol": "sctp", "mptcp": True},
        {"protocol": "udp", "congestion_control": "reno"},
        {"protocol": "sctp", "congestion_control": "reno"},
        {"protocol": "udp", "zerocopy": True},
        {"protocol": "sctp", "zerocopy": True},
        {"protocol": "sctp", "skip_rx_copy": True},
        {"protocol": "sctp", "fq_rate_bps": 0},
        {"protocol": "sctp", "fq_rate_bps": 1000},
        {"protocol": "sctp", "tos": 1},
        {"protocol": "sctp", "tos": 255},
        {"protocol": "sctp", "mss": 511},
        {"protocol": "udp", "mss": 1400},
        {"protocol": "udp", "no_delay": True},
        {"protocol": "udp", "flow_label": 1, "address_family": "ipv6"},
        {"protocol": "sctp", "flow_label": 1, "address_family": "ipv6"},
        {"flow_label": 1},
        {"flow_label": 1, "address_family": "ipv4"},
        {"sctp_streams": 1},
        {"sctp_bind_addresses": ("127.0.0.1",)},
        {"udp_counters_64bit": True},
        {"dont_fragment": True},
        {"gsro": True},
    ],
)
def test_inapplicable_native_controls_are_rejected(options):
    """Protocol combinations with known native no-ops are explicit errors."""
    with pytest.raises(ValueError):
        ClientConfig("localhost", **options)


@pytest.mark.parametrize(
    "options",
    [
        {"zerocopy": True, "skip_rx_copy": True},
        {"protocol": "udp", "skip_rx_copy": True, "fq_rate_bps": 0},
        {"protocol": "sctp", "mss": 512, "no_delay": True},
        {"protocol": "sctp", "tos": 0},
        {"protocol": "sctp", "mss": 32767, "sctp_streams": 2},
        {"flow_label": 0xFFFFF, "address_family": "ipv6"},
        {"protocol": "udp", "udp_counters_64bit": True, "gsro": True},
    ],
)
def test_protocol_specific_controls_remain_available(options):
    """Supported option combinations remain admitted independently of OS capability."""
    config = ClientConfig("localhost", **options)
    assert config_from_dict(config_to_dict(config)) == config


@pytest.mark.parametrize("server,family", [("::1", "auto"), ("host", "ipv6")])
def test_dont_fragment_rejects_ipv6(server, family):
    """IPv4 fragmentation policy cannot silently become an IPv6 no-op."""
    with pytest.raises(ValueError, match="IPv4"):
        ClientConfig(server, protocol=Protocol.UDP, dont_fragment=True, address_family=family)


def test_dont_fragment_resolves_automatic_family_to_ipv4():
    """Hostname resolution cannot pick a family that ignores the requested DF flag."""
    config = ClientConfig("host", protocol=Protocol.UDP, dont_fragment=True)
    assert config.address_family == "ipv4"
    assert config_to_dict(config)["address_family"] == "ipv4"


@pytest.mark.parametrize(
    "options",
    [
        {"duration": None},
        {"bytes_to_send": 10},
        {"blocks_to_send": 10},
        {"duration": 0, "bytes_to_send": 10},
        {"duration": None, "bytes_to_send": 10, "blocks_to_send": 1},
    ],
)
def test_termination_modes_are_mutually_exclusive(options):
    """Neither finite nor unlimited duration can be mixed with transfer counts."""
    with pytest.raises(ValueError, match="duration"):
        ClientConfig("localhost", **options)


@pytest.mark.parametrize(
    "options",
    [
        {"duration": 0},
        {"duration": None, "bytes_to_send": 1},
        {"duration": None, "blocks_to_send": 1},
    ],
)
def test_standalone_count_and_unlimited_modes_require_worker(options):
    """Non-duration modes retain explicit admission and run through isolation."""
    config = ClientConfig("localhost", **options)
    assert requires_worker(config)
    assert config_from_dict(config_to_dict(config)) == config


@pytest.mark.parametrize(
    "options",
    [
        {"server_affinity": 0},
        {"username": "user"},
        {"rsa_public_key_path": "public.pem"},
        {"use_pkcs1_padding": True},
        {"payload_file": "payload.bin", "repeating_payload": True},
    ],
)
def test_dependent_settings_require_complete_intent(options):
    """Dependent options cannot silently borrow missing credential or affinity state."""
    with pytest.raises(ValueError):
        ClientConfig("localhost", **options)


@pytest.mark.parametrize(
    "value", [(1, 1), [1, 1, 1], (True, 1, 1), (-1, 1, 1), (1, 1, 2**31), (2, 1, 2)]
)
def test_control_keepalive_rejects_invalid_shape_and_relationship(value):
    """Keepalive uses three bounded integers with the native retry relationship."""
    with pytest.raises((TypeError, ValueError), match="control_keepalive"):
        ClientConfig("localhost", control_keepalive=value)


@pytest.mark.parametrize("value", [(0, 0, 0), (0, 2, 3), (30, 2, 3), (100, 0, 0)])
def test_control_keepalive_preserves_native_default_selectors(value):
    """Zero components select native defaults; the tuple is not a disable flag."""
    config = ClientConfig("localhost", control_keepalive=value)
    assert config_from_dict(config_to_dict(config)) == config


@pytest.mark.parametrize("value", [["127.0.0.1"], ("",), ("host\0tail",), (123,)])
def test_sctp_bind_addresses_require_a_tuple_of_safe_text(value):
    """Repeated association addresses are typed rather than implicitly iterable text."""
    with pytest.raises((TypeError, ValueError), match="sctp_bind_addresses"):
        ClientConfig("localhost", protocol=Protocol.SCTP, sctp_bind_addresses=value)


@pytest.mark.parametrize("value", [None, 4, True, "IPv4", "inet", ""])
def test_address_family_rejects_undeclared_values(value):
    """Only documented address-family enum values are accepted."""
    with pytest.raises((TypeError, ValueError), match="address_family"):
        ClientConfig("localhost", address_family=value)


def test_json_snapshot_is_detached_and_restores_declared_tuples():
    """JSON arrays regain tuple semantics without mutating their original mapping."""
    values: dict[str, Any] = {
        "server": IPv6Address("::1"),
        "protocol": "sctp",
        "address_family": "ipv6",
        "sctp_bind_addresses": ("::1", "::2"),
        "control_keepalive": (30, 2, 3),
    }
    config = ClientConfig(**values)
    snapshot = config_to_dict(config)
    assert snapshot["server"] == "::1"
    assert snapshot["sctp_bind_addresses"] == ["::1", "::2"]
    restored = config_from_dict(snapshot)
    assert restored.sctp_bind_addresses == ("::1", "::2")
    snapshot["sctp_bind_addresses"].append("::3")
    assert restored.sctp_bind_addresses == config.sctp_bind_addresses == ("::1", "::2")
    assert isinstance(snapshot["control_keepalive"], list)


def test_compact_snapshot_preserves_legacy_fields_and_selected_extensions():
    """Default extensions can be omitted without changing admitted native intent."""
    default = ClientConfig("localhost")
    snapshot = config_to_dict(default, compact=True)
    assert snapshot.keys() == LEGACY_CONFIG_FIELDS
    assert config_from_dict(snapshot) == default
    changed = replace(default, bind_address="127.0.0.1", control_keepalive=(0, 0, 0))
    compact = config_to_dict(changed, compact=True)
    assert compact.keys() == LEGACY_CONFIG_FIELDS | {"bind_address", "control_keepalive"}
    assert config_from_dict(compact) == changed
    assert config_to_dict(default).keys() == {item.name for item in fields(ClientConfig)}


@pytest.mark.parametrize("options", [{"mptcp": True}, {"json_stream": True}, {"title": "run"}])
def test_extended_features_route_to_worker(options):
    """Legacy simple configurations stay direct while extended controls use isolation."""
    assert not requires_worker(ClientConfig("localhost"))
    assert requires_worker(ClientConfig("localhost", **options))


def test_config_codec_rejects_unknown_fields_and_restores_validation():
    """Deserialization cannot bypass constructor validation or add a password field."""
    snapshot = config_to_dict(ClientConfig("localhost"))
    with pytest.raises(TypeError, match="password"):
        config_from_dict({**snapshot, "password": "must-not-be-retained"})
    with pytest.raises(TypeError, match="client_port"):
        config_from_dict({**snapshot, "client_port": True})
