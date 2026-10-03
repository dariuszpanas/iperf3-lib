"""Sequential, process-owned async plans with retained evidence after interruption."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import Callable

from ._ipc import IPCError, encode_frame
from .artifacts import artifact_from_result
from .events import NativeEvent
from .exceptions import IperfCleanupError
from .plan_execution import (
    PlanCancelledError,
    PlanCleanupError,
    PlanExecutionResult,
    PlanTimeoutError,
    PlanTrialRecord,
)
from .result import Result
from .trials import (
    PreparedPlan,
    TrialException,
    TrialSpec,
    _copy_spec,
    _retain_unencodable_result,
    _seconds,
    prepare_plan,
)


async def _execute(
    spec: TrialSpec, *, timeout: float | None, on_event: Callable[[NativeEvent], None]
) -> Result:
    from .iperf_client import Client

    return await Client(spec.config, rate_intent=spec.rate_intent).arun(
        timeout=timeout, on_event=on_event
    )


class _Events:
    """Retain a detached, bounded prefix from callbacks on the execution thread."""

    def __init__(self) -> None:
        self.events: list[NativeEvent] = []
        self.observed = 0
        self.bytes = 0
        self.closed = False
        self.lock = threading.Lock()

    def __call__(self, event: NativeEvent) -> None:
        with self.lock:
            self.observed += 1
            if self.closed or len(self.events) >= 64:
                self.closed = True
                return
            try:
                if (
                    not isinstance(event.kind, str)
                    or not event.kind.strip()
                    or type(event.sequence) is not int
                    or event.sequence <= 0
                    or (self.events and event.sequence <= self.events[-1].sequence)
                    or isinstance(event.received_at_seconds, bool)
                    or not math.isfinite(event.received_at_seconds)
                ):
                    raise ValueError("Invalid partial event metadata")
                frame = encode_frame(
                    {
                        "kind": event.kind,
                        "data": event.data,
                        "sequence": event.sequence,
                        "received_at_seconds": event.received_at_seconds,
                    },
                    max_bytes=65536,
                )
                if len(frame) > 65536 or self.bytes + len(frame) > 1024 * 1024:
                    raise ValueError("Partial event retention limit reached")
                self.events.append(NativeEvent(**json.loads(frame[4:])))
                self.bytes += len(frame)
            except (IPCError, ValueError, TypeError, OverflowError):
                self.closed = True


def _exception(error: BaseException) -> TrialException:
    return TrialException(f"{type(error).__module__}.{type(error).__qualname__}", str(error))


class _Run:
    """Keep one stop decision and one owned child throughout cancellation cleanup."""

    def __init__(self, plan: PreparedPlan, timeout: float | None) -> None:
        self.plan = plan
        self.timeout = timeout
        self.started_at, self.started = time.time(), time.monotonic()
        self.deadline = self.started + timeout if timeout is not None else None
        self.records: list[PlanTrialRecord] = []
        self.pause_elapsed = 0.0
        self.stop_reason = None
        self.cause: BaseException | None = None
        self.cleanup_errors: list[IperfCleanupError] = []

    def stop(self, reason, cause: BaseException | None = None) -> None:
        if self.stop_reason is None:
            self.stop_reason, self.cause = reason, cause

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def check_limits(self) -> None:
        if self.remaining() == 0:
            self.stop("timeout", TimeoutError("Plan execution exceeded its deadline"))
        limit = self.plan.budget.stop_after_elapsed_seconds
        if limit is not None and time.monotonic() - self.started >= limit:
            self.stop("elapsed_admission_limit")

    async def before_trial(self, index: int) -> None:
        # Observe cancellation queued by the caller before creating another child.
        await asyncio.sleep(0)
        self.check_limits()
        if self.stop_reason is not None or not index or not self.plan.policy.pause_seconds:
            return
        pause_started = time.monotonic()
        pause = self.plan.policy.pause_seconds
        remaining = self.remaining()
        if remaining is not None:
            pause = min(pause, remaining)
        limit = self.plan.budget.stop_after_elapsed_seconds
        if limit is not None:
            pause = min(pause, max(0.0, limit - (pause_started - self.started)))
        try:
            await asyncio.sleep(pause)
        finally:
            self.pause_elapsed += time.monotonic() - pause_started
        self.check_limits()

    async def drain(self, child: asyncio.Task[Result]) -> None:
        # Cancel once: further cancellation must never reach the child's cleanup.
        if not child.done():
            message = (
                self.cause.args[0]
                if isinstance(self.cause, asyncio.CancelledError) and self.cause.args
                else None
            )
            child.cancel(message)
        while not child.done():
            try:
                await asyncio.wait((child,))
            except asyncio.CancelledError as error:
                self.stop("cancelled", error)

    async def trial(self, spec: TrialSpec) -> None:
        events = _Events()
        started_at, started = time.time(), time.monotonic()
        owner = asyncio.current_task()
        cancellations = owner.cancelling() if owner is not None else 0

        async def execute() -> Result:
            # A queued parent cancellation can precede this child's first step.
            if owner is not None and owner.cancelling() > cancellations:
                raise asyncio.CancelledError()
            remaining = self.remaining()
            if remaining == 0:
                raise TimeoutError("Plan execution exceeded its deadline")
            return await _execute(_copy_spec(spec), timeout=remaining, on_event=events)

        child = asyncio.create_task(execute())
        try:
            done, _ = await asyncio.wait((child,), timeout=self.remaining())
            if not done or self.remaining() == 0:
                self.stop("timeout", TimeoutError("Plan execution exceeded its deadline"))
            if not done:
                await self.drain(child)
        except asyncio.CancelledError as error:
            self.stop("cancelled", error)
            await self.drain(child)

        artifact = None
        error_record = None
        reason = None
        result = None
        evidence = None
        diagnostics = ()
        cleanup_confirmed = True
        try:
            # Always retrieve the owned task, including failures after parent cancellation.
            result = child.result()
            if not isinstance(result, Result):
                raise TypeError("Plan client must return a Result")
            artifact = artifact_from_result(result)
            metadata = artifact.result.execution
            # Canonical artifact conversion supplies explicit compatibility
            # metadata even for legacy Results that have no execution model.
            assert metadata is not None
            status = metadata.status
        except IperfCleanupError as error:
            self.cleanup_errors.append(error)
            prior = error.__cause__
            if isinstance(prior, asyncio.CancelledError):
                self.stop("cancelled", prior)
            elif isinstance(prior, TimeoutError) and self.timeout is not None:
                self.stop("timeout", prior)
            self.stop("cleanup_failed", error)
            status, cleanup_confirmed = "cleanup_failed", False
            reason, error_record = self.stop_reason, _exception(error)
        except asyncio.CancelledError as error:
            self.stop("cancelled", error)
            status = "timed_out" if self.stop_reason == "timeout" else "cancelled"
            reason, error_record = self.stop_reason, _exception(error)
        except TimeoutError as error:
            if self.timeout is None:
                status, error_record = "exception", _exception(error)
            else:
                self.stop("timeout", error)
                status = "cancelled" if self.stop_reason == "cancelled" else "timed_out"
                reason, error_record = self.stop_reason, _exception(error)
        except Exception as error:
            status = "exception"
            error_record = _exception(error)
            if isinstance(result, Result):
                evidence, diagnostics = _retain_unencodable_result(result)
        partial = status in ("cancelled", "timed_out", "cleanup_failed")
        self.records.append(
            PlanTrialRecord(
                spec=spec,
                status=status,
                artifact=artifact,
                exception=error_record,
                reason=reason,
                started_at_seconds=started_at,
                completed_at_seconds=time.time(),
                elapsed_seconds=time.monotonic() - started,
                returned_result_evidence=evidence,
                diagnostics=diagnostics,
                cleanup_confirmed=cleanup_confirmed,
                partial_events=tuple(events.events) if partial else (),
                events_observed=events.observed if partial else 0,
                events_dropped=events.observed - len(events.events) if partial else 0,
            )
        )
        if status != "completed" and self.plan.policy.stop_on_error:
            self.stop("stop_on_error")

    async def run(self) -> PlanExecutionResult:
        for index, spec in enumerate(self.plan.trials):
            if self.stop_reason is None:
                try:
                    await self.before_trial(index)
                except asyncio.CancelledError as error:
                    self.stop("cancelled", error)
            if self.stop_reason is not None:
                self.records.append(PlanTrialRecord(spec, "not_run", reason=self.stop_reason))
            else:
                await self.trial(spec)
        result = PlanExecutionResult(
            self.plan,
            tuple(self.records),
            self.started_at,
            time.time(),
            time.monotonic() - self.started,
            self.pause_elapsed,
            self.stop_reason,
            self.timeout,
        )
        if self.cleanup_errors:
            raise PlanCleanupError(result, tuple(self.cleanup_errors)) from self.cause
        if self.stop_reason == "cancelled":
            args = self.cause.args if self.cause is not None else ()
            raise PlanCancelledError(result, *args) from self.cause
        if self.stop_reason == "timeout":
            raise PlanTimeoutError(result) from self.cause
        from .plan_reports import snapshot_plan_execution

        return snapshot_plan_execution(result)


async def arun_plan(plan: PreparedPlan, *, timeout: float | None = None) -> PlanExecutionResult:
    """Run an owned sequential plan and retain its complete declared history.

    Cancellation and the optional positive deadline stop admission, cancel the
    active isolated client, and wait for cleanup. Interruption raises
    PlanCancelledError or PlanTimeoutError with a detached partial_result;
    unconfirmed cleanup instead raises PlanCleanupError retaining process owners.
    Repeated task cancellation cannot shorten cleanup. The deadline starts after
    static plan validation and includes executor queuing, startup, runs and pauses;
    cleanup can extend beyond it. The existing elapsed budget only stops admission.

    Internal event collection enables native JSON streaming and retains at most
    64 events / 1 MiB for an interrupted trial (64 KiB per event, framed bytes).
    Counts describe delivered callbacks and retention drops, not native event loss.
    Completed trials keep canonical artifacts; partial events are not results or
    performance samples. This function owns only this invocation's workers and
    does not coordinate independent callers or execute trials concurrently.
    """
    if not isinstance(plan, PreparedPlan):
        raise ValueError("plan must be a PreparedPlan")
    if timeout is not None:
        _seconds(timeout, "timeout")
        if timeout == 0:
            raise ValueError("timeout must be positive")
    plan = prepare_plan(
        plan.trials, policy=plan.policy, budget=plan.budget, order_seed=plan.order_seed
    )
    return await _Run(plan, timeout).run()
