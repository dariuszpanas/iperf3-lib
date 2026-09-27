"""Deterministic analysis of canonical measurements without native execution.

Direction, endpoint observation and interval scope are independent selections.
All rates are bits/second; temporal statistics weight interval-average rates by
measured duration. No function reparses native JSON or diagnoses physical causes.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from ._evidence import has_observed_evidence
from .result import IntervalStats, Result, SumStats

type Quality = Literal["complete", "partial", "insufficient_data"]
type Direction = Literal["client_to_server", "server_to_client"]
type Observation = Literal["sender", "receiver"]


@dataclass(frozen=True)
class EvidenceRef:
    """A canonical result pointer, optionally scoped to a caller's trial ID."""

    path: str
    trial_id: str | None = None


@dataclass(frozen=True)
class AnalysisDiagnostic:
    """An explicit data-quality or methodology limitation, never a root-cause claim."""

    code: str
    message: str
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Selection:
    """Select exactly one direction, endpoint observation and interval population."""

    direction: Direction
    observation: Observation
    scope: Literal["aggregate", "stream"] = "aggregate"
    stream_id: int | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous scope, stream identity or endpoint selections."""
        _selection(self.direction, self.observation)
        if self.scope not in ("aggregate", "stream"):
            raise ValueError("scope must be aggregate or stream")
        if self.scope == "aggregate" and self.stream_id is not None:
            raise ValueError("aggregate selection cannot identify a stream")
        if self.scope == "stream":
            _count(self.stream_id, "stream_id")


@dataclass(frozen=True)
class IntervalPolicy:
    """Recorded omission, duration-derivation and temporal-precision decisions."""

    unknown_omission: Literal["exclude", "include"] = "exclude"
    derive_duration_from_bounds: bool = False
    time_tolerance_seconds: float = 0.000001
    minimum_intervals: int = 2

    def __post_init__(self) -> None:
        """Validate finite tolerances and an explicit minimum sample count."""
        if self.unknown_omission not in ("exclude", "include"):
            raise ValueError("unknown_omission must be exclude or include")
        if type(self.derive_duration_from_bounds) is not bool:
            raise ValueError("derive_duration_from_bounds must be boolean")
        _number(self.time_tolerance_seconds, "time_tolerance_seconds")
        if _count(self.minimum_intervals, "minimum_intervals") < 1:
            raise ValueError("minimum_intervals must be positive")


@dataclass(frozen=True)
class Coverage:
    """Measured population coverage with unknown denominators left unavailable."""

    selected_count: int = 0
    included_count: int = 0
    omitted_count: int = 0
    unknown_omission_count: int = 0
    invalid_or_missing_count: int = 0
    measured_seconds: float = 0
    byte_covered_seconds: float = 0
    observed_span_seconds: float | None = None
    measured_time_fraction: float | None = None


@dataclass(frozen=True)
class ThroughputAnalysis:
    """An endpoint's measured bytes divided by its measured traffic duration."""

    quality: Quality
    direction: Direction
    observation: Observation
    bytes: int | None = None
    measured_seconds: float | None = None
    throughput_bps: float | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class StabilityAnalysis:
    """Duration-weighted statistics of one population of interval-average rates."""

    quality: Quality
    selection: Selection
    policy: IntervalPolicy
    coverage: Coverage
    interval_rate_basis: Literal["bytes", "reported", "mixed", "unavailable"] = "unavailable"
    minimum_interval_average_bps: float | None = None
    duration_weighted_mean_interval_average_bps: float | None = None
    duration_weighted_stddev_interval_average_bps: float | None = None
    interval_average_coefficient_of_variation: float | None = None
    duration_weighted_interval_average_quantiles_bps: dict[float, float] = field(
        default_factory=dict
    )
    below_threshold_fraction_of_measured_time: float | None = None
    interval_bytes_throughput_bps: float | None = None
    threshold_bps: float | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class StreamRate:
    """A run-local stream average with its own measured duration and evidence."""

    stream_id: int
    throughput_bps: float | None
    measured_seconds: float | None
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class StreamBalanceAnalysis:
    """Descriptive balance of endpoint-specific stream averages, with coverage."""

    quality: Quality
    direction: Direction
    observation: Observation
    source: Literal["summaries", "intervals"]
    per_stream: tuple[StreamRate, ...]
    expected_streams: int | None
    valid_streams: int
    stream_coverage_fraction: float | None
    min_over_max_rate: float | None = None
    population_rate_cv: float | None = None
    jain_fairness_index: float | None = None
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class AnalysisTrial:
    """A caller-owned trial identity paired with its preserved canonical result."""

    trial_id: str
    result: Result


_COMPARISON_FIELDS = frozenset(
    {
        "protocol",
        "method",
        "native_version",
        "native_system_info",
        "server",
        "port",
        "duration",
        "omit",
        "parallel",
        "rate",
        "blksize",
        "tos",
        "rate_intent",
    }
)

# Frozen additions to report-v1 compatibility. Do not derive these defaults
# from the current ClientConfig: archived comparisons must not acquire new
# requirements when a future release adds settings or changes defaults.
_EXPANDED_DEFAULTS_V1: dict[str, object] = {
    "mptcp": False,
    "json_stream": False,
    "bind_address": None,
    "bind_device": None,
    "client_port": None,
    "address_family": "auto",
    "socket_buffer_bytes": None,
    "congestion_control": None,
    "no_delay": False,
    "mss": None,
    "connect_timeout_ms": None,
    "bytes_to_send": None,
    "blocks_to_send": None,
    "interval_seconds": None,
    "pacing_timer_us": None,
    "fq_rate_bps": None,
    "burst_packets": None,
    "zerocopy": False,
    "skip_rx_copy": False,
    "udp_counters_64bit": False,
    "dont_fragment": False,
    "flow_label": None,
    "sctp_streams": None,
    "sctp_bind_addresses": [],
    "payload_file": None,
    "repeating_payload": False,
    "affinity": None,
    "server_affinity": None,
    "get_server_output": False,
    "title": None,
    "extra_data": None,
    "receive_timeout_ms": None,
    "send_timeout_ms": None,
    "control_keepalive": None,
    "gsro": False,
    "username": None,
    "rsa_public_key_path": None,
    "use_pkcs1_padding": False,
}


