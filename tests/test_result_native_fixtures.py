"""Normalize captured minimum/latest libiperf JSON from both reporting endpoints."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

import pytest

from iperf3_lib.result import result_from_iperf_json

FIXTURES = Path(__file__).parent / "fixtures" / "native"
VERSIONS = ("3.19.1", "3.21")
SCENARIOS = ("tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp")


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("role", ["client", "server"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_native_endpoint_fixture_preserves_summary_and_interval_semantics(
    version: str, role: Literal["client", "server"], scenario: str
) -> None:
    """Match every flow summary and stream interval to independent native evidence."""
    raw = json.loads((FIXTURES / version / f"{scenario}-{role}.json").read_text(encoding="utf-8"))
    result = result_from_iperf_json(raw, reporting_role=role)
    assert result.ok, result.error
    assert result.raw == raw
    assert result.reporting_role == role
    assert raw["start"]["version"] == f"iperf {version}"
    assert ("connecting_to" in raw["start"]) == (role == "client")
    assert ("accepted_connection" in raw["start"]) == (role == "server")
    assert raw["start"]["test_start"]["num_streams"] == 2
    assert raw["start"]["test_start"]["target_bitrate"] == 4_000_000

    primary = "server_to_client" if scenario == "tcp-reverse" else "client_to_server"
    directions = [primary, "server_to_client"] if scenario == "tcp-bidirectional" else [primary]
    assert [flow.direction for flow in result.flows] == directions
    assert result.bidirectional == (scenario == "tcp-bidirectional")
    for index, flow in enumerate(result.flows):
        suffix = "_bidir_reverse" if index else ""
        for observation, key in (("sender", "sum_sent"), ("receiver", "sum_received")):
            native = raw["end"].get(key + suffix)
            stats = getattr(flow, observation)
            if native is None:
                assert stats is None
                continue
            assert stats is not None
            assert stats.direction == flow.direction
            assert stats.observation == observation
            for field in ("bits_per_second", "retransmits", "lost_percent", "jitter_ms"):
                assert getattr(stats, field) == native.get(field)

    assert result.end is not None
    for observation, key in (("sender", "sum_sent"), ("receiver", "sum_received")):
        stats = getattr(result.end, key)
        assert stats == getattr(result.flows[0], observation)

    expected = []
    connected_sockets = {connection["socket"] for connection in raw["start"]["connected"]}
    for interval in raw["intervals"]:
        for key, direction in (("sum", primary), ("sum_bidir_reverse", "server_to_client")):
            if key not in interval:
                continue
            native = interval[key]
            expected.append(
                (
                    None,
                    direction,
                    "sender" if native["sender"] else "receiver",
                    native["start"],
                    native["end"],
                    native["bits_per_second"],
                )
            )
        for stream in interval["streams"]:
            assert stream["socket"] in connected_sockets
            local_sender = stream["sender"]
            if scenario == "tcp-bidirectional":
                direction = (
                    "client_to_server" if local_sender == (role == "client") else "server_to_client"
                )
            else:
                direction = primary
                assert local_sender == ((role == "client") != (scenario == "tcp-reverse"))
            expected.append(
                (
                    stream["socket"],
                    direction,
                    "sender" if local_sender else "receiver",
                    stream["start"],
                    stream["end"],
                    stream["bits_per_second"],
                )
            )
    observed = [
        (
            interval.stream_id,
            interval.direction,
            interval.observation,
            interval.start_seconds,
            interval.end_seconds,
            interval.bits_per_second,
        )
        for interval in result.intervals
    ]
    assert Counter(observed) == Counter(expected)
    assert {
        interval.stream_id for interval in result.intervals if interval.stream_id is not None
    } == connected_sockets


@pytest.mark.parametrize("version", VERSIONS)
def test_bidirectional_socket_role_mapping_does_not_depend_on_array_order(version: str) -> None:
    """Shuffling real stream arrays must retain each socket's direction and observation."""
    raw = json.loads(
        (FIXTURES / version / "tcp-bidirectional-client.json").read_text(encoding="utf-8")
    )
    expected = result_from_iperf_json(raw, reporting_role="client")
    raw["start"]["connected"].reverse()
    raw["end"]["streams"].reverse()
    for interval in raw["intervals"]:
        interval["streams"].reverse()
    shuffled = result_from_iperf_json(raw, reporting_role="client")

    def identify(item):
        return (
            item.start_seconds,
            item.end_seconds,
            item.direction,
            item.observation,
            item.bits_per_second,
        )

    assert {
        interval.stream_id: identify(interval)
        for interval in shuffled.intervals
        if interval.stream_id is not None
    } == {
        interval.stream_id: identify(interval)
        for interval in expected.intervals
        if interval.stream_id is not None
    }
