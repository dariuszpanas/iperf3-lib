"""Finite sequential experiments with preserved failures and explicit admission budgets."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .artifacts import ResultArtifact, artifact_from_result
from .config import ClientConfig
from .exceptions import UnsupportedFeatureError
from .intent import PlanEstimate, RateIntent, estimate_plan, resolve_rate
from .result import JSONValue, Result


def _count(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _seconds(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")


@dataclass(frozen=True)
class TrialPolicy:
    """Measured repetitions, separate warm-up runs, and pauses between native calls."""

    repetitions: int = 3
    warmup_runs: int = 0
    pause_seconds: float = 0
    max_trials: int = 1000
    stop_on_error: bool = False

    def __post_init__(self) -> None:
        """Reject unbounded counts and ambiguous execution policies."""
        _count(self.repetitions, "repetitions", 1)
        _count(self.warmup_runs, "warmup_runs")
        _count(self.max_trials, "max_trials", 1)
        _seconds(self.pause_seconds, "pause_seconds")
        if type(self.stop_on_error) is not bool:
            raise ValueError("stop_on_error must be boolean")
        if self.repetitions + self.warmup_runs > self.max_trials:
            raise ValueError("repetitions and warm-up exceed max_trials")


@dataclass(frozen=True)
class PlanBudget:
    """Admission estimates and an optional elapsed stop checked between blocking calls.

    None explicitly leaves the corresponding estimate uncapped. Payload excludes
    network overhead. Neither estimates nor elapsed admission stop bound a
    native call, cancel traffic, or promise an actual wire-byte ceiling.
    """

    max_active_seconds: int | None
    max_payload_bytes: int | None
    stop_after_elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        """Validate separately named estimate and elapsed admission limits."""
        for name in ("max_active_seconds", "max_payload_bytes"):
            value = getattr(self, name)
            if value is not None:
                _count(value, name)
        if self.stop_after_elapsed_seconds is not None:
            _seconds(self.stop_after_elapsed_seconds, "stop_after_elapsed_seconds")


@dataclass(frozen=True)
class TrialSpec:
    """One declared run; config and rate intent are detached again at admission."""

    trial_id: str
    cell_id: str
    phase: Literal["warmup", "measured"]
    repetition: int
    config: ClientConfig
    rate_intent: RateIntent | None = None

    @property
    def resolved_config(self) -> ClientConfig:
        """Return the detached native-level configuration admitted for this trial."""
        resolution = resolve_rate(self.config, self.rate_intent)
        return replace(self.config, rate=resolution.native_per_stream_bps)


@dataclass(frozen=True)
class PreparedPlan:
    """Finite resolved order, requested settings, estimates, and execution policy."""

    trials: tuple[TrialSpec, ...]
    policy: TrialPolicy
    budget: PlanBudget
    estimate: PlanEstimate
    planned_pause_seconds: float
    order_seed: int | None = None


@dataclass(frozen=True)
class TrialException:
    """A wrapper/executor exception without a fabricated native result."""

    type_name: str
    message: str


@dataclass(frozen=True)
class TrialRecord:
    """Every planned trial retains its actual outcome or explicit non-execution reason."""

    spec: TrialSpec
    status: Literal["completed", "failed", "incomplete", "exception", "not_run"]
    artifact: ResultArtifact | None = None
    exception: TrialException | None = None
    reason: str | None = None
    started_at_seconds: float | None = None
    completed_at_seconds: float | None = None
    elapsed_seconds: float | None = None
    returned_result_evidence: dict[str, JSONValue] | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanResult:
    """Sequential execution history; warm-up, failed, and unstarted trials are retained."""

    plan: PreparedPlan
    trials: tuple[TrialRecord, ...]
    started_at_seconds: float
    completed_at_seconds: float
    elapsed_seconds: float
    observed_pause_seconds: float
    stop_reason: str | None = None

    @property
    def execution_success(self) -> bool:
        """Require every planned run, including warm-up, to complete successfully."""
        return bool(self.trials) and all(trial.status == "completed" for trial in self.trials)


def _copy_spec(spec: TrialSpec) -> TrialSpec:
    if not isinstance(spec, TrialSpec):
        raise ValueError("trials must contain TrialSpec values")
    for name in ("trial_id", "cell_id"):
        value = getattr(spec, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
    if spec.phase not in ("warmup", "measured"):
        raise ValueError("trial phase must be warmup or measured")
    _count(spec.repetition, "repetition")
    if not isinstance(spec.config, ClientConfig):
        raise ValueError("trial config must be a ClientConfig")
    config = replace(spec.config)
    if config.mptcp or config.json_stream:
        raise UnsupportedFeatureError("trial plans cannot use unsupported mptcp or json_stream")
    if spec.rate_intent is not None and not isinstance(spec.rate_intent, RateIntent):
        raise ValueError("trial rate_intent must be a RateIntent or None")
    intent = replace(spec.rate_intent) if spec.rate_intent is not None else None
    resolve_rate(config, intent)
    return replace(spec, config=config, rate_intent=intent)


def prepare_plan(
    trials: Sequence[TrialSpec],
    *,
    policy: TrialPolicy,
    budget: PlanBudget,
    order_seed: int | None = None,
) -> PreparedPlan:
    """Validate and detach the entire finite plan before any traffic is generated.

    Native host/kernel support is established by execution, not these static
    checks. The stored estimate includes native omit intervals and warm-up runs;
    pauses are stored separately because they do not generate intended traffic.
    """
    if not isinstance(policy, TrialPolicy) or not isinstance(budget, PlanBudget):
        raise ValueError("policy and budget must be TrialPolicy and PlanBudget")
    policy, budget = replace(policy), replace(budget)
    if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)):
        raise ValueError("trials must be a finite sequence")
    if not 1 <= len(trials) <= policy.max_trials:
        raise ValueError("trial count must be positive and no greater than max_trials")
    if order_seed is not None:
        _count(order_seed, "order_seed")
    admitted = tuple(_copy_spec(spec) for spec in trials)
    if len({spec.trial_id for spec in admitted}) != len(admitted):
        raise ValueError("trial IDs must be unique")
    cells: dict[str, list[tuple[str, int]]] = {}
    for spec in admitted:
        cells.setdefault(spec.cell_id, []).append((spec.phase, spec.repetition))
    expected = [
        (phase, index)
        for phase, count in (("warmup", policy.warmup_runs), ("measured", policy.repetitions))
        for index in range(count)
    ]
    if any(sequence != expected for sequence in cells.values()):
        raise ValueError(
            "each cell must retain the declared warm-up and measured counts in phase/repetition order"
        )
    estimate = estimate_plan(
        [spec.config for spec in admitted],
        [spec.rate_intent for spec in admitted],
        max_active_seconds=budget.max_active_seconds,
        max_payload_bytes=budget.max_payload_bytes,
    )
    pauses = policy.pause_seconds * (len(admitted) - 1)
    _seconds(pauses, "planned_pause_seconds")
    return PreparedPlan(admitted, policy, budget, estimate, pauses, order_seed)


_DEFAULT_POLICY = TrialPolicy()


def prepare_trials(
    config: ClientConfig,
    *,
    budget: PlanBudget,
    policy: TrialPolicy = _DEFAULT_POLICY,
    rate_intent: RateIntent | None = None,
    cell_id: str = "default",
) -> PreparedPlan:
    """Expand warm-up followed by measured repetitions of one configuration."""
    if not isinstance(policy, TrialPolicy):
        raise ValueError("policy must be a TrialPolicy")
    policy = replace(policy)
    specs = [
        TrialSpec(f"{cell_id}:{phase}:{index}", cell_id, phase, index, config, rate_intent)
        for phase, count in (("warmup", policy.warmup_runs), ("measured", policy.repetitions))
        for index in range(count)
    ]
    return prepare_plan(specs, policy=policy, budget=budget)


def _native_execute(spec: TrialSpec) -> Result:
    # Resolve the current module rather than retaining a reloaded native binding.
    from .iperf_client import Client

    return Client(spec.config, rate_intent=spec.rate_intent).run()


def _json_evidence(value, active: frozenset[int] = frozenset()) -> JSONValue:
    """Copy strictly JSON-safe partial evidence without coercing invalid values."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, dict)):
        if id(value) in active:
            raise ValueError("cyclic evidence")
        active = active | {id(value)}
        if isinstance(value, list):
            return [_json_evidence(item, active) for item in value]
        if all(isinstance(key, str) for key in value):
            return {key: _json_evidence(item, active) for key, item in value.items()}
    raise ValueError("evidence is not strict JSON")


