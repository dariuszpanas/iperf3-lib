"""Prometheus text exposition for completed iperf3 results."""

from __future__ import annotations

import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..result import FlowStats, Result

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")
_RESERVED_LABELS = {"__name__"}


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float | int | None) -> str | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Prometheus samples must be finite numbers")
    return str(int(number)) if number.is_integer() else repr(number)


def render_text(
    result: Result,
    labels: Mapping[str, str] | None = None,
    *,
    last_success_timestamp_seconds: float | None = None,
) -> str:
    """Render latest-run gauges as Prometheus text exposition format."""
    normalized_labels = dict(labels or {})
    for name, value in normalized_labels.items():
        if not _LABEL_NAME.fullmatch(name) or name in _RESERVED_LABELS:
            raise ValueError(f"invalid Prometheus label name: {name!r}")
        if not isinstance(value, str):
            raise TypeError(f"Prometheus label {name!r} must have a string value")

    lines: list[str] = []

    def emit(name: str, help_text: str, value: float | int | None, **extra: str) -> None:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid Prometheus metric name: {name!r}")
        formatted = _number(value)
        if formatted is None:
            return
        all_labels = normalized_labels | extra
        label_text = ""
        if all_labels:
            pairs = ",".join(
                f'{key}="{_escape_label(val)}"' for key, val in sorted(all_labels.items())
            )
            label_text = f"{{{pairs}}}"
        lines.extend(
            [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name}{label_text} {formatted}",
            ]
        )

    emit(
        "iperf3_last_run_success",
        "Whether the latest run completed successfully.",
        int(result.ok),
    )
    completed_at = result.completed_at_seconds
    if (
        completed_at is None
        and result.started_at_seconds is not None
        and result.duration_seconds is not None
    ):
        completed_at = result.started_at_seconds + result.duration_seconds
    if completed_at is not None:
        emit(
            "iperf3_last_run_completed_timestamp_seconds",
            "Unix timestamp when the latest run completed.",
            completed_at,
        )
    last_success = (
        completed_at if result.ok and completed_at is not None else last_success_timestamp_seconds
    )
    emit(
        "iperf3_last_success_timestamp_seconds",
        "Unix timestamp when the latest successful run completed.",
        last_success,
    )
    if result.ok:
        flows = result.flows
        if not flows and result.end is not None:
            flows = [
                FlowStats(
                    direction="client_to_server",
                    sender=result.end.sum_sent,
                    receiver=result.end.sum_received,
                )
            ]
        for flow in flows:
            for observer, stats in (("sender", flow.sender), ("receiver", flow.receiver)):
                if stats is None:
                    continue
                emit(
                    "iperf3_last_run_throughput_bytes_per_second",
                    "Throughput observed during the latest run.",
                    stats.bits_per_second / 8,
                    direction=flow.direction,
                    observer=observer,
                )
                if stats.retransmits is not None:
                    emit(
                        "iperf3_last_run_retransmissions",
                        "TCP retransmissions reported during the latest run.",
                        stats.retransmits,
                        direction=flow.direction,
                        observer=observer,
                    )
                if stats.lost_percent is not None:
                    emit(
                        "iperf3_last_run_packet_loss_ratio",
                        "Packet loss ratio reported during the latest run.",
                        stats.lost_percent / 100,
                        direction=flow.direction,
                        observer=observer,
                    )
                if stats.jitter_ms is not None:
                    emit(
                        "iperf3_last_run_jitter_seconds",
                        "Jitter reported during the latest run.",
                        stats.jitter_ms / 1000,
                        direction=flow.direction,
                        observer=observer,
                    )
    return "\n".join(lines) + "\n"


def write_textfile(
    path: str | os.PathLike[str],
    result: Result,
    labels: Mapping[str, str] | None = None,
    *,
    last_success_timestamp_seconds: float | None = None,
) -> None:
    """Atomically replace a node_exporter textfile collector metrics file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = render_text(
        result,
        labels,
        last_success_timestamp_seconds=last_success_timestamp_seconds,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        raise
