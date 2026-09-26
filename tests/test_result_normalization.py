"""Tests for normalized result models and native JSON mapping."""

import json

import pytest

from iperf3_lib.result import EndStats, FlowStats, Result, SumStats, result_from_iperf_json


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


@pytest.mark.parametrize("reverse", [False, True])
def test_direction_is_consistent_across_flow_end_and_interval_views(reverse):
    """Use one direction before assigning either normalized or compatibility views."""
    raw = {
        "start": {"test_start": {"reverse": reverse}},
        "end": {
            "sum_sent": {"bits_per_second": 8_000_000},
            "sum_received": {"bits_per_second": 7_000_000},
        },
        "intervals": [{"sum": {"start": 0, "end": 1, "bits_per_second": 8_000_000}}],
    }

    result = result_from_iperf_json(raw)
    expected = "server_to_client" if reverse else "client_to_server"
    flow = result.flows[0]
    assert result.end is not None
    assert flow.sender is not None
    assert flow.receiver is not None
    assert flow.direction == flow.sender.direction == flow.receiver.direction == expected
    assert result.end.sum_sent is flow.sender
    assert result.end.sum_received is flow.receiver
    assert flow.sender.observation == "sender"
    assert flow.receiver.observation == "receiver"
    assert result.intervals[0].direction == expected
    assert result.summary_mbps == 8.0


@pytest.mark.parametrize("rate", [None, 0, 8_000_000])
def test_missing_and_zero_summary_bitrates_remain_distinct(rate):
    """Preserve optional bitrate values even when other endpoint measurements exist."""
    raw = {"end": {"sum_received": {"bits_per_second": rate, "lost_percent": 0}}}

    result = result_from_iperf_json(raw)

    assert result.ok
    assert result.end is not None
    assert result.end.sum_received is not None
    assert result.end.sum_received.bits_per_second == rate
    assert result.end.sum_received.lost_percent == 0
    assert result.to_dict()["end"]["sum_received"]["bits_per_second"] == rate
    missing_rate = [d for d in result.diagnostics if "bits_per_second is missing" in d.message]
    assert bool(missing_rate) is (rate is None)


@pytest.mark.parametrize("source", ["end", "flows"])
@pytest.mark.parametrize(
    ("sender_rate", "receiver_rate", "expected"),
    [(None, None, 0), (None, 8_000_000, 8), (0, 8_000_000, 0), (8_000_000, 7_000_000, 8)],
)
def test_summary_mbps_prefers_first_available_value_including_zero(
    source, sender_rate, receiver_rate, expected
):
    """Keep the legacy absent fallback while avoiding receiver substitution for real zero."""
    sender = SumStats(sender_rate)
    receiver = SumStats(receiver_rate)
    result = Result(ok=True)
    if source == "end":
        result.end = EndStats(sender, receiver)
    else:
        result.flows = [FlowStats("client_to_server", sender, receiver)]

    assert result.summary_mbps == expected


@pytest.mark.parametrize("error", ["unable to connect to server", ""])
def test_saved_native_error_preserves_partial_evidence_and_is_failed(error):
    """Retain native errors and measurements without claiming successful completion."""
    raw = {
        "start": {"timestamp": {"timesecs": 100}, "test_start": {"duration": 10, "reverse": 1}},
        "end": {"sum_received": {"bits_per_second": 8_000_000}},
        "error": error,
    }

    result = result_from_iperf_json(raw)

    assert not result.ok
    assert result.error == error
    assert result.raw is raw
    assert result.started_at_seconds == 100
    assert result.completed_at_seconds is None
    assert result.end is not None
    assert result.end.sum_received is not None
    assert result.end.sum_received.bits_per_second == 8_000_000
    assert any(d.severity == "error" for d in result.diagnostics)


@pytest.mark.parametrize(
    "raw",
    [{}, {"start": {}}, {"end": {}}, {"end": {"sum_sent": {}}}, {"intervals": [{"sum": {}}]}],
)
def test_incomplete_native_json_cannot_report_success(raw):
    """Fail closed when no end-of-test endpoint measurements establish a result."""
    result = result_from_iperf_json(raw)

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("Incomplete native JSON")
    assert result.raw is raw
    assert any(d.severity == "error" and "Incomplete" in d.message for d in result.diagnostics)


