"""Tests for normalized result models and native JSON mapping."""

import json
from dataclasses import fields

import pytest

from iperf3_lib.result import (
    ConfigurationSnapshot,
    Diagnostic,
    EndStats,
    ExecutionMetadata,
    FlowStats,
    IntervalStats,
    Result,
    SumStats,
    result_from_iperf_json,
)


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


@pytest.mark.parametrize("measurement", [{"bytes": 0}, {"packets": 0}, {"retransmits": 0}])
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


def test_model_additions_preserve_existing_positional_field_order_and_independent_defaults():
    """Append artifact foundations without changing existing dataclass constructors."""
    assert [f.name for f in fields(SumStats)][:6] == [
        "bits_per_second",
        "retransmits",
        "lost_percent",
        "jitter_ms",
        "direction",
        "observation",
    ]
    assert [f.name for f in fields(IntervalStats)][:6] == [
        "start_seconds",
        "end_seconds",
        "bits_per_second",
        "direction",
        "observation",
        "stream_id",
    ]
    assert [f.name for f in fields(Diagnostic)][:2] == ["message", "severity"]
    first = ExecutionMetadata("completed")
    second = ExecutionMetadata("completed")
    first.configuration.requested = {"duration": 1}
    first.timing.elapsed_seconds = 2
    assert second.configuration == ConfigurationSnapshot()
    assert second.timing.elapsed_seconds is None
    first_result = Result(True)
    second_result = Result(True)
    first_result.extensions["example.test"] = {"value": 1}
    assert second_result.extensions == {}


def test_native_interval_scope_and_measurements_are_explicit_and_preserve_warmup():
    """Keep native bytes, seconds, omissions and scope without deriving them from socket presence."""
    data = {
        "start": -1,
        "end": 0,
        "seconds": 1.01,
        "bytes": 1200,
        "bits_per_second": 9504.95,
        "omitted": True,
        "packets": 10,
        "lost_packets": 1,
        "retransmits": 2,
        "lost_percent": 10,
        "jitter_ms": 0.5,
        "sender": False,
    }
    result = result_from_iperf_json(
        {
            "start": {"test_start": {"reverse": 0}},
            "intervals": [{"sum": {**data, "socket": 7}, "streams": [data]}],
        }
    )

    aggregate, stream = result.intervals
    assert aggregate.scope == "aggregate"
    assert stream.scope == "stream"
    assert aggregate.stream_id is None and stream.stream_id is None
    for interval in result.intervals:
        assert interval.bytes == 1200
        assert interval.duration_seconds == 1.01
        assert interval.start_seconds == -1 and interval.end_seconds == 0
        assert interval.omitted is True
        assert interval.packets == 10 and interval.lost_packets == 1
        assert interval.retransmits == 2 and interval.lost_percent == 10
        assert interval.jitter_ms == 0.5


def test_saved_native_timing_preserves_estimate_without_claiming_actual_completion():
    """Separate inferred timestamps from observed operation timing in saved native JSON."""
    result = result_from_iperf_json(
        {
            "start": {"timestamp": {"timesecs": 100}, "test_start": {"duration": 10, "reverse": 0}},
            "end": {"sum_sent": {"bits_per_second": 1}},
        }
    )

    assert result.execution is not None
    assert result.execution.status == "completed"
    assert result.execution.method == "forward"
    assert result.started_at_seconds == 100 and result.duration_seconds == 10
    assert result.completed_at_seconds is None
    timing = result.execution.timing
    assert timing.native_started_at_seconds == 100
    assert timing.requested_duration_seconds == 10
    assert timing.estimated_completed_at_seconds == 110
    assert timing.started_at_seconds is None
    assert timing.completed_at_seconds is None
    assert timing.elapsed_seconds is None


@pytest.mark.parametrize(
    ("raw", "status"), [({"error": "connection refused"}, "failed"), ({}, "incomplete")]
)
def test_execution_status_distinguishes_failed_and_incomplete_native_documents(raw, status):
    """Retain native failure versus missing terminal evidence as separate outcomes."""
    result = result_from_iperf_json(raw)

    assert not result.ok
    assert result.execution is not None
    assert result.execution.status == status
    assert result.execution.timing.estimated_completed_at_seconds is None
    assert any(
        d.code == ("native.error" if status == "failed" else "native.incomplete")
        and d.evidence_paths
        for d in result.diagnostics
    )


