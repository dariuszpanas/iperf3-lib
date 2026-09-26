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
SCENARIOS = (
    "tcp-forward",
    "tcp-reverse",
    "tcp-bidirectional",
    "udp",
    "udp-reverse",
    "udp-bidirectional",
    "sctp-forward",
    "sctp-reverse",
    "sctp-bidirectional",
    "tcp-warmup",
)
MEASUREMENTS = (
    ("bits_per_second", "bits_per_second"),
    ("retransmits", "retransmits"),
    ("lost_percent", "lost_percent"),
    ("jitter_ms", "jitter_ms"),
    ("bytes", "bytes"),
    ("duration_seconds", "seconds"),
    ("start_seconds", "start"),
    ("end_seconds", "end"),
    ("packets", "packets"),
    ("lost_packets", "lost_packets"),
    ("omitted", "omitted"),
)


def native_measurements(native, scenario):
    """Use captured values except source-proven unsupported SCTP retransmissions."""
    return tuple(
        None if key == "retransmits" and scenario.startswith("sctp") else native.get(key)
        for _, key in MEASUREMENTS
    )


def normalized_measurements(stats):
    """Read the same unit-preserving canonical measurements across summary and interval models."""
    return tuple(getattr(stats, field) for field, _ in MEASUREMENTS)


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

    is_reverse = scenario.endswith("-reverse")
    is_bidirectional = scenario.endswith("-bidirectional")
    primary = "server_to_client" if is_reverse else "client_to_server"
    directions = [primary, "server_to_client"] if is_bidirectional else [primary]
    assert [flow.direction for flow in result.flows] == directions
    assert result.bidirectional == is_bidirectional
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
            assert normalized_measurements(stats) == native_measurements(native, scenario)

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
                    "aggregate",
                    native_measurements(native, scenario),
                )
            )
        for stream in interval["streams"]:
            assert stream["socket"] in connected_sockets
            local_sender = stream["sender"]
            if is_bidirectional:
                direction = (
                    "client_to_server" if local_sender == (role == "client") else "server_to_client"
                )
            else:
                direction = primary
                assert local_sender == ((role == "client") != is_reverse)
            expected.append(
                (
                    stream["socket"],
                    direction,
                    "sender" if local_sender else "receiver",
                    "stream",
                    native_measurements(stream, scenario),
                )
            )
    observed = [
        (
            interval.stream_id,
            interval.direction,
            interval.observation,
            interval.scope,
            normalized_measurements(interval),
        )
        for interval in result.intervals
    ]
    assert Counter(observed) == Counter(expected)
    assert {
        interval.stream_id for interval in result.intervals if interval.stream_id is not None
    } == connected_sockets

    assert len(result.streams) == len(raw["end"]["streams"])
    for index, (stream, native_stream) in enumerate(
        zip(result.streams, raw["end"]["streams"], strict=True)
    ):
        native_part = next(iter(native_stream.values()))
        assert stream.stream_id == native_part["socket"]
        direction = (
            "client_to_server"
            if native_part["sender"] == (role == "client")
            else "server_to_client"
        )
        assert stream.direction == direction
        if scenario.startswith("udp"):
            assert stream.sender is None and stream.receiver is None
            assert stream.unattributed is not None
            assert stream.unattributed.observation is None
            assert normalized_measurements(stream.unattributed) == native_measurements(
                native_stream["udp"], scenario
            )
            assert (
                result.availability[f"/streams/{index}/unattributed/observation"].state == "unknown"
            )
        else:
            assert stream.unattributed is None
            for observation in ("sender", "receiver"):
                part = getattr(stream, observation)
                assert part is not None
                assert part.direction == direction and part.observation == observation
                assert normalized_measurements(part) == native_measurements(
                    native_stream[observation], scenario
                )

    assert result.execution is not None and result.execution.status == "completed"
    assert result.execution.native_version == f"iperf {version}"
    assert result.execution.native_system_info == raw["start"]["system_info"]
    assert result.execution.configuration.requested is None
    assert result.execution.configuration.effective["protocol"].value == scenario.split("-")[0]
    assert result.completed_at_seconds is None
    assert result.execution.timing.completed_at_seconds is None
    assert (
        result.execution.timing.native_started_at_seconds == raw["start"]["timestamp"]["timesecs"]
    )
    assert result.execution.timing.requested_duration_seconds == 1
    if scenario == "tcp-warmup":
        assert result.execution.configuration.effective["omit"].value == 1
        assert {interval.omitted for interval in result.intervals} == {True, False}
    if scenario.startswith("sctp"):
        assert result.availability
        assert {
            value.state
            for path, value in result.availability.items()
            if path.endswith("/retransmits")
        } == {"unsupported"}
        assert any(d.code == "measurement.unsupported" for d in result.diagnostics)


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