@pytest.mark.parametrize("measurement", [{"bytes": 0}, {"seconds": 1}, {"retransmits": 0}])
def test_partial_end_measurements_remain_usable_without_bitrate(measurement):
    """Allow a measured endpoint result without manufacturing its absent throughput."""
    result = result_from_iperf_json({"end": {"sum_sent": measurement}})

    assert result.ok
    assert result.end is not None
    assert result.end.sum_sent is not None
    assert result.end.sum_sent.bits_per_second is None
    assert any("bits_per_second is missing" in d.message for d in result.diagnostics)


def test_missing_interval_boundaries_observation_and_rate_stay_unknown():
    """Represent absent interval measurements and sender flags without zero defaults."""
    result = result_from_iperf_json(
        {"start": {"test_start": {"reverse": 0}}, "intervals": [{"sum": {}, "streams": [{}]}]}
    )

    assert len(result.intervals) == 2
    for interval in result.intervals:
        assert interval.start_seconds is None
        assert interval.end_seconds is None
        assert interval.bits_per_second is None
        assert interval.observation is None
    assert any("boundaries are missing" in d.message for d in result.diagnostics)
    assert any("observation is unknown" in d.message for d in result.diagnostics)


@pytest.mark.parametrize(
    ("marker", "role", "expected"),
    [
        ("connecting_to", "client", ["client_to_server", "server_to_client"]),
        ("accepted_connection", "server", ["server_to_client", "client_to_server"]),
    ],
)
def test_bidirectional_stream_directions_use_reporting_role_and_local_sender(
    marker, role, expected
):
    """Derive direction from proven native endpoint role rather than stream ordering."""
    raw = {
        "start": {marker: {"host": "127.0.0.1", "port": 5201}, "test_start": {"bidir": 1}},
        "intervals": [{"streams": [{"socket": 9, "sender": True}, {"socket": 3, "sender": False}]}],
    }

    result = result_from_iperf_json(raw)

    assert result.reporting_role == role
    assert [i.direction for i in result.intervals] == expected
    assert [i.observation for i in result.intervals] == ["sender", "receiver"]


def test_socket_provenance_fills_missing_interval_sender_flag_without_using_endpoint_key():
    """Interpret nested sender flags as local roles even under a receiver observation."""
    raw = {
        "start": {"test_start": {"bidir": 1}},
        "end": {
            "streams": [
                {
                    "sender": {"socket": 7, "sender": False},
                    "receiver": {"socket": 7, "sender": False},
                },
                {"udp": {"socket": 9, "sender": True}},
            ]
        },
        "intervals": [{"streams": [{"socket": 9}, {"socket": 7}]}],
    }

    result = result_from_iperf_json(raw, reporting_role="client")

    assert result.reporting_role == "client"
    assert [i.direction for i in result.intervals] == ["client_to_server", "server_to_client"]
    assert [i.observation for i in result.intervals] == ["sender", "receiver"]


def test_bidirectional_stream_direction_without_reporting_role_is_explicitly_unknown():
    """Do not treat a generic connected socket or sender flag as client provenance."""
    raw = {
        "start": {"connected": [{"socket": 7}], "test_start": {"bidir": 1}},
        "intervals": [{"streams": [{"socket": 7, "sender": True}]}],
    }

    result = result_from_iperf_json(raw)

    assert result.reporting_role is None
    assert result.intervals[0].direction == "unknown"
    assert result.intervals[0].observation == "sender"
    assert any("direction is unknown" in d.message for d in result.diagnostics)


@pytest.mark.parametrize(
    ("markers", "hint"),
    [
        ({"connecting_to": {}, "accepted_connection": {}}, None),
        ({"accepted_connection": {}}, "client"),
    ],
)
def test_conflicting_reporting_role_provenance_is_not_silently_overridden(markers, hint):
    """Retain ambiguous native evidence without assigning an invented report role."""
    raw = {
        "start": {**markers, "test_start": {"bidir": 1}},
        "intervals": [{"streams": [{"socket": 7, "sender": True}]}],
    }

    result = result_from_iperf_json(raw, reporting_role=hint)

    assert result.reporting_role is None
    assert result.intervals[0].direction == "unknown"
    assert any("Conflicting reporting-role" in d.message for d in result.diagnostics)


def test_conflicting_socket_provenance_marks_all_affected_intervals_unknown():
    """Reject contradictory role assignments for one socket across native records."""
    raw = {
        "start": {"test_start": {"bidir": 1}},
        "end": {"streams": [{"receiver": {"socket": 7, "sender": False}}]},
        "intervals": [{"streams": [{"socket": 7, "sender": True}]}, {"streams": [{"socket": 7}]}],
    }

    result = result_from_iperf_json(raw, reporting_role="client")

    assert all(i.direction == "unknown" and i.observation is None for i in result.intervals)
    assert any("Conflicting native sender provenance" in d.message for d in result.diagnostics)


