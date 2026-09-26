"""Real-libiperf integration tests for client protocol and option application."""

from __future__ import annotations

from typing import Any

import pytest

from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.iperf_client import Client

CASES: list[tuple[str, Protocol, dict[str, Any], dict[str, Any]]] = [
    ("tcp_defaults", Protocol.TCP, {}, {"protocol": "TCP"}),
    (
        "udp_defaults",
        Protocol.UDP,
        {},
        {"protocol": "UDP", "target_bitrate": 1024 * 1024},
    ),
    (
        "sctp_defaults",
        Protocol.SCTP,
        {},
        {"protocol": "SCTP", "blksize": 64 * 1024},
    ),
    (
        "tcp_combined_settings",
        Protocol.TCP,
        {"parallel": 2, "blksize": 1500, "tos": 16, "omit": 1, "reverse": True},
        {
            "protocol": "TCP",
            "num_streams": 2,
            "blksize": 1500,
            "tos": 16,
            "omit": 1,
            "reverse": 1,
        },
    ),
    (
        "tcp_bidirectional",
        Protocol.TCP,
        {"bidirectional": True},
        {"protocol": "TCP", "bidir": 1},
    ),
    (
        "tcp_rate",
        Protocol.TCP,
        {"rate": 500_000},
        {"protocol": "TCP", "target_bitrate": 500_000},
    ),
    (
        "udp_rate",
        Protocol.UDP,
        {"rate": 500_000},
        {"protocol": "UDP", "target_bitrate": 500_000},
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("_name", "protocol", "options", "expected"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_client_applies_protocol_and_settings(
    iperf3_server,
    capfd,
    _name: str,
    protocol: Protocol,
    options: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Verify successful native runs report the requested settings without stdout noise."""
    host, port = iperf3_server
    cfg = ClientConfig(
        server=host,
        port=port,
        protocol=protocol,
        duration=1,
        **options,
    )

    result = Client(cfg).run()
    captured = capfd.readouterr()

    assert captured.out == ""
    assert result.ok, result.error

    test_start = result.raw["start"]["test_start"]
    assert test_start["duration"] == 1
    if protocol is Protocol.UDP:
        assert 16 <= test_start["blksize"] <= 65507
    for key, value in expected.items():
        assert test_start[key] == value


@pytest.mark.integration
def test_client_normalizes_both_directions_from_native_bidirectional_run(iperf3_server) -> None:
    """Preserve native bidir flags and all four endpoint summaries in normalized results."""
    host, port = iperf3_server
    result = Client(
        ClientConfig(server=host, port=port, duration=1, bidirectional=True, rate=4_000_000)
    ).run()

    assert result.ok, result.error
    assert result.raw["start"]["test_start"]["bidir"] == 1
    assert result.bidirectional is True
    assert len(result.flows) == 2
    flows = {flow.direction: flow for flow in result.flows}
    assert set(flows) == {"client_to_server", "server_to_client"}
    for direction, suffix in (("client_to_server", ""), ("server_to_client", "_bidir_reverse")):
        flow = flows[direction]
        for observer, native_key in (("sender", "sum_sent"), ("receiver", "sum_received")):
            stats = getattr(flow, observer)
            assert stats is not None
            assert stats.direction == direction
            assert stats.observation == observer
            assert stats.bits_per_second > 0
            assert (
                stats.bits_per_second
                == result.raw["end"][f"{native_key}{suffix}"]["bits_per_second"]
            )
