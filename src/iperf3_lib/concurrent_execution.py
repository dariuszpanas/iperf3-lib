"""Owned concurrent plan outcomes and explicit admission reservations."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from .artifacts import ArtifactProducer, ResultArtifact
from .events import NativeEvent
from .exceptions import IperfCleanupError, IperfLibraryError
from .intent import resolve_rate
from .result import JSONValue
from .trials import PreparedPlan, TrialException, TrialSpec, prepare_plan


@dataclass(frozen=True)
class ConcurrentExecutionPolicy:
    """Required worker and configured aggregate target-rate admission ceilings."""

    max_workers: int
    max_active_target_bps: int

    def __post_init__(self) -> None:
        """Reject implicit, unlimited, boolean, and nonintegral reservations."""
        for name in ("max_workers", "max_active_target_bps"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def prepare_concurrent_plan(
    plan: PreparedPlan,
    policy: ConcurrentExecutionPolicy,
    resources: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[
    PreparedPlan,
    ConcurrentExecutionPolicy,
    dict[str, tuple[str, ...]],
    tuple[tuple[str, ...], ...],
    tuple[int, ...],
]:
    """Detach admission inputs and derive finite rates and resource keys per trial.

    Endpoint keys canonicalize spelling without resolving DNS. Callers must
    declare a shared user resource for aliases or other shared infrastructure.
    Fixed source ports and nonzero pauses are unsupported by this execution mode.
    """
    if not isinstance(plan, PreparedPlan) or not isinstance(policy, ConcurrentExecutionPolicy):
        raise ValueError("plan and policy must be PreparedPlan and ConcurrentExecutionPolicy")
    detached = prepare_plan(
        plan.trials, policy=plan.policy, budget=plan.budget, order_seed=plan.order_seed
    )
    if detached != plan:
        raise ValueError("plan estimates or settings disagree with the finite plan")
    policy = replace(policy)
    if detached.policy.pause_seconds != 0:
        raise ValueError("concurrent execution requires zero pause_seconds")
    if resources is not None and not isinstance(resources, Mapping):
        raise ValueError("resources must map cell IDs to tuples of resource keys")
    cells = {spec.cell_id for spec in detached.trials}
    extras: dict[str, tuple[str, ...]] = {}
    for cell, values in (resources or {}).items():
        if type(cell) is not str or cell not in cells:
            raise ValueError("resources contains an unknown cell ID")
        if type(values) is not tuple or any(
            type(key) is not str or not key.strip() for key in values
        ):
            raise ValueError("resource keys must be tuples of nonempty strings")
        if len(set(values)) != len(values):
            raise ValueError("resource keys must be unique within a cell")
        extras[cell] = tuple(values)
    keys, targets = [], []
    for spec in detached.trials:
        if spec.config.client_port is not None:
            raise ValueError("concurrent execution does not support fixed client_port")
        host = str(spec.config.server)
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            host = host.lower().rstrip(".")
        endpoint = "endpoint:" + json.dumps([host, spec.config.port], separators=(",", ":"))
        keys.append(
            tuple(sorted({endpoint, *("user:" + key for key in extras.get(spec.cell_id, ()))}))
        )
        target = resolve_rate(spec.config, spec.rate_intent).aggregate_bps_all_directions
        if type(target) is not int or target <= 0:
            raise ValueError("concurrent execution requires a finite positive target rate")
        if target > policy.max_active_target_bps:
            raise ValueError("trial target rate exceeds max_active_target_bps")
        targets.append(target)
    return detached, policy, extras, tuple(keys), tuple(targets)


@dataclass(frozen=True)
class ConcurrentTrialRecord:
    """Declared outcome, reservation interval, and bounded events without a final artifact."""

    spec: TrialSpec
    status: Literal[
        "completed",
        "failed",
        "incomplete",
        "exception",
        "not_run",
        "cancelled",
        "timed_out",
        "cleanup_failed",
    ]
    resource_keys: tuple[str, ...]
    target_bps: int
    admission_index: int | None = None
    admitted_offset_seconds: float | None = None
    finished_offset_seconds: float | None = None
    released_offset_seconds: float | None = None
    artifact: ResultArtifact | None = None
    exception: TrialException | None = None
    reason: str | None = None
    started_at_seconds: float | None = None
    completed_at_seconds: float | None = None
    elapsed_seconds: float | None = None
    returned_result_evidence: dict[str, JSONValue] | None = None
    diagnostics: tuple[str, ...] = ()
    cleanup_confirmed: bool = True
    partial_events: tuple[NativeEvent, ...] = ()
    events_observed: int = 0
    events_dropped: int = 0


@dataclass(frozen=True)
class ConcurrentPlanResult:
    """Declared history, reservation evidence, and separate soft and hard stops."""

    plan: PreparedPlan
    policy: ConcurrentExecutionPolicy
    resources: dict[str, tuple[str, ...]]
    trials: tuple[ConcurrentTrialRecord, ...]
    started_at_seconds: float
    completed_at_seconds: float
    elapsed_seconds: float
    stop_reason: (
        Literal[
            "stop_on_error", "elapsed_admission_limit", "cancelled", "timeout", "cleanup_failed"
        ]
        | None
    ) = None
    stopped_offset_seconds: float | None = None
    termination_reason: Literal["cancelled", "timeout", "cleanup_failed"] | None = None
    termination_offset_seconds: float | None = None
    timeout_seconds: float | None = None

    @property
    def cleanup_confirmed(self) -> bool:
        """Whether every admitted worker has confirmed cleanup."""
        return all(record.cleanup_confirmed for record in self.trials)

    @property
    def execution_success(self) -> bool:
        """Require completion of every declared trial without any stop decision."""
        return (
            self.stop_reason is None
            and self.termination_reason is None
            and bool(self.trials)
            and self.cleanup_confirmed
            and all(record.status == "completed" for record in self.trials)
        )


@dataclass(frozen=True)
class ConcurrentPlanReport:
    """Standalone v3 execution evidence with explicit zero-pause concurrency."""

    execution: ConcurrentPlanResult
    producer: ArtifactProducer
    kind: Literal["iperf3-lib.plan-execution"] = "iperf3-lib.plan-execution"
    schema_version: int = 3
    execution_mode: Literal["bounded-concurrent-owned"] = "bounded-concurrent-owned"
    pause_semantics: Literal["zero-only"] = "zero-only"
    extensions: dict[str, JSONValue] = field(default_factory=dict)


class ConcurrentPlanCancelledError(asyncio.CancelledError):
    """Cancellation retaining a detached history after all owned cleanup attempts."""

    def __init__(self, partial_result: ConcurrentPlanResult, *args: object) -> None:
        """Preserve cancellation arguments independently from report evidence."""
        from .concurrent_reports import snapshot_concurrent_execution

        super().__init__(*args)
        self.partial_result = snapshot_concurrent_execution(partial_result)


class ConcurrentPlanTimeoutError(TimeoutError):
    """A concurrent deadline with detached completed and interrupted outcomes."""

    def __init__(
        self,
        partial_result: ConcurrentPlanResult,
        message: str = "Concurrent plan execution exceeded its deadline",
    ) -> None:
        """Retain partial evidence when the deadline terminates owned workers."""
        from .concurrent_reports import snapshot_concurrent_execution

        super().__init__(message)
        self.partial_result = snapshot_concurrent_execution(partial_result)


class ConcurrentPlanCleanupError(IperfLibraryError):
    """Unconfirmed cleanup retaining every original live worker owner."""

    def __init__(
        self, partial_result: ConcurrentPlanResult, cleanup_errors: tuple[IperfCleanupError, ...]
    ) -> None:
        """Keep live owners separate from the detached portable execution report."""
        from .concurrent_reports import snapshot_concurrent_execution

        if not cleanup_errors or any(
            not isinstance(error, IperfCleanupError) for error in cleanup_errors
        ):
            raise ValueError("cleanup_errors must retain at least one IperfCleanupError")
        self.cleanup_errors = tuple(cleanup_errors)
        super().__init__(
            "Concurrent plan worker cleanup could not be confirmed; process ownership is retained"
        )
        self.partial_result = snapshot_concurrent_execution(partial_result)