def _expanded_comparison_fields_v1(
    results: Sequence[Result], policy: ComparisonPolicy
) -> frozenset[str]:
    """Freeze requested or policy-selected advanced fields for every trial."""
    names = (set(policy.varying_fields) | policy.allowed_differences.keys()) & (
        _EXPANDED_DEFAULTS_V1.keys()
    )
    for result in results:
        requested = result.execution.configuration.requested if result.execution else None
        if requested is None:
            continue
        for name, default in _EXPANDED_DEFAULTS_V1.items():
            if name in requested:
                value = requested[name]
                if type(value) is not type(default) or value != default:
                    names.add(name)
    return frozenset(names)


def _expanded_effective_v1(result: Result, name: str) -> object:
    """Read a typed advanced observation for both analysis and frozen v1 reports.

    Requested values select comparison dimensions; they never establish actual
    native settings. Missing observations remain unknown even if both requests
    are identical. Bounds here describe native observations, including explicit
    zero/default sentinels, rather than constructor admission rules.
    """
    setting = result.execution.configuration.effective.get(name) if result.execution else None
    if setting is None:
        return None
    if setting.state not in {"verified", "unavailable", "unsupported"}:
        raise ValueError("unknown effective-setting state")
    if setting.state != "verified" or not has_observed_evidence(result, setting.evidence_paths):
        return None
    value = setting.value
    if value is None:
        return None
    if type(_EXPANDED_DEFAULTS_V1[name]) is bool:
        if type(value) is not bool:
            raise ValueError(f"effective {name} must be a boolean")
    elif name in {
        "bind_address",
        "bind_device",
        "congestion_control",
        "payload_file",
        "title",
        "extra_data",
        "username",
        "rsa_public_key_path",
    }:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"effective {name} must be a nonempty NUL-free string")
    elif name == "address_family":
        if value not in ("auto", "ipv4", "ipv6"):
            raise ValueError("effective address_family must be auto, ipv4, or ipv6")
    elif name == "interval_seconds":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not (value == 0 or 0.1 <= value <= 60)
        ):
            raise ValueError("effective interval_seconds must be zero or between 0.1 and 60")
    elif name == "sctp_bind_addresses":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() and "\0" not in item for item in value
        ):
            raise ValueError("effective sctp_bind_addresses must be an array of address strings")
        value = list(value)
    elif name == "control_keepalive":
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not all(type(item) is int and 0 <= item <= 2**31 - 1 for item in value)
        ):
            raise ValueError("effective control_keepalive must contain three nonnegative integers")
        value = list(value)
    else:
        minimum, maximum = {
            "client_port": (0, 65535),
            "socket_buffer_bytes": (0, 2**31 - 1),
            "mss": (0, 65535),
            "connect_timeout_ms": (-1, 2**31 - 1),
            "bytes_to_send": (0, 2**64 - 1),
            "blocks_to_send": (0, 2**64 - 1),
            "pacing_timer_us": (0, 2**31 - 1),
            "fq_rate_bps": (0, 2**64 - 1),
            "burst_packets": (0, 1000),
            "flow_label": (0, 0xFFFFF),
            "sctp_streams": (0, 65535),
            "affinity": (-1, 1024),
            "server_affinity": (-1, 1024),
            "receive_timeout_ms": (0, 86400000),
            "send_timeout_ms": (0, 86400000),
        }[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"effective {name} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class ComparisonPolicy:
    """Experiment identity and explicit reasons for relaxing concrete compatibility checks."""

    group_id: str
    endpoint_pair: tuple[str, str]
    varying_fields: tuple[str, ...] = ()
    allowed_differences: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject unknown override names, empty identities and unexplained differences."""
        if not isinstance(self.group_id, str) or not self.group_id.strip():
            raise ValueError("group_id must be a nonempty string")
        if (
            type(self.endpoint_pair) is not tuple
            or len(self.endpoint_pair) != 2
            or not all(isinstance(value, str) and value.strip() for value in self.endpoint_pair)
        ):
            raise ValueError("endpoint_pair must contain two nonempty endpoint identities")
        if type(self.varying_fields) is not tuple or len(set(self.varying_fields)) != len(
            self.varying_fields
        ):
            raise ValueError("varying_fields must be a tuple of distinct field names")
        if type(self.allowed_differences) is not dict:
            raise ValueError("allowed_differences must be a mapping of field names to reasons")
        if set(self.varying_fields) - (
            _COMPARISON_FIELDS | _EXPANDED_DEFAULTS_V1.keys()
        ) or self.allowed_differences.keys() - (_COMPARISON_FIELDS | _EXPANDED_DEFAULTS_V1.keys()):
            raise ValueError("unknown comparison field")
        if not all(
            isinstance(reason, str) and reason.strip()
            for reason in self.allowed_differences.values()
        ):
            raise ValueError("each allowed difference requires a nonempty reason")


@dataclass(frozen=True)
class CompatibilityAnalysis:
    """Concrete comparison fingerprints, policy decisions and missing evidence."""

    quality: Quality
    compatible: bool
    policy: ComparisonPolicy
    fingerprints: dict[str, dict[str, object]]
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class TrialGroup:
    """Valid trial rates at one tested stream count and their median."""

    stream_count: int
    trial_ids: tuple[str, ...]
    throughput_bps: tuple[float, ...]
    median_throughput_bps: float | None


@dataclass(frozen=True)
class ScalingAnalysis:
    """The smallest tested count reaching a fraction of the best eligible median."""

    quality: Quality
    direction: Direction
    observation: Observation
    compatibility: ComparisonPolicy
    per_count_trial_summary: tuple[TrialGroup, ...]
    best_fraction: float
    minimum_valid_trials: int
    best_observed_bps: float | None = None
    smallest_tested_qualifying_count: int | None = None
    excluded_trials: tuple[str, ...] = ()
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


@dataclass(frozen=True)
class SequentialMethodology:
    """Recorded order and cooldown for a sequential forward/reverse experiment."""

    pair_id: str
    comparison_policy: ComparisonPolicy
    execution_order: Literal["forward_then_reverse", "reverse_then_forward"]
    cooldown_seconds: float | None

    def __post_init__(self) -> None:
        """Require explicit experiment identity, order and known-or-unknown cooldown."""
        if not isinstance(self.pair_id, str) or not self.pair_id.strip():
            raise ValueError("pair_id must be a nonempty string")
        if not isinstance(self.comparison_policy, ComparisonPolicy):
            raise ValueError("comparison_policy must be a ComparisonPolicy")
        if self.execution_order not in ("forward_then_reverse", "reverse_then_forward"):
            raise ValueError("execution_order must specify forward/reverse order")
        if self.cooldown_seconds is not None:
            _number(self.cooldown_seconds, "cooldown_seconds")


@dataclass(frozen=True)
class AsymmetryAnalysis:
    """Directional differences under one explicitly recorded experiment methodology."""

    quality: Quality
    methodology: Literal["simultaneous_bidirectional", "sequential_forward_reverse"]
    observation: Observation
    sequential_methodology: SequentialMethodology | None = None
    client_to_server_bps: float | None = None
    server_to_client_bps: float | None = None
    signed_difference_bps: float | None = None
    client_to_server_over_server_to_client: float | None = None
    normalized_signed_difference: float | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[AnalysisDiagnostic, ...] = ()


def _number(value, name: str, *, signed: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is outside the finite numeric range") from exc
    if not math.isfinite(number) or (not signed and number < 0):
        raise ValueError(f"{name} must be finite{' and nonnegative' if not signed else ''}")
    return number


def _count(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _selection(direction, observation) -> None:
    if direction not in ("client_to_server", "server_to_client"):
        raise ValueError("direction must be client_to_server or server_to_client")
    if observation not in ("sender", "receiver"):
        raise ValueError("observation must be sender or receiver")


def _diagnostic(code: str, message: str, *paths: str) -> AnalysisDiagnostic:
    return AnalysisDiagnostic(code, message, tuple(EvidenceRef(path) for path in paths))


def _completed(result: Result) -> bool:
    if not isinstance(result, Result) or type(result.ok) is not bool:
        raise ValueError("result must be a Result with boolean ok")
    if result.execution is not None and result.execution.status not in (
        "completed",
        "failed",
        "incomplete",
    ):
        raise ValueError("unknown execution status")
    return (
        result.ok
        and result.error is None
        and (result.execution is None or result.execution.status == "completed")
    )


def _measurement(stats: SumStats | IntervalStats, path: str):
    if not isinstance(stats, (SumStats, IntervalStats)):
        raise ValueError(f"{path} must be a statistics dataclass")
    count = None if stats.bytes is None else _count(stats.bytes, f"{path}/bytes")
    duration = (
        None
        if stats.duration_seconds is None
        else _number(stats.duration_seconds, f"{path}/duration_seconds")
    )
    if stats.bits_per_second is not None:
        _number(stats.bits_per_second, f"{path}/bits_per_second")
    if stats.omitted is not None and type(stats.omitted) is not bool:
        raise ValueError(f"{path}/omitted must be boolean or None")
    return count, duration


def _divide_bytes(count: int, duration: float) -> float:
    try:
        return _number(8 * count / duration, "derived throughput")
    except OverflowError as exc:
        raise ValueError("derived throughput is outside the finite numeric range") from exc


def summary_throughput(
    result: Result, *, direction: Direction, observation: Observation
) -> ThroughputAnalysis:
    """Derive one flow endpoint's rate from bytes and measured traffic duration."""
    _selection(direction, observation)
    if not _completed(result):
        return ThroughputAnalysis(
            "insufficient_data",
            direction,
            observation,
            diagnostics=(
                _diagnostic(
                    "execution.not_completed",
                    "A failed or incomplete run cannot supply a successful performance comparison.",
                    "/execution/status",
                    "/ok",
                ),
            ),
        )
    matches = [
        (index, flow) for index, flow in enumerate(result.flows) if flow.direction == direction
    ]
    if len(matches) != 1:
        return ThroughputAnalysis(
            "insufficient_data",
            direction,
            observation,
            diagnostics=(
                _diagnostic(
                    "selection.ambiguous",
                    "Exactly one selected directional flow is required.",
                    "/flows",
                ),
            ),
        )
    index, flow = matches[0]
    path = f"/flows/{index}/{observation}"
    stats = getattr(flow, observation)
    if stats is None:
        return ThroughputAnalysis(
            "insufficient_data",
            direction,
            observation,
            diagnostics=(
                _diagnostic("measurement.absent", "The selected endpoint summary is absent.", path),
            ),
        )
    if stats.direction not in (None, direction) or stats.observation not in (None, observation):
        raise ValueError("summary provenance conflicts with its selected container")
    count, duration = _measurement(stats, path)
    evidence = (EvidenceRef(f"{path}/bytes"), EvidenceRef(f"{path}/duration_seconds"))
    if count is None or duration is None or duration <= 0 or stats.omitted is True:
        return ThroughputAnalysis(
            "insufficient_data",
            direction,
            observation,
            count,
            duration,
            evidence=evidence,
            diagnostics=(
                _diagnostic(
                    "measurement.insufficient",
                    "Non-omitted bytes and positive measured duration are required.",
                    path,
                ),
            ),
        )
    return ThroughputAnalysis(
        "complete",
        direction,
        observation,
        count,
        duration,
        _divide_bytes(count, duration),
        evidence,
    )


@dataclass(frozen=True)
class _Sample:
    start: float
    end: float
    duration: float
    rate: float
    bytes: int | None
    path: str


def _interval_samples(result: Result, selection: Selection, policy: IntervalPolicy):
    samples: list[_Sample] = []
    diagnostics: list[AnalysisDiagnostic] = []
    selected = omitted = unknown = missing = 0
    bounds: list[tuple[float, float]] = []
    coverage_unknown = False
    for index, interval in enumerate(result.intervals):
        path = f"/intervals/{index}"
        if not isinstance(interval, IntervalStats):
            raise ValueError(f"{path} must be an IntervalStats")
        if (
            interval.direction not in ("client_to_server", "server_to_client", "unknown")
            or interval.observation not in ("sender", "receiver", None)
            or interval.scope not in ("aggregate", "stream", "unknown")
        ):
            raise ValueError(f"{path} has invalid direction, observation or scope")
        if (
            interval.direction not in (selection.direction, "unknown")
            or interval.observation not in (selection.observation, None)
            or interval.scope not in (selection.scope, "unknown")
        ):
            continue
        if selection.scope == "stream" and interval.stream_id not in (selection.stream_id, None):
            continue
        selected += 1
        count, duration = _measurement(interval, path)
        if interval.stream_id is not None:
            _count(interval.stream_id, f"{path}/stream_id")
        start = (
            None
            if interval.start_seconds is None
            else _number(interval.start_seconds, f"{path}/start_seconds", signed=True)
        )
        end = (
            None
            if interval.end_seconds is None
            else _number(interval.end_seconds, f"{path}/end_seconds", signed=True)
        )
        if interval.omitted is True:
            omitted += 1
            diagnostics.append(
                _diagnostic("interval.omitted", "Explicit warm-up interval excluded.", path)
            )
            continue
        if interval.omitted is None:
            unknown += 1
            coverage_unknown = True
            diagnostics.append(
                _diagnostic(
                    "interval.unknown_omission",
                    f"Unknown omission state {policy.unknown_omission}d by recorded policy.",
                    path,
                )
            )
            if policy.unknown_omission == "exclude":
                continue
        if (
            interval.direction != selection.direction
            or interval.observation != selection.observation
            or interval.scope != selection.scope
            or (selection.scope == "stream" and interval.stream_id != selection.stream_id)
        ):
            missing += 1
            coverage_unknown = True
            diagnostics.append(
                _diagnostic(
                    "provenance.unknown",
                    "Interval provenance does not establish the selected population.",
                    path,
                )
            )
            continue
        if selection.scope == "aggregate" and interval.stream_id is not None:
            raise ValueError("aggregate interval cannot carry a component stream ID")
        if start is None or end is None:
            missing += 1
            coverage_unknown = True
            diagnostics.append(
                _diagnostic(
                    "interval.missing_bounds",
                    "Bounds are required to establish temporal coverage.",
                    path,
                )
            )
            continue
        if end < start:
            raise ValueError(f"{path} end must not precede start")
        if end == start:
            missing += 1
            coverage_unknown = True
            diagnostics.append(
                _diagnostic(
                    "interval.zero_width", "A zero-width interval supplies no measured time.", path
                )
            )
            continue
        bounds.append((start, end))
        if duration is None and policy.derive_duration_from_bounds:
            duration = _number(end - start, "derived duration")
            diagnostics.append(
                _diagnostic(
                    "duration.derived",
                    "Duration was derived from interval boundaries by explicit policy.",
                    f"{path}/start_seconds",
                    f"{path}/end_seconds",
                )
            )
        if duration is None or duration == 0:
            missing += 1
            diagnostics.append(
                _diagnostic(
                    "measurement.missing_duration", "Positive measured duration is required.", path
                )
            )
            continue
        if not math.isclose(
            duration, end - start, rel_tol=1e-9, abs_tol=policy.time_tolerance_seconds
        ):
            missing += 1
            coverage_unknown = True
            diagnostics.append(
                _diagnostic(
                    "interval.duration_conflict",
                    "Measured duration conflicts with interval bounds.",
                    path,
                )
            )
            continue
        if count is not None:
            rate = _divide_bytes(count, duration)
            if interval.bits_per_second is not None and not math.isclose(
                rate, interval.bits_per_second, rel_tol=1e-6, abs_tol=1
            ):
                diagnostics.append(
                    _diagnostic(
                        "measurement.rate_difference",
                        "Native reported rate differs from bytes/time; bytes/time is used.",
                        f"{path}/bits_per_second",
                        f"{path}/bytes",
                        f"{path}/duration_seconds",
                    )
                )
        elif interval.bits_per_second is not None:
            rate = _number(interval.bits_per_second, f"{path}/bits_per_second")
            diagnostics.append(
                _diagnostic(
                    "measurement.reported_rate",
                    "Reported rate supports stability; missing bytes prevent complete bytes/time throughput.",
                    path,
                )
            )
        else:
            missing += 1
            diagnostics.append(
                _diagnostic(
                    "measurement.missing_rate",
                    "Neither bytes/time nor reported rate is available.",
                    path,
                )
            )
            continue
        samples.append(_Sample(start, end, duration, rate, count, path))
    samples.sort(key=lambda sample: (sample.start, sample.end))
    overlap = any(
        right.start < left.end - policy.time_tolerance_seconds
        for left, right in zip(samples, samples[1:], strict=False)
    )
    gaps = any(
        right.start > left.end + policy.time_tolerance_seconds
        for left, right in zip(samples, samples[1:], strict=False)
    )
    if overlap:
        diagnostics.append(
            _diagnostic(
                "interval.overlap",
                "Overlapping or duplicate intervals cannot be counted as independent measured time.",
                *(sample.path for sample in samples),
            )
        )
    if gaps:
        diagnostics.append(
            _diagnostic(
                "interval.gaps",
                "Unmeasured gaps are excluded; no zero rates were inserted.",
                *(sample.path for sample in samples),
            )
        )
    try:
        duration = _number(math.fsum(sample.duration for sample in samples), "total duration")
        byte_duration = _number(
            math.fsum(sample.duration for sample in samples if sample.bytes is not None),
            "byte-covered duration",
        )
    except OverflowError as exc:
        raise ValueError("combined durations exceed finite numeric range") from exc
    span = (
        max(end for _, end in bounds) - min(start for start, _ in bounds)
        if bounds and not coverage_unknown
        else None
    )
    if span is not None:
        span = _number(span, "observed span")
    fraction = min(1.0, duration / span) if span is not None and span > 0 and not overlap else None
    coverage = Coverage(
        selected, len(samples), omitted, unknown, missing, duration, byte_duration, span, fraction
    )
    return samples, coverage, diagnostics, overlap, gaps


_DEFAULT_INTERVAL_POLICY = IntervalPolicy()


def interval_stability(
    result: Result,
    *,
    selection: Selection,
    threshold_bps: float | None = None,
    quantiles: tuple[float, ...] = (0.5, 0.95),
    policy: IntervalPolicy = _DEFAULT_INTERVAL_POLICY,
) -> StabilityAnalysis:
    """Describe time-weighted interval-average throughput, never packet performance.

    Quantiles use the smallest rate whose cumulative duration reaches the chosen
    fraction; no interpolation is performed. Time below threshold uses strict
    less-than comparison and only included measured time as its denominator.
    """
    if not isinstance(selection, Selection) or not isinstance(policy, IntervalPolicy):
        raise ValueError("selection and policy must use their declared dataclasses")
    if threshold_bps is not None:
        threshold_bps = _number(threshold_bps, "threshold_bps")
    if type(quantiles) is not tuple:
        raise ValueError("quantiles must be a tuple")
    for quantile in quantiles:
        if _number(quantile, "quantile") > 1:
            raise ValueError("quantiles must be between zero and one")
    if len(set(quantiles)) != len(quantiles):
        raise ValueError("quantiles must be distinct")
    if not _completed(result):
        return StabilityAnalysis(
            "insufficient_data",
            selection,
            policy,
            Coverage(),
            threshold_bps=threshold_bps,
            diagnostics=(
                _diagnostic(
                    "execution.not_completed",
                    "Run did not complete successfully.",
                    "/execution/status",
                ),
            ),
        )
    samples, coverage, diagnostics, overlap, gaps = _interval_samples(result, selection, policy)
    evidence = tuple(EvidenceRef(sample.path) for sample in samples)
    if not samples or overlap:
        if not samples:
            diagnostics.append(
                _diagnostic(
                    "measurement.insufficient",
                    "No eligible measured intervals remain.",
                    "/intervals",
                )
            )
        return StabilityAnalysis(
            "insufficient_data",
            selection,
            policy,
            coverage,
            threshold_bps=threshold_bps,
            evidence=evidence,
            diagnostics=tuple(diagnostics),
        )
    total = coverage.measured_seconds
    mean = _number(
        math.fsum(sample.duration / total * sample.rate for sample in samples), "weighted mean"
    )
    scale = max(sample.rate for sample in samples)
    variance_scaled = (
        math.fsum(
            sample.duration / total * ((sample.rate - mean) / scale) ** 2 for sample in samples
        )
        if scale
        else 0
    )
    stddev = _number(math.sqrt(variance_scaled) * scale, "weighted standard deviation")
    quantile_values: dict[float, float] = {}
    ordered = sorted(samples, key=lambda sample: sample.rate)
    for quantile in quantiles:
        cumulative = 0.0
        quantile_values[quantile] = ordered[-1].rate
        for sample in ordered:
            cumulative = math.fsum((cumulative, sample.duration))
            if cumulative >= quantile * total:
                quantile_values[quantile] = sample.rate
                break
    complete_bytes = all(sample.bytes is not None for sample in samples)
    quality: Quality = "complete"
    if (
        coverage.unknown_omission_count
        or coverage.invalid_or_missing_count
        or gaps
        or not complete_bytes
        or any(
            item.code in {"duration.derived", "measurement.rate_difference"} for item in diagnostics
        )
    ):
        quality = "partial"
    if len(samples) < policy.minimum_intervals:
        quality = "insufficient_data"
        diagnostics.append(
            _diagnostic(
                "stability.too_few_intervals",
                "Fewer intervals than the recorded stability policy requires.",
                *(sample.path for sample in samples),
            )
        )
    if mean == 0:
        diagnostics.append(
            _diagnostic(
                "ratio.zero_mean",
                "Coefficient of variation is undefined for zero mean throughput.",
                *(sample.path for sample in samples),
            )
        )
    return StabilityAnalysis(
        quality,
        selection,
        policy,
        coverage,
        "bytes"
        if complete_bytes
        else "reported"
        if all(sample.bytes is None for sample in samples)
        else "mixed",
        min(sample.rate for sample in samples),
        mean,
        stddev,
        stddev / mean if mean else None,
        quantile_values,
        math.fsum(sample.duration for sample in samples if sample.rate < threshold_bps) / total
        if threshold_bps is not None
        else None,
        _divide_bytes(sum(sample.bytes for sample in samples if sample.bytes is not None), total)
        if complete_bytes
        else None,
        threshold_bps,
        evidence,
        tuple(diagnostics),
    )


def _effective(result: Result, name: str):
    if result.execution is None:
        return None
    value = result.execution.configuration.effective.get(name)
    if value is None:
        return None
    if value.state not in ("verified", "unavailable", "unsupported"):
        raise ValueError("unknown effective-setting state")
    if value.state != "verified" or not has_observed_evidence(result, value.evidence_paths):
        return None
    observed = value.value
    if name in {"port", "duration", "omit", "parallel", "rate", "blksize", "tos"}:
        observed = _count(observed, f"effective {name}")
        bounds = {
            "port": (1, 65535),
            "duration": (0, 86400),
            "omit": (0, 600),
            "parallel": (1, 128),
            "rate": (0, 2**64 - 1),
            "blksize": (0, 1024 * 1024),
            "tos": (0, 255),
        }
        minimum, maximum = bounds[name]
        if not minimum <= observed <= maximum:
            raise ValueError(f"effective {name} must be between {minimum} and {maximum}")
    elif name == "server" and (not isinstance(observed, str) or not observed.strip()):
        raise ValueError("effective server must be a string")
    return observed


def stream_balance(
    result: Result,
    *,
    direction: Direction,
    observation: Observation,
    source: Literal["summaries", "intervals"] = "summaries",
    policy: IntervalPolicy = _DEFAULT_INTERVAL_POLICY,
) -> StreamBalanceAnalysis:
    """Compare endpoint-specific stream averages without treating missing streams as zero."""
    _selection(direction, observation)
    if source not in ("summaries", "intervals") or not isinstance(policy, IntervalPolicy):
        raise ValueError("source must be summaries or intervals, with an IntervalPolicy")
    diagnostics: list[AnalysisDiagnostic] = []
    rates: list[StreamRate] = []
    expected = _effective(result, "parallel")
    if expected is not None and _count(expected, "effective parallel") < 1:
        raise ValueError("effective parallel must be positive")
    if not _completed(result):
        return StreamBalanceAnalysis(
            "insufficient_data",
            direction,
            observation,
            source,
            (),
            expected,
            0,
            None,
            diagnostics=(
                _diagnostic(
                    "execution.not_completed",
                    "Run did not complete successfully.",
                    "/execution/status",
                ),
            ),
        )
    seen: set[int] = set()
    if source == "summaries":
        for index, stream in enumerate(result.streams):
            if stream.direction != direction:
                continue
            path = f"/streams/{index}/{observation}"
            if stream.stream_id is None:
                diagnostics.append(
                    _diagnostic(
                        "stream.unknown_identity",
                        "A selected stream has no local socket identity.",
                        f"/streams/{index}",
                    )
                )
                continue
            identity = _count(stream.stream_id, "stream_id")
            if identity in seen:
                raise ValueError("duplicate local stream identity")
            seen.add(identity)
            stats = getattr(stream, observation)
            if stats is not None and (
                stats.direction not in (None, direction)
                or stats.observation not in (None, observation)
            ):
                raise ValueError("stream summary provenance conflicts with its container")
            count, duration = (None, None) if stats is None else _measurement(stats, path)
            rate = (
                _divide_bytes(count, duration)
                if count is not None
                and duration is not None
                and duration > 0
                and stats.omitted is not True
                else None
            )
            rates.append(StreamRate(identity, rate, duration, (EvidenceRef(path),)))
    else:
        identities = {
            interval.stream_id
            for interval in result.intervals
            if interval.scope == "stream"
            and interval.direction == direction
            and interval.observation == observation
            and interval.stream_id is not None
        }
        for identity in sorted(identities):
            analysis = interval_stability(
                result,
                selection=Selection(direction, observation, "stream", identity),
                policy=replace(policy, minimum_intervals=1),
            )
            rates.append(
                StreamRate(
                    identity,
                    analysis.interval_bytes_throughput_bps,
                    analysis.coverage.measured_seconds,
                    analysis.evidence,
                )
            )
            diagnostics.extend(analysis.diagnostics)
    valid = [item.throughput_bps for item in rates if item.throughput_bps is not None]
    coverage = len(valid) / expected if expected is not None else None
    quality: Quality = "complete"
    if (
        expected is None
        or len(valid) != expected
        or len(valid) != len(rates)
        or any(item.code != "interval.omitted" for item in diagnostics)
    ):
        quality = "partial"
        diagnostics.append(
            _diagnostic(
                "stream.incomplete_coverage",
                "Stream coverage is incomplete or its expected count is unknown.",
                "/streams",
                "/execution/configuration/effective/parallel",
            )
        )
    if not valid or (expected is not None and len(rates) > expected):
        quality = "insufficient_data"
    ratio = cv = fairness = None
    if valid and quality != "insufficient_data":
        maximum = max(valid)
        if maximum > 0:
            scaled = [rate / maximum for rate in valid]
            ratio = min(scaled)
            cv = statistics.pstdev(scaled) / statistics.mean(scaled)
            fairness = math.fsum(scaled) ** 2 / (
                len(scaled) * math.fsum(rate * rate for rate in scaled)
            )
        else:
            diagnostics.append(
                _diagnostic(
                    "ratio.zero_mean",
                    "All stream rates are zero; relative balance measures are undefined.",
                    "/streams",
                )
            )
    return StreamBalanceAnalysis(
        quality,
        direction,
        observation,
        source,
        tuple(rates),
        expected,
        len(valid),
        coverage,
        ratio,
        cv,
        fairness,
        tuple(diagnostics),
    )


def _fingerprint(result: Result, expanded: frozenset[str] = frozenset()) -> dict[str, object]:
    metadata = result.execution
    if result.protocol not in ("tcp", "udp", "sctp", None):
        raise ValueError("unknown protocol")
    if metadata is not None:
        if metadata.method not in ("forward", "reverse", "bidirectional", "unknown"):
            raise ValueError("unknown execution method")
        for name in ("native_version", "native_system_info"):
            value = getattr(metadata, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty string or None")
    fingerprint: dict[str, object] = {
        "protocol": result.protocol,
        "method": metadata.method if metadata else None,
        "native_version": metadata.native_version if metadata else None,
        "native_system_info": metadata.native_system_info if metadata else None,
        "rate_intent": _rate_intent(result),
    }
    for name in ("server", "port", "duration", "omit", "parallel", "rate", "blksize", "tos"):
        fingerprint[name] = _effective(result, name)
    for name in sorted(expanded):
        fingerprint[name] = _expanded_effective_v1(result, name)
    return fingerprint


def _rate_intent(result: Result) -> dict[str, object]:
    extension = result.extensions.get("iperf3_lib.rate_intent")
    if extension is None:
        return {"basis": "native_per_stream"}
    if (
        not isinstance(extension, dict)
        or type(extension.get("schema_version")) is not int
        or extension["schema_version"] != 1
    ):
        raise ValueError("unsupported rate intent extension schema")
    resolution, intent = extension.get("resolution"), extension.get("intent")
    if not isinstance(resolution, dict) or resolution.get("source") not in {
        "legacy",
        "per_stream",
        "aggregate",
        "protocol_default",
    }:
        raise ValueError("rate intent resolution is malformed")
    if resolution["source"] == "aggregate":
        if not isinstance(intent, dict):
            raise ValueError("aggregate rate intent is missing")
        target = _count(intent.get("aggregate_bps_per_direction"), "aggregate intent")
        if not 1 <= target <= 2**64 - 1:
            raise ValueError("aggregate rate intent must be a positive uint64 value")
        return {"basis": "aggregate_per_direction", "target_bps": target}
    return {"basis": "native_per_stream"}


def _trial_ids(trials: Sequence[AnalysisTrial]) -> None:
    if any(not isinstance(trial, AnalysisTrial) for trial in trials):
        raise ValueError("trials must contain AnalysisTrial objects")
    ids = [trial.trial_id for trial in trials]
    if any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(
        set(ids)
    ) != len(ids):
        raise ValueError("trial IDs must be nonempty and unique")
    for trial in trials:
        _completed(trial.result)


def check_compatibility(
    trials: Sequence[AnalysisTrial], *, policy: ComparisonPolicy
) -> CompatibilityAnalysis:
    """Check concrete evidence under an explicit comparison policy.

    Every fixed field, including parallel streams, must be present and equal.
    ``varying_fields`` declares observed experimental variables; ``allowed_differences``
    permits observed differences with retained reasons, but cannot supply missing
    evidence. Equal aggregate rate intent permits the resolved per-stream rate to
    differ only when each verified rate matches the declared allocation. Scaling adds ``parallel`` to its varying fields; sequential asymmetry
    adds ``method``. Caller group and endpoint labels never establish compatibility
    on their own. Execution success and throughput quality are separate checks.
    Nondefault advanced requests and policy-named advanced fields are compared
    for every trial using observed native settings. Missing receipts are never
    replaced with a requested value or an assumed protocol default.
    """
    if not isinstance(policy, ComparisonPolicy):
        raise ValueError("policy must be a ComparisonPolicy")
    policy.__post_init__()
    _trial_ids(trials)
    expanded = _expanded_comparison_fields_v1([trial.result for trial in trials], policy)
    fingerprints = {trial.trial_id: _fingerprint(trial.result, expanded) for trial in trials}
    diagnostics: list[AnalysisDiagnostic] = []
    incompatible = not trials
    varying = set(policy.varying_fields)
    aggregate_intents = [item["rate_intent"] for item in fingerprints.values()]
    shared_aggregate = aggregate_intents and all(
        isinstance(intent, dict)
        and intent.get("basis") == "aggregate_per_direction"
        and intent == aggregate_intents[0]
        for intent in aggregate_intents
    )
    if shared_aggregate:
        target = _count(_rate_intent(trials[0].result)["target_bps"], "aggregate intent")
        valid_resolution = True
        for trial_id, fingerprint in fingerprints.items():
            parallel, rate = fingerprint["parallel"], fingerprint["rate"]
            if (
                type(parallel) is not int
                or parallel < 1
                or target < parallel
                or rate != target // parallel
            ):
                valid_resolution = False
                incompatible = True
                diagnostics.append(
                    AnalysisDiagnostic(
                        "comparison.rate_intent_conflict",
                        "Verified native rate must equal aggregate intent floor-divided by verified stream count.",
                        (
                            EvidenceRef("/execution/configuration/effective/rate", trial_id),
                            EvidenceRef("/execution/configuration/effective/parallel", trial_id),
                            EvidenceRef("/extensions/iperf3_lib.rate_intent", trial_id),
                        ),
                    )
                )
        if valid_resolution:
            varying.add("rate")
    if not trials:
        diagnostics.append(_diagnostic("comparison.empty", "No trial evidence was supplied."))
    for name in sorted(_COMPARISON_FIELDS | expanded):
        values = [fingerprint[name] for fingerprint in fingerprints.values()]
        missing = any(
            value is None or (name not in expanded and value == "unknown") for value in values
        )
        differs = bool(values) and any(value != values[0] for value in values[1:])
        if missing or (differs and name not in varying):
            path = (
                f"/execution/configuration/effective/{name}"
                if name
                in {"server", "port", "duration", "omit", "parallel", "rate", "blksize", "tos"}
                | expanded
                else "/extensions/iperf3_lib.rate_intent"
                if name == "rate_intent"
                else "/protocol"
                if name == "protocol"
                else f"/execution/{name}"
            )
            evidence = tuple(EvidenceRef(path, trial.trial_id) for trial in trials)
            reason = policy.allowed_differences.get(name)
            allowed = reason is not None and not missing
            detail = (
                "missing compatibility evidence"
                if missing
                else reason or "differing compatibility evidence"
            )
            if missing and reason:
                detail += f"; a difference allowance cannot supply evidence ({reason})"
            diagnostics.append(
                AnalysisDiagnostic(
                    "comparison.allowed_difference" if allowed else "comparison.incompatible",
                    f"{name}: {detail}",
                    evidence,
                )
            )
            incompatible |= not allowed
    return CompatibilityAnalysis(
        "insufficient_data" if incompatible else "partial" if diagnostics else "complete",
        not incompatible,
        policy,
        fingerprints,
        tuple(diagnostics),
    )


def _compatibility(trials: Sequence[AnalysisTrial], policy: ComparisonPolicy, *, varying: set[str]):
    if not isinstance(policy, ComparisonPolicy):
        raise ValueError("compatibility must be a ComparisonPolicy")
    comparison = check_compatibility(
        trials,
        policy=replace(policy, varying_fields=tuple(sorted(set(policy.varying_fields) | varying))),
    )
    return list(comparison.diagnostics), not comparison.compatible


def stream_scaling(
    trials: Sequence[AnalysisTrial],
    *,
    direction: Direction,
    observation: Observation,
    compatibility: ComparisonPolicy,
    best_fraction: float = 0.95,
    minimum_valid_trials: int = 1,
) -> ScalingAnalysis:
    """Select the smallest tested count reaching a fraction of the best eligible median."""
    _selection(direction, observation)
    fraction = _number(best_fraction, "best_fraction")
    if not 0 < fraction <= 1:
        raise ValueError("best_fraction must be in (0, 1]")
    if _count(minimum_valid_trials, "minimum_valid_trials") < 1:
        raise ValueError("minimum_valid_trials must be positive")
    if not isinstance(compatibility, ComparisonPolicy):
        raise ValueError("compatibility must be a ComparisonPolicy")
    _trial_ids(trials)
    groups: dict[int, list[tuple[str, float]]] = {}
    excluded: list[str] = []
    diagnostics: list[AnalysisDiagnostic] = []
    eligible: list[AnalysisTrial] = []
    for trial in trials:
        throughput = summary_throughput(trial.result, direction=direction, observation=observation)
        parallel = _effective(trial.result, "parallel")
        if parallel is None or throughput.throughput_bps is None:
            excluded.append(trial.trial_id)
            diagnostics.append(
                AnalysisDiagnostic(
                    "trial.excluded",
                    "Completed throughput and verified stream count are required.",
                    (EvidenceRef("/execution", trial.trial_id),),
                )
            )
            continue
        if _count(parallel, "effective parallel") < 1:
            raise ValueError("effective parallel must be positive")
        groups.setdefault(parallel, []).append((trial.trial_id, throughput.throughput_bps))
        eligible.append(trial)
    compatibility_diagnostics, incompatible = _compatibility(
        eligible, compatibility, varying={"parallel"}
    )
    diagnostics.extend(compatibility_diagnostics)
    grouped = tuple(
        TrialGroup(
            count,
            tuple(identity for identity, _ in values),
            tuple(rate for _, rate in values),
            statistics.median(rate for _, rate in values)
            if len(values) >= minimum_valid_trials
            else None,
        )
        for count, values in sorted(groups.items())
    )
    medians = [
        group.median_throughput_bps for group in grouped if group.median_throughput_bps is not None
    ]
    best = max(medians) if medians and not incompatible else None
    chosen = min(
        (
            group.stream_count
            for group in grouped
            if group.median_throughput_bps is not None
            and best is not None
            and best > 0
            and group.median_throughput_bps >= fraction * best
        ),
        default=None,
    )
    quality: Quality = (
        "insufficient_data"
        if chosen is None
        else "partial"
        if excluded or diagnostics or any(group.median_throughput_bps is None for group in grouped)
        else "complete"
    )
    if best == 0:
        diagnostics.append(
            _diagnostic(
                "scaling.zero_reference",
                "All eligible medians are zero; no useful qualifying stream count is established.",
            )
        )
    return ScalingAnalysis(
        quality,
        direction,
        observation,
        compatibility,
        grouped,
        fraction,
        minimum_valid_trials,
        best,
        chosen,
        tuple(excluded),
        tuple(diagnostics),
    )


def _asymmetry(
    forward: ThroughputAnalysis,
    reverse: ThroughputAnalysis,
    observation: Observation,
    methodology: SequentialMethodology | None,
    diagnostics: list[AnalysisDiagnostic],
    incompatible: bool,
):
    mode = "sequential_forward_reverse" if methodology else "simultaneous_bidirectional"
    fwd, rev = forward.throughput_bps, reverse.throughput_bps
    evidence = forward.evidence + reverse.evidence
    diagnostics.extend(forward.diagnostics + reverse.diagnostics)
    if incompatible or fwd is None or rev is None:
        return AsymmetryAnalysis(
            "insufficient_data",
            mode,
            observation,
            methodology,
            fwd,
            rev,
            evidence=evidence,
            diagnostics=tuple(diagnostics),
        )
    maximum = max(fwd, rev)
    ratio = fwd / rev if rev > 0 else None
    if ratio is not None and not math.isfinite(ratio):
        ratio = None
        diagnostics.append(
            _diagnostic("ratio.out_of_range", "Directional ratio exceeds the finite numeric range.")
        )
    if not maximum:
        diagnostics.append(
            _diagnostic(
                "ratio.zero_reference", "Both rates are zero; relative asymmetry is undefined."
            )
        )
    return AsymmetryAnalysis(
        "partial" if diagnostics else "complete",
        mode,
        observation,
        methodology,
        fwd,
        rev,
        fwd - rev,
        ratio,
        (fwd - rev) / maximum if maximum else None,
        evidence,
        tuple(diagnostics),
    )


def simultaneous_asymmetry(result: Result, *, observation: Observation) -> AsymmetryAnalysis:
    """Compare two directions only within one explicitly simultaneous bidirectional run."""
    _selection("client_to_server", observation)
    if (
        result.execution is None
        or result.execution.method != "bidirectional"
        or result.bidirectional is not True
    ):
        return AsymmetryAnalysis(
            "insufficient_data",
            "simultaneous_bidirectional",
            observation,
            diagnostics=(
                _diagnostic(
                    "methodology.not_bidirectional",
                    "An explicitly simultaneous bidirectional result is required.",
                    "/execution/method",
                    "/bidirectional",
                ),
            ),
        )
    return _asymmetry(
        summary_throughput(result, direction="client_to_server", observation=observation),
        summary_throughput(result, direction="server_to_client", observation=observation),
        observation,
        None,
        [],
        False,
    )


def sequential_asymmetry(
    forward: Result,
    reverse: Result,
    *,
    observation: Observation,
    methodology: SequentialMethodology,
) -> AsymmetryAnalysis:
    """Compare forward/reverse trials under recorded sequential order and compatibility."""
    _selection("client_to_server", observation)
    if not isinstance(methodology, SequentialMethodology):
        raise ValueError("methodology must be a SequentialMethodology")
    methodology.__post_init__()
    diagnostics, incompatible = _compatibility(
        [AnalysisTrial("forward", forward), AnalysisTrial("reverse", reverse)],
        methodology.comparison_policy,
        varying={"method"},
    )
    if (
        forward.execution is None
        or reverse.execution is None
        or forward.execution.method != "forward"
        or reverse.execution.method != "reverse"
        or forward.bidirectional
        or reverse.bidirectional
    ):
        incompatible = True
        diagnostics.append(
            _diagnostic(
                "methodology.not_sequential_pair",
                "One forward run and one reverse run are required.",
                "/execution/method",
            )
        )
    if methodology.cooldown_seconds is None:
        diagnostics.append(
            _diagnostic("methodology.unknown_cooldown", "Sequential cooldown was not recorded.")
        )
    fwd = summary_throughput(forward, direction="client_to_server", observation=observation)
    rev = summary_throughput(reverse, direction="server_to_client", observation=observation)
    fwd = replace(fwd, evidence=tuple(replace(item, trial_id="forward") for item in fwd.evidence))
    rev = replace(rev, evidence=tuple(replace(item, trial_id="reverse") for item in rev.evidence))
    return _asymmetry(fwd, rev, observation, methodology, diagnostics, incompatible)
