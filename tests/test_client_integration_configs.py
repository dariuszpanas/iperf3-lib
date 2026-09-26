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
@pytest.mark.parametrize("scenario", ["tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp"])
def test_client_normalizes_native_flow_and_stream_directions(iperf3_server, scenario: str) -> None:
    """Match nested flow summaries and every per-stream interval to actual native JSON."""
    host, port = iperf3_server
    result = Client(
        ClientConfig(
            server=host,
            port=port,
            duration=1,
            parallel=2,
            protocol=Protocol.UDP if scenario == "udp" else Protocol.TCP,
            reverse=scenario == "tcp-reverse",
            bidirectional=scenario == "tcp-bidirectional",
            rate=4_000_000,
            blksize=1200 if scenario == "udp" else None,
        )
    ).run()

    assert result.ok, result.error
    assert result.reporting_role == "client"
    assert result.raw["start"]["test_start"]["bidir"] == (scenario == "tcp-bidirectional")
    assert result.raw["start"]["test_start"]["reverse"] == (scenario == "tcp-reverse")
    assert result.raw["start"]["test_start"]["num_streams"] == 2
    assert result.bidirectional == (scenario == "tcp-bidirectional")
    primary = "server_to_client" if scenario == "tcp-reverse" else "client_to_server"
    flows = {flow.direction: flow for flow in result.flows}
    expected_flows = [(primary, "")]
    if scenario == "tcp-bidirectional":
        expected_flows.append(("server_to_client", "_bidir_reverse"))
    assert set(flows) == {direction for direction, _ in expected_flows}
    for direction, suffix in expected_flows:
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
    assert result.end is not None
    assert result.end.sum_sent == flows[primary].sender
    assert result.end.sum_received == flows[primary].receiver
    native_streams = [
        stream for interval in result.raw["intervals"] for stream in interval["streams"]
    ]
    normalized_streams = [
        interval for interval in result.intervals if interval.stream_id is not None
    ]
    assert len(normalized_streams) == len(native_streams)
    for normalized, native in zip(normalized_streams, native_streams, strict=True):
        direction = (
            ("client_to_server" if native["sender"] else "server_to_client")
            if scenario == "tcp-bidirectional"
            else primary
        )
        assert normalized.stream_id == native["socket"]
        assert normalized.direction == direction
        assert normalized.observation == ("sender" if native["sender"] else "receiver")
        assert normalized.start_seconds == native["start"]
        assert normalized.end_seconds == native["end"]
        assert normalized.bits_per_second == native["bits_per_second"]