def test_known_reporting_role_without_local_sender_cannot_assign_bidirectional_stream():
    """A reporting endpoint alone does not identify a bidirectional stream's flow."""
    result = result_from_iperf_json(
        {"start": {"test_start": {"bidir": 1}}, "intervals": [{"streams": [{"socket": 7}]}]},
        reporting_role="server",
    )

    assert result.intervals[0].direction == "unknown"
    assert result.intervals[0].observation is None


def test_unidirectional_stream_role_conflict_is_explicitly_unknown():
    """Flag a local receiver that contradicts a declared client-to-server client run."""
    result = result_from_iperf_json(
        {
            "start": {"test_start": {"reverse": 0}},
            "intervals": [{"streams": [{"socket": 7, "sender": False}]}],
        },
        reporting_role="client",
    )

    assert result.intervals[0].direction == "unknown"
    assert any("conflicts with test direction" in d.message for d in result.diagnostics)


@pytest.mark.parametrize(
    "raw",
    [
        {"error": None},
        {"error": 1},
        {"start": {"connecting_to": []}},
        {"start": {"accepted_connection": "server"}},
        {"end": {"streams": {}}},
        {"end": {"streams": [1]}},
        {"end": {"streams": [{"sender": []}]}},
        {"end": {"streams": [{"sender": {"socket": True}}]}},
        {"intervals": [{"streams": [{"sender": "yes"}]}]},
        {"intervals": [{"sum": {"sender": 2}}]},
    ],
)
def test_rejects_malformed_error_and_provenance_shapes(raw):
    """Keep malformed native input distinct from missing but preservable evidence."""
    with pytest.raises(ValueError):
        result_from_iperf_json(raw)


@pytest.mark.parametrize("role", ["c", "s", "unknown", 1, False])
def test_rejects_invalid_explicit_reporting_role(role):
    """Require the parser caller to name an actual endpoint role."""
    with pytest.raises(ValueError, match="reporting_role"):
        result_from_iperf_json({}, reporting_role=role)


@pytest.mark.parametrize(
    "raw",
    [
        {
            "start": {"test_start": {"bidir": 0, "reverse": 0}},
            "end": {
                "sum_sent": {"bits_per_second": 1},
                "sum_sent_bidir_reverse": {"bits_per_second": 2},
            },
        },
        {"start": {"test_start": {"bidir": 1, "reverse": 1}}},
        {
            "start": {"test_start": {"reverse": 1}},
            "end": {"sum_received_bidir_reverse": {"bits_per_second": 1}},
        },
        {
            "start": {"test_start": {"bidir": 0}},
            "intervals": [{"sum_bidir_reverse": {"bits_per_second": 1}}],
        },
        {
            "start": {"test_start": {"reverse": 1}},
            "intervals": [{"sum_bidir_reverse": {"bits_per_second": 1}}],
        },
    ],
)
def test_rejects_contradictory_native_direction_modes_and_end_evidence(raw):
    """Do not silently discard reverse-flow data or override incompatible mode flags."""
    with pytest.raises(ValueError, match="conflict"):
        result_from_iperf_json(raw)


def test_absent_bidirectional_flag_can_use_unambiguous_reverse_flow_end_evidence():
    """Retain both observed flows when their native summary keys establish the mode."""
    result = result_from_iperf_json(
        {
            "end": {
                "sum_sent": {"bits_per_second": 1},
                "sum_sent_bidir_reverse": {"bits_per_second": 2},
            }
        }
    )

    assert result.ok
    assert result.bidirectional
    assert [flow.direction for flow in result.flows] == ["client_to_server", "server_to_client"]
    assert any("mode was inferred" in d.message for d in result.diagnostics)


def test_null_reverse_summaries_do_not_imply_bidirectional_mode():
    """Do not infer additional traffic from an explicitly absent summary."""
    result = result_from_iperf_json({"end": {"sum_sent_bidir_reverse": None}})

    assert not result.bidirectional
    assert not result.ok


def test_reverse_interval_without_direction_metadata_retains_evidence_with_diagnostic():
    """Preserve partial interval evidence while making missing mode metadata explicit."""
    result = result_from_iperf_json({"intervals": [{"sum_bidir_reverse": {"bits_per_second": 1}}]})

    assert not result.ok
    assert result.intervals[0].direction == "server_to_client"
    assert result.intervals[0].bits_per_second == 1
    assert any("without bidirectional mode metadata" in d.message for d in result.diagnostics)
