"""Pure bounded UDP exploration with retained observations and explicit decisions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast

from ._evidence import has_observed_evidence
from .analysis import _EXPANDED_DEFAULTS_V1, _expanded_effective_v1
from .config import ClientConfig, Protocol
from .intent import MAX_RATE, RateIntent, resolve_rate
from .reports import (
    ReportValidationError,
    _summary_selection_v1,
    plan_result_from_dict,
    plan_result_to_dict,
)
from .result import JSONValue
from .sweeps import SettingCheck, _exact_equal
from .trials import PlanBudget, PlanResult, PreparedPlan, TrialPolicy, TrialSpec, prepare_plan


@dataclass(frozen=True)
class AdaptiveUDPPolicy:
    """Finite exploration limits and conservative all-observations acceptance."""

    min_rate_bps: int
    max_rate_bps: int
    max_distinct_rates: int
    max_refinement_depth: int
    receiver_loss_percent: float
    minimum_valid_trials: int
    minimum_sender_fraction: float
    confirmation_batches: int = 1

    def __post_init__(self) -> None:
        """Require explicit finite search limits and unambiguous thresholds."""
        for name in (
            "min_rate_bps",
            "max_rate_bps",
            "max_distinct_rates",
            "minimum_valid_trials",
            "confirmation_batches",
        ):
            _integer(getattr(self, name), name, 1)
        _integer(self.max_refinement_depth, "max_refinement_depth")
        if not self.min_rate_bps <= self.max_rate_bps <= MAX_RATE:
            raise ValueError("rate limits must be ordered positive uint64 values")
        _finite(self.receiver_loss_percent, "receiver_loss_percent")
        _finite(self.minimum_sender_fraction, "minimum_sender_fraction")
        if not 0 <= self.receiver_loss_percent <= 100:
            raise ValueError("receiver_loss_percent must lie between zero and 100")
        if not 0 < self.minimum_sender_fraction <= 1:
            raise ValueError("minimum_sender_fraction must lie above zero and at most one")


@dataclass(frozen=True)
class PreparedAdaptiveUDP:
    """Detached fixed configuration, declared grid and whole-experiment policies."""

    config: ClientConfig
    initial_rates: tuple[int, ...]
    policy: TrialPolicy
    budget: PlanBudget
    adaptive_policy: AdaptiveUDPPolicy


@dataclass(frozen=True)
class AdaptiveBatch:
    """One admitted finite batch with its deterministic selection reason."""

    index: int
    rates: tuple[int, ...]
    reason: str
    depth: int
    plan: PreparedPlan


@dataclass(frozen=True)
class AdaptiveBatchResult:
    """Every admitted batch retains its full execution and preceding cooldown."""

    batch: AdaptiveBatch
    execution: PlanResult
    pause_before_seconds: float = 0.0


@dataclass(frozen=True)
class AdaptiveDecision:
    """The next finite reservation or an explicit terminal admission reason."""

    batch: AdaptiveBatch | None
    reason: str


@dataclass(frozen=True)
class AdaptiveObservation:
    """One retained trial's allocation, endpoint evidence and quality assessment."""

    trial_id: str
    phase: str
    execution_status: str
    requested_rate_bps: int
    native_per_stream_bps: int
    allocated_rate_bps: int
    unused_rate_bps: int
    sender_bytes: int | None
    sender_seconds: float | None
    sender_bps: float | None
    sender_fraction: float | None
    receiver_bytes: int | None
    receiver_seconds: float | None
    receiver_bps: float | None
    receiver_lost_packets: int | None
    receiver_packets: int | None
    count_loss_percent: float | None
    native_loss_percent: float | None
    valid: bool
    acceptable: bool
    reasons: tuple[str, ...]
    setting_checks: tuple[SettingCheck, ...]


