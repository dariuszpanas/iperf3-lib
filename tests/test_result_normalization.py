"""Tests for normalized result models and native JSON mapping."""

import json

import pytest

from iperf3_lib.result import result_from_iperf_json


def test_normalizes_summary_direction_and_intervals():
    """Preserve endpoint roles and interval units from regular iperf JSON."""
    result = result_from_iperf_json(
        {
            "start": {
                "timestamp": {"timesecs": 1_700_000_000},
                "test_start": {"protocol": "TCP", "duration": 10, "reverse": 0},
            },
            "intervals": [
                {
                    "sum": {"start": 0, "end": 1, "bits_per_second": 8_000_000},
                    "streams": [
                        {
                            "socket": 1,
                            "start": 0,
                            "end": 1,
                            "bits_per_second": 4_000_000,
                            "sender": False,
                        }
                    ],
                }
            ],
            "end": {
                "sum_sent": {"bits_per_second": 8_000_000, "retransmits": 2},
                "sum_received": {"bits_per_second": 7_000_000},
            },
        }
    )

    assert result.protocol == "tcp"
    assert result.started_at_seconds == 1_700_000_000
    assert result.duration_seconds == 10
    assert result.flows[0].direction == "client_to_server"
    assert result.flows[0].sender.observation == "sender"
    assert result.flows[0].receiver.observation == "receiver"
    assert [interval.bits_per_second for interval in result.intervals] == [
        8_000_000,
        4_000_000,
    ]
    assert result.intervals[1].observation == "receiver"
    assert json.loads(json.dumps(result.to_dict()))["protocol"] == "tcp"


def test_reverse_and_bidirectional_are_separate_flow_semantics():
    """Normalize reverse and simultaneous bidirectional directions distinctly."""
    reverse = result_from_iperf_json(
        {"start": {"test_start": {"reverse": 1}}, "end": {"sum_received": {"bits_per_second": 1}}}
    )
    bidir = result_from_iperf_json(
        {
            "start": {"test_start": {"bidirectional": 1}},
            "intervals": [
                {
                    "sum": {"start": 0, "end": 1, "bits_per_second": 4},
                    "sum_bidir_reverse": {
                        "start": 0,
                        "end": 1,
                        "bits_per_second": 3,
                    },
                }
            ],
            "end": {
                "sum_received": {"bits_per_second": 2},
                "sum_sent_bidir_reverse": {"bits_per_second": 3},
                "sum_received_bidir_reverse": {"bits_per_second": 2.5},
            },
        }
    )

    assert [flow.direction for flow in reverse.flows] == ["server_to_client"]
    assert reverse.bidirectional is False
    assert [flow.direction for flow in bidir.flows] == [
        "client_to_server",
        "server_to_client",
    ]
    assert bidir.flows[1].sender.bits_per_second == 3
    assert bidir.flows[1].receiver.bits_per_second == 2.5
    assert [interval.direction for interval in bidir.intervals] == [
        "client_to_server",
        "server_to_client",
    ]


def test_missing_summary_is_not_normalized_to_zero_measurement():
    """Keep missing native measurements distinct from measured zero values."""
    result = result_from_iperf_json({"end": {}})

    assert result.end is None
    assert result.flows[0].sender is None
    assert result.flows[0].receiver is None


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"start": []},
        {"end": {"sum_sent": []}},
        {"intervals": {}},
        {"intervals": [{"streams": ["invalid"]}]},
        {"end": {"sum_sent": {"bits_per_second": "fast"}}},
        {"intervals": [{"sum": {"start": float("inf")}}]},
    ],
)
def test_rejects_malformed_or_non_numeric_native_json(raw):
    """Reject malformed JSON shapes and values instead of hiding bad data."""
    with pytest.raises(ValueError):
        result_from_iperf_json(raw)