def test_native_configuration_and_environment_have_verified_evidence_without_request_fabrication():
    """Record returned settings and native environment while keeping caller/environment unknowns absent."""
    raw = {
        "start": {
            "version": "iperf 3.21",
            "system_info": "Linux native-host",
            "connecting_to": {"host": "target.example", "port": 5201},
            "test_start": {
                "protocol": "UDP",
                "duration": 1,
                "num_streams": 2,
                "omit": 1,
                "reverse": 0,
                "bidir": 0,
                "blksize": 1200,
                "target_bitrate": 4000000,
                "tos": 16,
            },
        },
        "end": {"sum_sent": {"bytes": 1200}},
    }
    result = result_from_iperf_json(raw)

    assert result.execution is not None
    assert result.execution.native_version == "iperf 3.21"
    assert result.execution.native_system_info == "Linux native-host"
    assert result.execution.python_version is None and result.execution.platform is None
    configuration = result.execution.configuration
    assert configuration.requested is None
    expected = {
        "server": "target.example",
        "port": 5201,
        "protocol": "udp",
        "duration": 1,
        "parallel": 2,
        "omit": 1,
        "reverse": False,
        "bidirectional": False,
        "blksize": 1200,
        "rate": 4000000,
        "tos": 16,
    }
    for name, value in expected.items():
        assert configuration.effective[name].value == value
        assert configuration.effective[name].state == "verified"
        assert configuration.effective[name].evidence_paths[0].startswith("/raw/start/")
    for name in ("mptcp", "json_stream"):
        assert configuration.effective[name].value is None
        assert configuration.effective[name].state == "unavailable"


def test_conflicting_native_rate_evidence_is_not_reported_as_verified():
    """Retain contradictory native rate fields with a diagnostic instead of choosing silently."""
    result = result_from_iperf_json(
        {"start": {"target_bitrate": 1, "test_start": {"target_bitrate": 2}}}
    )

    assert result.execution is not None
    assert result.execution.configuration.effective["rate"].state == "unavailable"
    assert any(
        d.code == "configuration.conflict" and len(d.evidence_paths) == 2
        for d in result.diagnostics
    )


def test_udp_end_stream_summary_remains_unattributed_with_native_evidence():
    """Preserve mixed native UDP fields without assigning the whole object to one endpoint."""
    udp = {
        "socket": 5,
        "sender": False,
        "start": 0,
        "end": 1,
        "seconds": 1,
        "bytes": 0,
        "bits_per_second": 0,
        "packets": 10,
        "lost_packets": 1,
        "lost_percent": 10,
        "jitter_ms": 0.25,
    }
    result = result_from_iperf_json(
        {
            "start": {"accepted_connection": {}, "test_start": {"protocol": "UDP", "reverse": 0}},
            "end": {"streams": [{"udp": udp}]},
        }
    )

    assert result.ok
    stream = result.streams[0]
    assert stream.stream_id == 5 and stream.direction == "client_to_server"
    assert stream.sender is None and stream.receiver is None
    assert stream.unattributed is not None
    assert stream.unattributed.observation is None
    assert stream.unattributed.bytes == 0
    assert stream.unattributed.lost_packets == 1
    assert stream.unattributed.jitter_ms == 0.25
    assert result.availability["/streams/0/unattributed/observation"].state == "unknown"
    assert any(
        d.code == "provenance.mixed_udp_summary" and d.evidence_paths == ["/raw/end/streams/0/udp"]
        for d in result.diagnostics
    )


def test_tcp_stream_summary_preserves_endpoint_fields_and_conflicting_socket_evidence():
    """Retain endpoint measurements while flagging an invalid pairing of local socket IDs."""
    result = result_from_iperf_json(
        {
            "start": {"test_start": {"reverse": 0}},
            "end": {
                "streams": [
                    {
                        "sender": {
                            "socket": 7,
                            "sender": True,
                            "bytes": 1200,
                            "seconds": 1,
                            "start": 0,
                            "end": 1,
                        },
                        "receiver": {"socket": 9, "sender": True, "bytes": 1100, "seconds": 1},
                    }
                ]
            },
        }
    )

    stream = result.streams[0]
    assert stream.stream_id is None and stream.direction == "unknown"
    assert stream.sender is not None and stream.receiver is not None
    assert stream.sender.bytes == 1200 and stream.receiver.bytes == 1100
    assert stream.sender.observation == "sender" and stream.receiver.observation == "receiver"
    assert any(
        d.code == "provenance.conflict" and d.path == "/streams/0" for d in result.diagnostics
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"start": {"test_start": {"protocol": "unknown"}}},
        {"start": {"version": 1}},
        {"end": {"sum_sent": {"bytes": -1}}},
        {"intervals": [{"sum": {"seconds": -1}}]},
        {"end": {"sum_sent": {"packets": True}}},
    ],
)
def test_rejects_malformed_canonical_metadata_and_measurements(raw):
    """Keep unsupported protocol values and invalid counts outside canonical models."""
    with pytest.raises(ValueError):
        result_from_iperf_json(raw)


