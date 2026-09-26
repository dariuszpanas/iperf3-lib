"""Explicit rate intent and admission estimates over strict native configuration."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .config import ClientConfig, Protocol
from .result import JSONValue

MAX_RATE = 2**64 - 1
DEFAULT_UDP_RATE = 1024 * 1024


def _integer(name: str, value: int, minimum: int = 0, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(
            f"{name} must be >= {minimum}" + (f" and <= {maximum}" if maximum is not None else "")
        )


@dataclass(frozen=True)
class RateIntent:
    """Choose exactly one per-stream or aggregate-per-direction rate, in bits/s."""

    per_stream_bps: int | None = None
    aggregate_bps_per_direction: int | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous intent and preserve native zero-as-unlimited semantics."""
        if (self.per_stream_bps is None) == (self.aggregate_bps_per_direction is None):
            raise ValueError("specify exactly one rate intent")
        if self.per_stream_bps is not None:
            _integer("per_stream_bps", self.per_stream_bps, maximum=MAX_RATE)
        if self.aggregate_bps_per_direction is not None:
            _integer("aggregate_bps_per_direction", self.aggregate_bps_per_direction, 1, MAX_RATE)


@dataclass(frozen=True)
class ResolvedRate:
    """Uniform native allocation; finite targets are estimates, not measured limits."""

    native_per_stream_bps: int | None
    aggregate_bps_per_direction: int | None
    aggregate_bps_all_directions: int | None
    unused_bps_per_direction: int | None
    active_directions: tuple[str, ...]
    source: Literal["legacy", "per_stream", "aggregate", "protocol_default"]

    def to_dict(self) -> dict[str, JSONValue]:
        """Return detached JSON-safe resolution metadata for artifact extensions."""
        return {
            "native_per_stream_bps": self.native_per_stream_bps,
            "aggregate_bps_per_direction": self.aggregate_bps_per_direction,
            "aggregate_bps_all_directions": self.aggregate_bps_all_directions,
            "unused_bps_per_direction": self.unused_bps_per_direction,
            "active_directions": list(self.active_directions),
            "source": self.source,
        }


def resolve_rate(config: ClientConfig, intent: RateIntent | None = None) -> ResolvedRate:
    """Resolve uniform per-stream native rate without modifying the caller's config.

    Aggregate targets are divided by streams per direction, rounding down. A
    target smaller than the stream count is rejected to avoid native zero,
    which disables pacing. Bidirectional applies the same target independently
    to both directions. Legacy/default unlimited rates have unknown totals.
    """
    if not isinstance(config, ClientConfig):
        raise TypeError("config must be a ClientConfig")
    cfg = replace(config)
    if intent is not None and not isinstance(intent, RateIntent):
        raise TypeError("intent must be a RateIntent or None")
    if intent is not None:
        intent = replace(intent)
        if cfg.rate is not None:
            raise ValueError("ClientConfig.rate and RateIntent cannot both be specified")
    directions = (
        ("client_to_server", "server_to_client")
        if cfg.bidirectional
        else ("server_to_client",)
        if cfg.reverse
        else ("client_to_server",)
    )
    remainder = 0
    source: Literal["legacy", "per_stream", "aggregate", "protocol_default"]
    if intent is not None and intent.aggregate_bps_per_direction is not None:
        rate, remainder = divmod(intent.aggregate_bps_per_direction, cfg.parallel)
        if rate == 0:
            raise ValueError("aggregate rate must be at least the number of parallel streams")
        source = "aggregate"
    elif intent is not None:
        rate = intent.per_stream_bps
        source = "per_stream"
    elif cfg.rate is not None:
        rate = cfg.rate
        source = "legacy"
    else:
        rate = DEFAULT_UDP_RATE if cfg.protocol is Protocol.UDP else None
        source = "protocol_default"
    aggregate = rate * cfg.parallel if rate else None
    return ResolvedRate(
        rate,
        aggregate,
        aggregate * len(directions) if aggregate is not None else None,
        remainder if aggregate is not None else None,
        directions,
        source,
    )


