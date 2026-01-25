from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SumStats(BaseModel):
    bits_per_second: float = Field(0, alias="bits_per_second")
    retransmits: int | None = None
    lost_percent: float | None = None
    jitter_ms: float | None = None


class EndStats(BaseModel):
    sum_sent: SumStats | None = None
    sum_received: SumStats | None = None


class Result(BaseModel):
    ok: bool
    error: str | None = None
    raw: dict[str, Any] = {}
    end: EndStats | None = None

    @property
    def summary_mbps(self) -> float:
        if not self.end:
            return 0.0
        for part in (self.end.sum_sent, self.end.sum_received):
            if part and part.bits_per_second:
                return part.bits_per_second / 1_000_000.0
        return 0.0
