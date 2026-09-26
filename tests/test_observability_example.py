"""Check that the live-stack verifier compares native measurements and rejects bad evidence."""

import copy
import json
from pathlib import Path

import pytest

from examples.observability.verify import (
    PROFILES,
    check_vector,
    expected_metrics,
    grafana_vector,
    series_key,
)


def _receipts() -> dict:
    receipts = {}
    for profile in PROFILES:
        sender = {"bits_per_second": 800, "retransmits": 3}
        receiver = {"bits_per_second": 720}
        if profile == "udp":
            sender = {"bits_per_second": 800}
            receiver |= {"lost_percent": 2.5, "jitter_ms": 3}
        end = {"sum_sent": sender, "sum_received": receiver}
        if profile == "tcp-bidirectional":
            end |= {
                "sum_sent_bidir_reverse": {"bits_per_second": 1600},
                "sum_received_bidir_reverse": {"bits_per_second": 1440},
            }
        receipts[profile] = {
            "last_success_timestamp_seconds": 100,
            "result": {
                "ok": profile != "transition",
                "completed_at_seconds": 110,
                "raw": {
                    "start": {
                        "test_start": {
                            "protocol": "UDP" if profile == "udp" else "TCP",
                            "duration": 2,
                            "num_streams": 1 if profile == "udp" else 2,
                            "reverse": profile == "tcp-reverse",
                            "bidir": profile == "tcp-bidirectional",
                            "target_bitrate": 4_000_000,
                        }
                    },
                    "end": end,
                },
            },
        }
    return receipts


def test_expected_metrics_derive_units_direction_and_observer_from_native_json() -> None:
    """Compare flow observations individually and apply the documented base-unit conversions."""
    expected = expected_metrics(_receipts())
    labels = {
        "profile": "udp",
        "target": "pod-loopback",
        "direction": "client_to_server",
        "observer": "receiver",
    }
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", labels)] == 90
    assert expected[series_key("iperf3_last_run_packet_loss_ratio", labels)] == 0.025
    assert expected[series_key("iperf3_last_run_jitter_seconds", labels)] == 0.003
    reverse = labels | {"profile": "tcp-reverse", "direction": "server_to_client"}
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", reverse)] == 90
    bidirectional = labels | {"profile": "tcp-bidirectional", "direction": "server_to_client"}
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", bidirectional)] == 180
    assert not any(
        "transition" in dict(key[1]).values() and "throughput" in key[0] for key in expected
    )


def test_native_configuration_mismatch_is_rejected() -> None:
    """Require returned native configuration to match the exercised protocol and dimensions."""
    receipts = _receipts()
    receipts["udp"]["result"]["raw"]["start"]["test_start"]["protocol"] = "TCP"
    with pytest.raises(ValueError, match="native protocol"):
        expected_metrics(receipts)


@pytest.mark.parametrize("flag", ["reverse", "bidir", "bidirectional"])
def test_native_direction_mismatch_is_rejected(flag: str) -> None:
    """Reject ignored requested direction flags and conflicting native aliases."""
    receipts = _receipts()
    profile = "tcp-reverse" if flag == "reverse" else "tcp-bidirectional"
    receipts[profile]["result"]["raw"]["start"]["test_start"][flag] = 0
    with pytest.raises(ValueError, match="flag"):
        expected_metrics(receipts)


def test_native_bidirectional_flag_must_be_present() -> None:
    """An absent flag cannot qualify a requested bidirectional native run."""
    receipts = _receipts()
    del receipts["tcp-bidirectional"]["result"]["raw"]["start"]["test_start"]["bidir"]
    with pytest.raises(ValueError, match="bidirectional flag"):
        expected_metrics(receipts)


def test_grafana_frames_preserve_measurement_labels() -> None:
    """Extract exact sample labels and numbers from the dashboard query response format."""
    labels = {"__name__": "iperf3_last_run_success", "profile": "udp"}
    frame = {
        "schema": {
            "fields": [
                {"name": "Time", "type": "time"},
                {"name": "Value", "type": "number", "labels": labels},
            ]
        },
        "data": {"values": [[100], [1]]},
    }
    assert grafana_vector({"results": {"A": {"status": 200, "frames": [frame]}}}) == [
        {"metric": labels, "value": [0, 1]}
    ]
    frame["data"]["values"][1] = [None]
    with pytest.raises(ValueError, match="one finite sample"):
        grafana_vector({"results": {"A": {"frames": [frame]}}})


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "wrong", "nan"])
def test_api_vector_errors_fail(mutation: str) -> None:
    """Reject successful HTTP responses containing incomplete or incorrect measurements."""
    labels = {"profile": "udp", "target": "pod-loopback"}
    expected = {series_key("iperf3_last_run_success", labels): 1}
    vector = [
        {
            "metric": labels
            | {
                "__name__": "iperf3_last_run_success",
                "job": "iperf3-textfile",
                "instance": "benchmark:9100",
            },
            "value": [100, "1"],
        }
    ]
    if mutation == "missing":
        vector.clear()
    elif mutation == "extra":
        extra = copy.deepcopy(vector[0])
        extra["metric"]["profile"] = "unexpected"
        vector.append(extra)
    elif mutation == "duplicate":
        vector.append(copy.deepcopy(vector[0]))
    elif mutation == "wrong":
        vector[0]["value"][1] = "0"
    else:
        vector[0]["value"][1] = "NaN"
    with pytest.raises(ValueError):
        check_vector(expected, vector)


def test_dashboard_current_values_use_instant_queries() -> None:
    """Prevent last-not-null range reduction from displaying old samples as current status."""
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads(
        (root / "examples/observability/dashboard.json").read_text(encoding="utf-8")
    )
    for panel in dashboard["panels"]:
        if panel["type"] == "stat":
            assert all(target["instant"] and not target["range"] for target in panel["targets"])
        if panel["title"] == "UDP packet loss ratio":
            assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"
            assert panel["fieldConfig"]["defaults"]["max"] == 1