def parse_rate(text: str) -> int:
    """Parse an explicit decimal SI bit/s or byte/s quantity into whole bits/s.

    Supported units are bit/s, kbit/s, Mbit/s, Gbit/s, Tbit/s and B/s, kB/s,
    MB/s, GB/s, TB/s, with bps/kbps/Mbps/Gbps/Tbps aliases. Unit case matters.
    Binary prefixes, exponent notation and fractional resulting bits are rejected.
    """
    if not isinstance(text, str):
        raise TypeError("rate text must be a string")
    if len(text) > 100:
        raise ValueError("rate text is too long")
    match = re.fullmatch(r"([0-9]+)(?:\.([0-9]+))?\s*([A-Za-z/]+)", text.strip())
    if match is None:
        raise ValueError("rate requires a decimal number and an explicit supported unit")
    whole, fraction, unit = match.groups()
    units = {
        prefix + suffix: factor * scale
        for prefix, scale in (("", 1), ("k", 10**3), ("M", 10**6), ("G", 10**9), ("T", 10**12))
        for suffix, factor in (("bit/s", 1), ("bps", 1), ("B/s", 8))
    }
    if unit not in units:
        raise ValueError(f"unsupported rate unit: {unit}")
    fraction = fraction or ""
    numerator = int(whole + fraction) * units[unit]
    value, remainder = divmod(numerator, 10 ** len(fraction))
    if remainder:
        raise ValueError("rate must resolve to whole bits per second")
    _integer("rate", value, maximum=MAX_RATE)
    return value


@dataclass(frozen=True)
class RunEstimate:
    """Requested active time and target payload for one sequential run, including warm-up."""

    rate: ResolvedRate
    active_seconds: int
    estimated_payload_bits: int | None


@dataclass(frozen=True)
class PlanEstimate:
    """Admission arithmetic only; no network overhead or wall-clock enforcement."""

    runs: tuple[RunEstimate, ...]
    active_seconds: int
    estimated_payload_bits: int | None
    estimated_payload_bytes: int | None


def estimate_plan(
    configs: Sequence[ClientConfig],
    intents: Sequence[RateIntent | None] | None = None,
    *,
    max_payload_bytes: int | None = None,
    max_active_seconds: int | None = None,
) -> PlanEstimate:
    """Estimate a finite sequential plan and reject exceeded admission budgets.

    Includes warm-up and both traffic directions, counts payload once, and
    rounds total estimated bytes upward. Unknown/unlimited rates cannot satisfy
    a finite payload budget. These budgets do not stop a blocking native run.
    """
    if not isinstance(configs, Sequence) or isinstance(configs, (str, bytes)):
        raise TypeError("configs must be a finite sequence of ClientConfig values")
    if intents is not None and (
        not isinstance(intents, Sequence) or isinstance(intents, (str, bytes))
    ):
        raise TypeError("intents must be a finite sequence")
    if intents is not None and len(intents) != len(configs):
        raise ValueError("configs and intents must have equal lengths")
    for name, value in (
        ("max_payload_bytes", max_payload_bytes),
        ("max_active_seconds", max_active_seconds),
    ):
        if value is not None:
            _integer(name, value)
    runs = []
    for index, config in enumerate(configs):
        if not isinstance(config, ClientConfig):
            raise TypeError("configs must contain ClientConfig values")
        cfg = replace(config)
        rate = resolve_rate(cfg, intents[index] if intents is not None else None)
        active = cfg.duration + cfg.omit
        bits = rate.aggregate_bps_all_directions
        runs.append(RunEstimate(rate, active, bits * active if bits is not None else None))
    active_seconds = sum(run.active_seconds for run in runs)
    payload_bits = (
        sum(run.estimated_payload_bits for run in runs if run.estimated_payload_bits is not None)
        if all(run.estimated_payload_bits is not None for run in runs)
        else None
    )
    payload_bytes = (payload_bits + 7) // 8 if payload_bits is not None else None
    if max_active_seconds is not None and active_seconds > max_active_seconds:
        raise ValueError("plan exceeds the active-time admission budget")
    if max_payload_bytes is not None:
        if payload_bytes is None:
            raise ValueError("plan payload estimate is unbounded; finite budget cannot be admitted")
        if payload_bytes > max_payload_bytes:
            raise ValueError("plan exceeds the payload admission budget")
    return PlanEstimate(tuple(runs), active_seconds, payload_bits, payload_bytes)