@pytest.mark.parametrize("summary", [{"seconds": 10}, {"start": 0, "end": 10, "seconds": 10}])
def test_terminal_duration_without_measurements_is_incomplete(summary):
    """Timing alone does not establish that native terminal traffic was measured."""
    result = result_from_iperf_json({"end": {"sum_sent": summary}})

    assert not result.ok
    assert result.execution is not None and result.execution.status == "incomplete"
    assert result.end is not None and result.end.sum_sent is not None
    assert result.end.sum_sent.duration_seconds == 10


@pytest.mark.parametrize("field", ["bytes", "packets"])
def test_terminal_zero_counts_are_valid_measurement_evidence(field):
    """Measured zero traffic remains distinct from absent measurements."""
    result = result_from_iperf_json({"end": {"sum_sent": {field: 0}}})

    assert result.ok
    assert result.execution is not None and result.execution.status == "completed"


@pytest.mark.parametrize("version", ["iperf 3.19.1", "iperf 3.21", "iperf 3.22"])
@pytest.mark.parametrize("value", [-2, -1, 0, 3684054920433006592])
def test_verified_sctp_retransmissions_are_unsupported_with_explicit_evidence(version, value):
    """Keep native sentinel, zero and uninitialized SCTP values solely in raw evidence."""
    raw = {
        "start": {"version": version, "test_start": {"protocol": "SCTP", "reverse": 0}},
        "end": {
            "sum_sent": {"retransmits": value, "bytes": 0},
            "streams": [{"sender": {"retransmits": value, "bytes": 0}}],
        },
        "intervals": [{"sum": {"retransmits": value, "sender": True}}],
    }
    result = result_from_iperf_json(raw)

    assert result.ok and result.raw == raw
    assert result.flows[0].sender is not None and result.flows[0].sender.retransmits is None
    assert result.streams[0].sender is not None and result.streams[0].sender.retransmits is None
    assert result.intervals[0].retransmits is None
    for path, source_path in (
        ("/flows/0/sender/retransmits", "/raw/end/sum_sent/retransmits"),
        ("/streams/0/sender/retransmits", "/raw/end/streams/0/sender/retransmits"),
        ("/intervals/0/retransmits", "/raw/intervals/0/sum/retransmits"),
    ):
        unavailable = result.availability[path]
        assert unavailable.state == "unsupported"
        assert unavailable.evidence_paths == [
            source_path,
            "/raw/start/test_start/protocol",
            "/raw/start/version",
        ]
        assert any(
            d.code == "measurement.unsupported"
            and d.path == path
            and d.evidence_paths == unavailable.evidence_paths
            for d in result.diagnostics
        )


@pytest.mark.parametrize("version", ["iperf 3.19.1", "iperf 3.21", "iperf 3.22"])
@pytest.mark.parametrize(
    ("protocol", "value"),
    [
        ("SCTP", True),
        ("SCTP", 0.0),
        ("SCTP", float("inf")),
        ("TCP", -1),
    ],
)
def test_sctp_unsupported_rule_does_not_accept_unrelated_invalid_values(version, protocol, value):
    """Require a verified producer and valid native sentinel/count shapes."""
    with pytest.raises(ValueError, match="retransmits"):
        result_from_iperf_json(
            {
                "start": {"version": version, "test_start": {"protocol": protocol}},
                "end": {"sum_sent": {"retransmits": value}},
            }
        )


@pytest.mark.parametrize("version", ["iperf 3.19.1", "iperf 3.21", "iperf 3.22"])
def test_unsupported_retransmissions_alone_do_not_establish_completed_execution(version):
    """An emitted unsupported field supplies no terminal measurement evidence."""
    result = result_from_iperf_json(
        {
            "start": {"version": version, "test_start": {"protocol": "SCTP"}},
            "end": {"sum_sent": {"retransmits": 0, "seconds": 1}},
        }
    )

    assert not result.ok
    assert result.execution is not None and result.execution.status == "incomplete"


@pytest.mark.parametrize("version", [None, "iperf unknown", "iperf 99.0"])
@pytest.mark.parametrize("value", [-2, 0, 7])
def test_unqualified_sctp_producer_keeps_retransmission_availability_unknown(version, value):
    """An unqualified producer cannot establish retransmission support or a real measurement."""
    result = result_from_iperf_json(
        {
            "start": {"version": version, "test_start": {"protocol": "SCTP"}},
            "end": {"sum_sent": {"retransmits": value, "bytes": 0}},
        }
    )

    assert result.ok
    assert result.flows[0].sender is not None and result.flows[0].sender.retransmits is None
    assert result.availability["/flows/0/sender/retransmits"].state == "unknown"
    assert any(d.code == "measurement.unknown" for d in result.diagnostics)
