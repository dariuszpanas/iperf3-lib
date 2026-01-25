"""Result and statistics models for iperf3 test runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SumStats(BaseModel):
    """Summary statistics for a test direction (sent/received)."""

    bits_per_second: float = Field(0, alias="bits_per_second")
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None


class EndStats(BaseModel):
    """End-of-test statistics for both directions."""

    sum_sent: SumStats | None = None
    sum_received: SumStats | None = None


class Result(BaseModel):
    """Top-level result object for an iperf3 test run."""

    ok: bool
    error: str | None = None
    raw: dict[str, Any] = {}
    end: EndStats | None = None

    @property
    def summary_mbps(self) -> float:
        """Return the summary throughput in Mbps, or 0.0 if unavailable."""
        if not self.end:
            return 0.0
        for part in (self.end.sum_sent, self.end.sum_received):
            if part and part.bits_per_second:
                return part.bits_per_second / 1_000_000.0
        return 0.0