def _retain_unencodable_result(result: Result):
    diagnostics = [
        "Returned result could not be encoded as a canonical artifact; JSON-safe fields are retained separately."
    ]
    try:
        candidate = result.to_dict()
    except Exception as exc:
        candidate = {"raw": result.raw, "ok": result.ok, "error": result.error}
        diagnostics.append(f"Result snapshot failed: {type(exc).__name__}: {exc}")
    evidence = {}
    for key, value in candidate.items():
        try:
            evidence[key] = _json_evidence(value)
        except Exception:
            diagnostics.append(
                f"Returned result field {key} is not JSON-safe and was not retained."
            )
    return evidence, tuple(diagnostics)


def run_plan(
    plan: PreparedPlan,
    *,
    executor: Callable[[TrialSpec], Result] | None = None,
) -> PlanResult:
    """Run sequentially, preserving errors without retrying or cancelling native calls.

    An optional executor enables an application-owned transport or deterministic
    experiment. It receives a detached spec and must return a canonical Result.
    Exceptions derived from Exception are recorded; process-control exceptions
    such as KeyboardInterrupt propagate. This function adds no concurrency guard
    across other callers: the existing non-reentrant native contract still applies.
    """
    if not isinstance(plan, PreparedPlan):
        raise ValueError("plan must be a PreparedPlan")
    if executor is not None and not callable(executor):
        raise ValueError("executor must be callable")
    # Revalidate mutable nested configs, recompute estimates, and detach all runs
    # before invoking caller code or allocating native objects.
    plan = prepare_plan(
        plan.trials, policy=plan.policy, budget=plan.budget, order_seed=plan.order_seed
    )
    execute = executor if executor is not None else _native_execute
    started_at, started = time.time(), time.monotonic()
    pause_elapsed = 0.0
    records: list[TrialRecord] = []
    stop_reason = None
    limit = plan.budget.stop_after_elapsed_seconds
    for index, spec in enumerate(plan.trials):
        if stop_reason is None and limit is not None and time.monotonic() - started >= limit:
            stop_reason = "elapsed_admission_limit"
        if stop_reason is None and index and plan.policy.pause_seconds:
            pause_started = time.monotonic()
            pause = plan.policy.pause_seconds
            if limit is not None:
                pause = min(pause, max(0.0, limit - (pause_started - started)))
            time.sleep(pause)
            pause_elapsed += time.monotonic() - pause_started
            if limit is not None and time.monotonic() - started >= limit:
                stop_reason = "elapsed_admission_limit"
        if stop_reason is not None:
            records.append(TrialRecord(spec, "not_run", reason=stop_reason))
            continue
        trial_start_at, trial_start = time.time(), time.monotonic()
        artifact = None
        error = None
        reason = None
        result = None
        evidence = None
        diagnostics = ()
        try:
            result = execute(_copy_spec(spec))
            if not isinstance(result, Result):
                raise TypeError("executor must return a Result")
            artifact = artifact_from_result(result)
            metadata = artifact.result.execution
            status: Literal["completed", "failed", "incomplete", "exception", "not_run"] = (
                metadata.status if metadata is not None else "incomplete"
            )
            if metadata is None:
                reason = "execution_metadata_unavailable"
        except Exception as exc:
            status = "exception"
            error = TrialException(f"{type(exc).__module__}.{type(exc).__qualname__}", str(exc))
            if isinstance(result, Result):
                evidence, diagnostics = _retain_unencodable_result(result)
        records.append(
            TrialRecord(
                spec,
                status,
                artifact,
                error,
                reason,
                trial_start_at,
                time.time(),
                time.monotonic() - trial_start,
                evidence,
                diagnostics,
            )
        )
        if status != "completed" and plan.policy.stop_on_error:
            stop_reason = "stop_on_error"
    return PlanResult(
        plan,
        tuple(records),
        started_at,
        time.time(),
        time.monotonic() - started,
        pause_elapsed,
        stop_reason,
    )
