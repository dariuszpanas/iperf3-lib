"""Tests for Prometheus text rendering and atomic textfile output."""

import re
from pathlib import Path

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


def test_groups_samples_with_one_metadata_pair_per_metric_family():
    """Group all endpoint samples after one HELP/TYPE pair per metric name."""
    result = Result(
        ok=True,
        flows=[
            FlowStats(
                direction=direction,
                sender=SumStats(8_000_000, retransmits=3, lost_percent=1, jitter_ms=4),
                receiver=SumStats(4_000_000, retransmits=0, lost_percent=2, jitter_ms=5),
            )
            for direction in ("client_to_server", "server_to_client")
        ],
    )

    text = render_text(result, {"target": "host-a", "profile": "bidir"})
    assert text == render_text(result, {"profile": "bidir", "target": "host-a"})
    lines = text.splitlines()
    for metric in (
        "iperf3_last_run_throughput_bytes_per_second",
        "iperf3_last_run_retransmissions",
        "iperf3_last_run_packet_loss_ratio",
        "iperf3_last_run_jitter_seconds",
    ):
        help_indices = [i for i, line in enumerate(lines) if line.startswith(f"# HELP {metric} ")]
        type_indices = [i for i, line in enumerate(lines) if line == f"# TYPE {metric} gauge"]
        sample_indices = [i for i, line in enumerate(lines) if line.startswith(f"{metric}{{")]
        assert len(help_indices) == 1
        assert type_indices == [help_indices[0] + 1]
        assert sample_indices == list(range(type_indices[0] + 1, type_indices[0] + 5))


@pytest.mark.parametrize("name", ["direction", "observer", "__name__", "__custom", "bad-label", 1])
def test_rejects_reserved_and_invalid_caller_label_names(name):
    """Reject caller labels that conflict with built-in or Prometheus labels."""
    with pytest.raises(ValueError, match="invalid Prometheus label name"):
        render_text(Result(ok=True), {name: "value"})


@pytest.mark.parametrize("value", [1, None, False])
def test_rejects_non_string_label_values(value):
    """Reject label values that cannot be escaped as strings."""
    with pytest.raises(TypeError, match="must have a string value"):
        render_text(Result(ok=True), {"target": value})


def test_rejects_duplicate_sample_identity():
    """Reject duplicate direction/observer samples instead of ambiguous output."""
    result = Result(
        ok=True,
        flows=[
            FlowStats(direction="client_to_server", sender=SumStats(rate))
            for rate in (8_000_000, 4_000_000)
        ],
    )

    with pytest.raises(ValueError, match="duplicate Prometheus sample"):
        render_text(result, {"target": "host-a"})


def test_omits_unavailable_measurements_without_hiding_measured_zero():
    """Keep absent endpoints and optional values distinct from zero samples."""
    result = Result(
        ok=True,
        flows=[FlowStats(direction="client_to_server", receiver=SumStats(0, retransmits=0))],
    )

    text = render_text(result)

    assert 'observer="sender"' not in text
    assert (
        'iperf3_last_run_throughput_bytes_per_second{direction="client_to_server",observer="receiver"} 0'
        in text
    )
    assert (
        'iperf3_last_run_retransmissions{direction="client_to_server",observer="receiver"} 0'
        in text
    )
    assert "packet_loss_ratio" not in text
    assert "jitter_seconds" not in text


def test_write_textfile_preserves_destination_and_cleans_up_after_replace_failure(
    tmp_path, monkeypatch
):
    """Remove an unfinished replacement and preserve the previous snapshot."""
    destination = tmp_path / "iperf.prom"
    destination.write_text("previous snapshot\n", encoding="utf-8")
    temporary_paths = []

    def fail_replace(source, target):
        """Inspect the completed temporary file before simulating an OS failure."""
        temporary = Path(source)
        temporary_paths.append(temporary)
        assert temporary.parent == destination.parent
        assert "iperf3_last_run_success 1" in temporary.read_text(encoding="utf-8")
        assert target == destination
        raise OSError("replacement denied")

    monkeypatch.setattr("iperf3_lib.exporters.prometheus.os.replace", fail_replace)

    with pytest.raises(OSError, match="replacement denied"):
        write_textfile(destination, Result(ok=True))

    assert destination.read_text(encoding="utf-8") == "previous snapshot\n"
    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
    assert list(tmp_path.iterdir()) == [destination]


def test_write_textfile_invalid_sample_has_no_filesystem_side_effects(tmp_path):
    """Reject invalid samples before creating directories or temporary files."""
    destination = tmp_path / "metrics" / "iperf.prom"
    result = Result(ok=True, end=EndStats(sum_sent=SumStats(float("inf"))))

    with pytest.raises(ValueError, match="must be finite"):
        write_textfile(destination, result)

    assert list(tmp_path.iterdir()) == []
