"""Unit tests for ClientConfig and Protocol validation."""

from ipaddress import IPv4Address, ip_address

import pytest

from iperf3_lib.config import MAX_BLOCK_SIZE, ClientConfig, Protocol


def test_config_defaults():
    """Test that ClientConfig sets expected defaults."""
    cfg = ClientConfig(server="127.0.0.1")
    assert cfg.port == 5201
    assert cfg.protocol == Protocol.TCP
    assert cfg.duration == 10
    assert not cfg.bidirectional


def test_config_validation():
    """Test that ClientConfig rejects an out-of-range tos value."""
    with pytest.raises(ValueError):
        ClientConfig(server="127.0.0.1", tos=300)  # >255 invalid


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": 0},
        {"port": 65536},
        {"duration": 0},
        {"duration": 86401},
        {"parallel": 0},
        {"parallel": 129},
        {"omit": 601},
        {"rate": -1},
        {"blksize": 0},
        {"blksize": MAX_BLOCK_SIZE + 1},
        {"protocol": Protocol.UDP, "blksize": 15},
        {"protocol": Protocol.UDP, "blksize": 65508},
        {"reverse": True, "bidirectional": True},
        {"unknown_option": True},
    ],
)
def test_config_rejects_invalid_limits_and_combinations(kwargs):
    """Reject values that libiperf cannot safely or consistently apply."""
    with pytest.raises((TypeError, ValueError)):
        ClientConfig(server="127.0.0.1", **kwargs)


def test_compatibility_fields_remain_configurable():
    """Retain legacy fields even though the direct backend rejects them at run time."""
    cfg = ClientConfig(server="127.0.0.1", mptcp=True, json_stream=True)

    assert cfg.mptcp is True
    assert cfg.json_stream is True


def test_config_accepts_maximum_omit_time():
    """Accept libiperf's documented maximum omit duration."""
    cfg = ClientConfig(server="127.0.0.1", omit=600)

    assert cfg.omit == 600


def test_config_normalizes_protocol_and_accepts_ip_address_objects():
    """Preserve string protocol convenience and stdlib address-object support."""
    cfg = ClientConfig(server=ip_address("192.0.2.1"), protocol="udp")

    assert cfg.server == IPv4Address("192.0.2.1")
    assert cfg.protocol is Protocol.UDP


@pytest.mark.parametrize("kwargs", [{"port": True}, {"duration": "10"}, {"reverse": 1}])
def test_config_rejects_implicit_coercion(kwargs):
    """Reject booleans and numeric strings instead of silently coercing them."""
    with pytest.raises(TypeError):
        ClientConfig(server="127.0.0.1", **kwargs)