@dataclass(frozen=True)
class AdaptiveRateSummary:
    """All observations at one tested rate, including failed confirmations."""

    rate_bps: int
    observations: tuple[AdaptiveObservation, ...]
    status: Literal["eligible", "provisional", "rejected", "inconclusive"]
    valid_trials: int
    confirmation_batches: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AdaptiveResult:
    """Tested points and unknown gaps, never a physical network capacity claim."""

    prepared: PreparedAdaptiveUDP
    batches: tuple[AdaptiveBatchResult, ...]
    decision: AdaptiveDecision
    summaries: tuple[AdaptiveRateSummary, ...]
    outcome: Literal["acceptable_tested_rates", "no_acceptable_tested_rate", "inconclusive"]
    highest_eligible_bps: int | None
    acceptable_rates_bps: tuple[int, ...]
    ceiling_censored: bool

    @property
    def observed_pause_seconds(self) -> float:
        """Include both within-batch pauses and recorded between-batch cooldowns."""
        return sum(
            item.execution.observed_pause_seconds + item.pause_before_seconds
            for item in self.batches
        )


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _finite(value, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and nonnegative")


def _prepare_v1(config, initial_rates, *, policy, budget, adaptive_policy):
    if not isinstance(config, ClientConfig):
        raise ValueError("config must be a ClientConfig")
    config = replace(config, server=str(config.server))
    if config.protocol is not Protocol.UDP or config.bidirectional:
        raise ValueError("adaptive UDP requires UDP in exactly one direction")
    if not config.duration or config.bytes_to_send is not None or config.blocks_to_send is not None:
        raise ValueError("adaptive UDP requires a positive fixed duration")
    if config.rate is not None:
        raise ValueError("adaptive offered rates cannot be combined with config.rate")
    if config.blksize is None:
        raise ValueError("adaptive UDP requires an explicit fixed blksize")
    if not isinstance(policy, TrialPolicy) or not isinstance(budget, PlanBudget):
        raise ValueError("policy and budget must be TrialPolicy and PlanBudget")
    policy, budget = replace(policy), replace(budget)
    if budget.max_active_seconds is None or budget.max_payload_bytes is None:
        raise ValueError("adaptive UDP requires finite active-time and payload budgets")
    if budget.stop_after_elapsed_seconds is not None:
        raise ValueError("adaptive UDP does not support an elapsed admission budget")
    if not isinstance(adaptive_policy, AdaptiveUDPPolicy):
        raise ValueError("adaptive_policy must be an AdaptiveUDPPolicy")
    adaptive_policy = replace(adaptive_policy)
    if adaptive_policy.minimum_valid_trials > policy.repetitions:
        raise ValueError("minimum_valid_trials cannot exceed measured repetitions per batch")
    if not isinstance(initial_rates, Sequence) or isinstance(initial_rates, (str, bytes)):
        raise ValueError("initial_rates must be a finite sequence")
    if not 1 <= len(initial_rates) <= adaptive_policy.max_distinct_rates:
        raise ValueError("initial grid exceeds the distinct-rate budget")
    if len(initial_rates) * (policy.repetitions + policy.warmup_runs) > policy.max_trials:
        raise ValueError("trial_count_limit")
    initial_rates = tuple(initial_rates)
    for rate in initial_rates:
        _integer(rate, "initial rate", 1)
        if not adaptive_policy.min_rate_bps <= rate <= adaptive_policy.max_rate_bps:
            raise ValueError("initial rate lies outside the offered-rate limits")
        resolve_rate(config, RateIntent(aggregate_bps_per_direction=rate))
    if len(set(initial_rates)) != len(initial_rates):
        raise ValueError("initial rates must be distinct")
    if (
        min(initial_rates) != adaptive_policy.min_rate_bps
        or max(initial_rates) != adaptive_policy.max_rate_bps
    ):
        raise ValueError("initial grid must include both offered-rate endpoints")
    prepared = PreparedAdaptiveUDP(config, initial_rates, policy, budget, adaptive_policy)
    _batch_v1(prepared, (), initial_rates, "initial_grid", 0)
    return prepared


def prepare_adaptive_udp(
    config: ClientConfig,
    initial_rates: Sequence[int],
    *,
    policy: TrialPolicy,
    budget: PlanBudget,
    adaptive_policy: AdaptiveUDPPolicy,
) -> PreparedAdaptiveUDP:
    """Admit the entire initial grid and detach fixed settings before any execution."""
    return _prepare_v1(
        config, initial_rates, policy=policy, budget=budget, adaptive_policy=adaptive_policy
    )


def _validated_prepared_v1(prepared):
    if not isinstance(prepared, PreparedAdaptiveUDP):
        raise ValueError("prepared must be a PreparedAdaptiveUDP")
    return _prepare_v1(
        prepared.config,
        prepared.initial_rates,
        policy=prepared.policy,
        budget=prepared.budget,
        adaptive_policy=prepared.adaptive_policy,
    )


def _batch_v1(prepared, history, rates, reason, depth):
    count = (prepared.policy.repetitions + prepared.policy.warmup_runs) * len(rates)
    used_count = sum(len(item.batch.plan.trials) for item in history)
    if used_count + count > prepared.policy.max_trials:
        raise ValueError("trial_count_limit")
    index = len(history)
    specs = tuple(
        TrialSpec(
            f"batch-{index:04d}:rate-{rate}:{phase}:{repetition}",
            f"batch-{index:04d}:rate-{rate}",
            phase,
            repetition,
            prepared.config,
            RateIntent(aggregate_bps_per_direction=rate),
        )
        for rate in rates
        for phase, runs in (
            ("warmup", prepared.policy.warmup_runs),
            ("measured", prepared.policy.repetitions),
        )
        for repetition in range(runs)
    )
    # Reserve all admitted estimates, including unstarted and failed trials.
    used_active = sum(item.batch.plan.estimate.active_seconds for item in history)
    used_bits = sum(item.batch.plan.estimate.estimated_payload_bits for item in history)
    try:
        plan = prepare_plan(specs, policy=prepared.policy, budget=prepared.budget)
    except ValueError as exc:
        if "active-time" in str(exc):
            raise ValueError("active_time_limit") from exc
        if "payload" in str(exc):
            raise ValueError("payload_limit") from exc
        raise
    if used_active + plan.estimate.active_seconds > prepared.budget.max_active_seconds:
        raise ValueError("active_time_limit")
    if (
        used_bits + plan.estimate.estimated_payload_bits + 7
    ) // 8 > prepared.budget.max_payload_bytes:
        raise ValueError("payload_limit")
    return AdaptiveBatch(index, tuple(rates), reason, depth, plan)


def _setting_checks_v1(result, spec):
    config = spec.resolved_config
    expected = {
        name: getattr(config, name)
        for name in (
            "server",
            "port",
            "protocol",
            "duration",
            "parallel",
            "omit",
            "blksize",
            "reverse",
            "bidirectional",
            "rate",
            "tos",
        )
    }
    expected["protocol"] = config.protocol.value
    requested = result.execution.configuration.requested if result.execution else None
    for name, default in _EXPANDED_DEFAULTS_V1.items():
        wanted = getattr(config, name)
        if not _exact_equal(wanted, default) or (
            requested is not None
            and name in requested
            and not _exact_equal(requested[name], default)
        ):
            expected[name] = wanted
    effective = result.execution.configuration.effective if result.execution else {}
    checks = []
    for name, wanted in sorted(expected.items()):
        setting = effective.get(name)
        if name in _EXPANDED_DEFAULTS_V1:
            observed = cast(JSONValue, _expanded_effective_v1(result, name))
            verified = observed is not None
        else:
            verified = (
                setting is not None
                and setting.state == "verified"
                and setting.value is not None
                and has_observed_evidence(result, setting.evidence_paths)
            )
            observed = setting.value if verified else None
        state = (
            "unknown"
            if not verified
            else "native_default"
            if wanted is None
            else "matched"
            if _exact_equal(wanted, observed)
            else "mismatch"
        )
        checks.append(
            SettingCheck(
                name, wanted, observed, state, tuple(setting.evidence_paths) if setting else ()
            )
        )
    return tuple(checks)


def _endpoint_evidence_v1(artifact, direction, observation, reasons):
    matches = [flow for flow in artifact.result.flows if flow.direction == direction]
    stats = getattr(matches[0], observation) if len(matches) == 1 else None
    try:
        selection = _summary_selection_v1(
            artifact, direction, observation, f"/adaptive/{observation}"
        )
    except ReportValidationError:
        # A serializable native summary can still overflow derived float arithmetic
        # or conflict with endpoint provenance. Preserve it as diagnostic evidence.
        reasons.append(f"{observation}_measurement_invalid")
        return stats, None
    if selection is None:
        reasons.append(f"{observation}_measurement_unavailable")
        return stats, None
    return stats, selection[2]


def _observation_v1(prepared, record):
    requested = record.spec.rate_intent.aggregate_bps_per_direction
    resolution = resolve_rate(record.spec.config, record.spec.rate_intent)
    assert resolution.native_per_stream_bps is not None
    assert resolution.aggregate_bps_per_direction is not None
    assert resolution.unused_bps_per_direction is not None
    reasons = []
    if record.spec.phase == "warmup":
        reasons.append("warmup_run")
    if record.status != "completed":
        reasons.append(record.status)
    sent = received = sender_bps = receiver_bps = None
    checks = ()
    if record.artifact is not None:
        result = record.artifact.result
        checks = _setting_checks_v1(result, record.spec)
        if any(check.state not in ("matched", "native_default") for check in checks):
            reasons.append("settings_unverified_or_mismatched")
        method = "reverse" if prepared.config.reverse else "forward"
        if (
            result.protocol != "udp"
            or result.execution is None
            or result.execution.method != method
        ):
            reasons.append("method_or_protocol_mismatch")
        direction = "server_to_client" if prepared.config.reverse else "client_to_server"
        sent, sender_bps = _endpoint_evidence_v1(record.artifact, direction, "sender", reasons)
        received, receiver_bps = _endpoint_evidence_v1(
            record.artifact, direction, "receiver", reasons
        )
    else:
        reasons.append("sender_measurement_unavailable")
        reasons.append("receiver_measurement_unavailable")
    fraction = sender_bps / requested if sender_bps is not None else None
    if fraction is not None and fraction < prepared.adaptive_policy.minimum_sender_fraction:
        reasons.append("sender_underdriven")
    packets = received.packets if received else None
    lost = received.lost_packets if received else None
    valid_counts = (
        type(packets) is int and packets > 0 and type(lost) is int and 0 <= lost <= packets
    )
    loss = 100 * lost / packets if valid_counts else None
    if not valid_counts:
        reasons.append("receiver_packet_counts_unavailable")
    valid = not reasons
    acceptable = valid and loss <= prepared.adaptive_policy.receiver_loss_percent
    if valid and not acceptable:
        reasons.append("receiver_loss_exceeded")
    return AdaptiveObservation(
        record.spec.trial_id,
        record.spec.phase,
        record.status,
        requested,
        resolution.native_per_stream_bps,
        resolution.aggregate_bps_per_direction,
        resolution.unused_bps_per_direction,
        sent.bytes if sent else None,
        sent.duration_seconds if sent else None,
        sender_bps,
        fraction,
        received.bytes if received else None,
        received.duration_seconds if received else None,
        receiver_bps,
        lost,
        packets,
        loss,
        received.lost_percent if received else None,
        valid,
        acceptable,
        tuple(reasons),
        checks,
    )


def _rate_summaries_v1(prepared, history):
    grouped = {}
    confirmations = {}
    for item in history:
        for rate in item.batch.rates:
            if item.batch.reason == "confirm_promising_rate":
                confirmations[rate] = confirmations.get(rate, 0) + 1
        for record in item.execution.trials:
            observation = _observation_v1(prepared, record)
            grouped.setdefault(observation.requested_rate_bps, []).append(observation)
    summaries = []
    for rate, observations in sorted(grouped.items()):
        measured = [value for value in observations if value.phase == "measured"]
        valid = sum(value.valid for value in measured)
        reasons = []
        rejected = any(value.valid and not value.acceptable for value in measured)
        complete = bool(measured) and all(value.valid for value in measured)
        if rejected:
            status = "rejected"
            reasons.append("observed_receiver_loss_exceeded")
        elif not complete or valid < prepared.adaptive_policy.minimum_valid_trials:
            status = "inconclusive"
            reasons.append("insufficient_valid_measurements")
        elif confirmations.get(rate, 0) < prepared.adaptive_policy.confirmation_batches:
            status = "provisional"
            reasons.append("confirmation_required")
        else:
            status = "eligible"
        summaries.append(
            AdaptiveRateSummary(
                rate, tuple(observations), status, valid, confirmations.get(rate, 0), tuple(reasons)
            )
        )
    return tuple(summaries)


def _choose_v1(prepared, history):
    if not history:
        return AdaptiveDecision(
            _batch_v1(prepared, history, prepared.initial_rates, "initial_grid", 0), "initial_grid"
        )
    if any(item.execution.stop_reason is not None for item in history):
        return AdaptiveDecision(None, "execution_stopped")
    summaries = _rate_summaries_v1(prepared, history)
    depths = {
        rate: item.batch.depth
        for item in history
        for rate in item.batch.rates
        if item.batch.reason != "confirm_promising_rate"
    }
    highest_eligible = max(
        (item.rate_bps for item in summaries if item.status == "eligible"), default=0
    )
    promising = [
        item.rate_bps
        for item in summaries
        if item.status == "provisional" and item.rate_bps > highest_eligible
    ]
    candidate = None
    if promising:
        rate = max(promising)
        candidate = ((rate,), "confirm_promising_rate", depths[rate])
    else:

        def observed_status(summary):
            return "acceptable" if summary.status in ("eligible", "provisional") else summary.status

        differing = [
            (left, right)
            for left, right in zip(summaries, summaries[1:], strict=False)
            if observed_status(left) != observed_status(right)
        ]
        integer_intervals = [
            (left, right) for left, right in differing if right.rate_bps - left.rate_bps > 1
        ]
        intervals = [
            (left, right)
            for left, right in integer_intervals
            if max(depths[left.rate_bps], depths[right.rate_bps])
            < prepared.adaptive_policy.max_refinement_depth
        ]
        if not intervals:
            reason = (
                "refinement_depth_limit"
                if integer_intervals
                else "integer_grid_exhausted"
                if differing
                else "no_differing_adjacent_outcomes"
            )
            return AdaptiveDecision(None, reason)
        if len(summaries) >= prepared.adaptive_policy.max_distinct_rates:
            return AdaptiveDecision(None, "distinct_rate_limit")
        left, right = intervals[-1]
        candidate = (
            ((left.rate_bps + right.rate_bps) // 2,),
            "refine_differing_outcomes",
            max(depths[left.rate_bps], depths[right.rate_bps]) + 1,
        )
    try:
        batch = _batch_v1(prepared, history, *candidate)
    except ValueError as exc:
        message = str(exc)
        if message not in ("trial_count_limit", "active_time_limit", "payload_limit"):
            raise
        return AdaptiveDecision(None, message)
    return AdaptiveDecision(batch, batch.reason)


def _history_v1(prepared, history):
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ValueError("history must be a finite sequence")
    retained = []
    for item in history:
        if not isinstance(item, AdaptiveBatchResult):
            raise ValueError("history must contain AdaptiveBatchResult values")
        expected = _choose_v1(prepared, tuple(retained)).batch
        if expected is None or not _exact_equal(asdict(expected), asdict(item.batch)):
            raise ValueError("recorded batch disagrees with the deterministic admission decision")
        execution = plan_result_from_dict(plan_result_to_dict(item.execution))
        if not _exact_equal(asdict(expected.plan), asdict(execution.plan)):
            raise ValueError("execution does not match its admitted adaptive batch")
        started_trials = sum(record.status != "not_run" for record in execution.trials)
        minimum_pause = max(0, started_trials - 1) * prepared.policy.pause_seconds
        if execution.observed_pause_seconds < minimum_pause and not math.isclose(
            execution.observed_pause_seconds, minimum_pause, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError("within-batch cooldown is shorter than the declared pauses")
        _finite(item.pause_before_seconds, "pause_before_seconds")
        if not retained and item.pause_before_seconds != 0:
            raise ValueError("the initial batch cannot have a preceding cooldown")
        if (
            retained
            and item.pause_before_seconds < prepared.policy.pause_seconds
            and not math.isclose(
                item.pause_before_seconds, prepared.policy.pause_seconds, rel_tol=1e-9, abs_tol=1e-9
            )
        ):
            raise ValueError("between-batch cooldown is shorter than the declared pause")
        retained.append(AdaptiveBatchResult(expected, execution, item.pause_before_seconds))
    return tuple(retained)


def _next_batch_v1(prepared, history=()):
    prepared = _validated_prepared_v1(prepared)
    history = _history_v1(prepared, history)
    return _choose_v1(prepared, history)


def next_adaptive_batch(
    prepared: PreparedAdaptiveUDP, history: Sequence[AdaptiveBatchResult] = ()
) -> AdaptiveDecision:
    """Propose one bounded batch solely from retained completed execution histories."""
    return _next_batch_v1(prepared, history)


def _summarize_v1(prepared, history):
    prepared = _validated_prepared_v1(prepared)
    history = _history_v1(prepared, history)
    decision = _choose_v1(prepared, history)
    summaries = _rate_summaries_v1(prepared, history)
    acceptable = tuple(value.rate_bps for value in summaries if value.status == "eligible")
    outcome = (
        "acceptable_tested_rates"
        if acceptable
        else "no_acceptable_tested_rate"
        if summaries and all(value.status == "rejected" for value in summaries)
        else "inconclusive"
    )
    highest = max(acceptable) if acceptable else None
    return AdaptiveResult(
        prepared,
        history,
        decision,
        summaries,
        outcome,
        highest,
        acceptable,
        highest == prepared.adaptive_policy.max_rate_bps,
    )


def summarize_adaptive_udp(
    prepared: PreparedAdaptiveUDP, history: Sequence[AdaptiveBatchResult]
) -> AdaptiveResult:
    """Describe only eligible tested rates; all intervening untested gaps stay unknown."""
    return _summarize_v1(prepared, history)


def _validate_result_v1(result):
    if not isinstance(result, AdaptiveResult):
        raise ValueError("result must be an AdaptiveResult")
    expected = _summarize_v1(result.prepared, result.batches)
    if not _exact_equal(asdict(result), asdict(expected)):
        raise ValueError("adaptive result disagrees with retained v1 evidence and decisions")
    return expected
