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
    assert result.flows[0].sender is not None
    assert result.flows[0].receiver is not None
    assert result.flows[0].sender.observation == "sender"
    assert result.flows[0].receiver.observation == "receiver"
    assert [interval.bits_per_second for interval in result.intervals] == [
        8_000_000,
        4_000_000,
    ]
    assert result.intervals[1].observation == "receiver"
    assert json.loads(json.dumps(result.to_dict()))["protocol"] == "tcp"


@pytest.mark.parametrize("bidirectional_flag", ["bidir", "bidirectional"])
def test_reverse_and_bidirectional_are_separate_flow_semantics(bidirectional_flag):
    """Normalize reverse and simultaneous bidirectional directions distinctly."""
    reverse = result_from_iperf_json(
        {"start": {"test_start": {"reverse": 1}}, "end": {"sum_received": {"bits_per_second": 1}}}
    )
    bidir = result_from_iperf_json(
        {
            "start": {
                "test_start": {
                    "protocol": "TCP",
                    "duration": 1,
                    "reverse": 0,
                    bidirectional_flag: 1,
                }
            },
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
                "sum_sent": {"bits_per_second": 4},
                "sum_received": {"bits_per_second": 2},
                "sum_sent_bidir_reverse": {"bits_per_second": 3},
                "sum_received_bidir_reverse": {"bits_per_second": 2.5},
            },
        }
    )

    assert [flow.direction for flow in reverse.flows] == ["server_to_client"]
    assert reverse.bidirectional is False
    assert bidir.bidirectional is True
    assert [flow.direction for flow in bidir.flows] == [
        "client_to_server",
        "server_to_client",
    ]
    assert bidir.flows[1].sender is not None
    assert bidir.flows[1].receiver is not None
    assert bidir.flows[1].sender.bits_per_second == 3
    assert bidir.flows[1].receiver.bits_per_second == 2.5
    assert [interval.direction for interval in bidir.intervals] == [
        "client_to_server",
        "server_to_client",
    ]


@pytest.mark.parametrize(
    ("native", "alias", "expected"),
    [(1, True, True), (True, 1, True), (0, False, False), (False, 0, False)],
)
def test_accepts_equivalent_bidirectional_flag_spellings(native, alias, expected):
    """Accept both spellings only when they express the same direction mode."""
    result = result_from_iperf_json(
        {"start": {"test_start": {"bidir": native, "bidirectional": alias}}}
    )

    assert result.bidirectional is expected
    assert len(result.flows) == (2 if expected else 1)


@pytest.mark.parametrize(
    "test_start",
    [
        {"bidir": 1, "bidirectional": 0},
        {"bidir": 0, "bidirectional": True},
    ],
)
def test_rejects_conflicting_bidirectional_flag_spellings(test_start):
    """Reject contradictory native and compatibility direction flags."""
    with pytest.raises(ValueError, match="flags conflict"):
        result_from_iperf_json({"start": {"test_start": test_start}})


@pytest.mark.parametrize("name", ["bidir", "bidirectional", "reverse"])
@pytest.mark.parametrize("value", [None, "1", 1.0, 2, -1])
def test_rejects_invalid_direction_flags(name, value):
    """Validate native and compatibility direction flags without coercion."""
    with pytest.raises(ValueError, match="flag must be"):
        result_from_iperf_json({"start": {"test_start": {name: value}}})


@pytest.mark.parametrize(
    "test_start",
    [{"bidir": 1, "bidirectional": "1"}, {"bidir": "1", "bidirectional": 1}],
)
def test_validates_both_bidirectional_flags_when_both_are_present(test_start):
    """Do not let one valid spelling hide a malformed alternative spelling."""
    with pytest.raises(ValueError, match="flag must be"):
        result_from_iperf_json({"start": {"test_start": test_start}})


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
