"""Tests for Prometheus text rendering and atomic textfile output."""

import re

import pytest

from iperf3_lib.exporters.prometheus import render_text, write_textfile
from iperf3_lib.result import EndStats, FlowStats, Result, SumStats


def test_render_text_uses_snapshot_gauges_consistent_units_and_escaped_labels():
    """Render complete latest-run observations with stable labels and units."""
    result = Result(
        ok=True,
        end=EndStats(
            sum_sent=SumStats(bits_per_second=8_000_000, retransmits=3),
            sum_received=SumStats(bits_per_second=4_000_000, lost_percent=2.5, jitter_ms=4),
        ),
        started_at_seconds=100,
        duration_seconds=5,
    )

    text = render_text(result, {"target": 'lab"\\\nserver', "profile": "tcp-4-streams"})

    assert "# TYPE iperf3_last_run_success gauge" in text
    assert 'target="lab\\"\\\\\\nserver"' in text
    assert (
        'iperf3_last_run_completed_timestamp_seconds{profile="tcp-4-streams",target="lab\\"\\\\\\nserver"} 105'
        in text
    )
    assert "iperf3_last_run_throughput_bytes_per_second" in text
    assert 'direction="client_to_server",observer="receiver"' in text
    assert " 500000" in text
    assert " 0.025" in text
    assert " 0.004" in text
    assert "iperf3_retransmissions_total" not in text
    for line in text.splitlines():
        if not line.startswith("#"):
            assert re.match(
                r"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{.*\})? -?(?:\d+(?:\.\d+)?|\d+\.\d+e[+-]?\d+)$", line
            )


def test_failures_export_status_without_stale_measurements():
    """Do not publish old measurements from a failed latest result."""
    text = render_text(
        Result(
            ok=False,
            error="failed",
            end=EndStats(sum_sent=SumStats(99)),
            completed_at_seconds=105,
        ),
        last_success_timestamp_seconds=90,
    )

    assert "iperf3_last_run_success 0" in text
    assert "throughput" not in text
    assert "completed_timestamp_seconds 105" in text
    assert "iperf3_last_success_timestamp_seconds 90" in text


def test_uses_normalized_direction_for_reverse_run():
    """Carry reverse direction through to the metrics label."""
    result = Result(
        ok=True,
        end=EndStats(sum_sent=SumStats(bits_per_second=8_000_000)),
        flows=[
            FlowStats(
                direction="server_to_client",
                sender=SumStats(bits_per_second=8_000_000),
            )
        ],
    )

    text = render_text(result)

    assert 'direction="server_to_client",observer="sender"' in text
    assert 'direction="client_to_server"' not in text


def test_rejects_invalid_labels_and_non_finite_samples():
    """Fail closed on malformed exposition data."""
    with pytest.raises(ValueError):
        render_text(Result(ok=True), {"bad-label": "value"})
    with pytest.raises(ValueError):
        render_text(Result(ok=True, end=EndStats(sum_sent=SumStats(float("nan")))))


def test_write_textfile_atomically_replaces_destination(tmp_path):
    """Write complete metrics through a same-directory temporary file."""
    destination = tmp_path / "metrics" / "iperf.prom"
    destination.parent.mkdir()
    destination.write_text("old metrics\n", encoding="utf-8")

    write_textfile(destination, Result(ok=True), {"target": "host-a"})

    content = destination.read_text(encoding="utf-8")
    assert 'iperf3_last_run_success{target="host-a"} 1' in content
    assert sorted(path.name for path in destination.parent.iterdir()) == ["iperf.prom"]
