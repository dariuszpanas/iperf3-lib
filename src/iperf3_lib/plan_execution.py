"""Owned sequential plan outcomes, kept separate from the frozen v1 trial models."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from .artifacts import ArtifactProducer, ResultArtifact
from .events import NativeEvent
from .exceptions import IperfCleanupError, IperfLibraryError
from .result import JSONValue
from .trials import PreparedPlan, TrialException, TrialSpec


@dataclass(frozen=True)
class PlanTrialRecord:
    """Declared trial with bounded event diagnostics for exceptions and interruption."""

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
class PlanExecutionResult:
    """A complete declared history, even when owned execution was interrupted."""

    plan: PreparedPlan
    trials: tuple[PlanTrialRecord, ...]
    started_at_seconds: float
    completed_at_seconds: float
    elapsed_seconds: float
    observed_pause_seconds: float
    stop_reason: (
        Literal[
            "stop_on_error", "elapsed_admission_limit", "cancelled", "timeout", "cleanup_failed"
        ]
        | None
    ) = None
    timeout_seconds: float | None = None

    @property
    def cleanup_confirmed(self) -> bool:
        """Whether every admitted worker was cleaned up or never acquired."""
        return all(record.cleanup_confirmed for record in self.trials)

    @property
    def execution_success(self) -> bool:
        """Require all declared trials, no stop decision, and confirmed cleanup."""
        return (
            self.stop_reason is None
            and bool(self.trials)
            and self.cleanup_confirmed
            and all(record.status == "completed" for record in self.trials)
        )


@dataclass(frozen=True)
class PlanExecutionReport:
    """Standalone v2 evidence; pause timing remains globally sequential."""

    execution: PlanExecutionResult
    producer: ArtifactProducer
    kind: Literal["iperf3-lib.plan-execution"] = "iperf3-lib.plan-execution"
    schema_version: int = 2
    execution_mode: Literal["sequential-owned"] = "sequential-owned"
    pause_semantics: Literal["global_completion_to_start"] = "global_completion_to_start"
    extensions: dict[str, JSONValue] = field(default_factory=dict)


class PlanCancelledError(asyncio.CancelledError):
    """Task cancellation after cleanup, with a detached partial execution snapshot."""

    def __init__(self, partial_result: PlanExecutionResult, *args: object) -> None:
        """Preserve the original cancellation arguments without retaining mutable evidence."""
        from .plan_reports import snapshot_plan_execution

        super().__init__(*args)
        self.partial_result = snapshot_plan_execution(partial_result)


class PlanTimeoutError(TimeoutError):
    """A plan deadline expired; completed and interrupted trials remain available."""

    def __init__(
        self,
        partial_result: PlanExecutionResult,
        message: str = "Plan execution exceeded its deadline",
    ) -> None:
        """Detach evidence before exposing the deadline failure to the caller."""
        from .plan_reports import snapshot_plan_execution

        super().__init__(message)
        self.partial_result = snapshot_plan_execution(partial_result)


class PlanCleanupError(IperfLibraryError):
    """Cleanup is unconfirmed; every original error retains its worker owner."""

    def __init__(
        self,
        partial_result: PlanExecutionResult,
        cleanup_errors: tuple[IperfCleanupError, ...],
    ) -> None:
        """Retain all live cleanup owners separately from detached report evidence."""
        from .plan_reports import snapshot_plan_execution

        if not cleanup_errors or any(
            not isinstance(error, IperfCleanupError) for error in cleanup_errors
        ):
            raise ValueError("cleanup_errors must retain at least one IperfCleanupError")
        self.cleanup_errors = tuple(cleanup_errors)
        super().__init__(
            "Plan worker cleanup could not be confirmed; process ownership is retained"
        )
        self.partial_result = snapshot_plan_execution(partial_result)
