"""Bounded concurrent plans with owned workers and explicit resource reservations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .artifacts import artifact_from_result
from .async_trials import _Events, _exception, _execute
from .concurrent_execution import (
    ConcurrentExecutionPolicy,
    ConcurrentPlanCancelledError,
    ConcurrentPlanCleanupError,
    ConcurrentPlanResult,
    ConcurrentPlanTimeoutError,
    ConcurrentTrialRecord,
    prepare_concurrent_plan,
)
from .exceptions import IperfCleanupError
from .result import Result
from .trials import PreparedPlan, _copy_spec, _retain_unencodable_result, _seconds


@dataclass
class _Owned:
    task: asyncio.Future[Result]
    events: _Events
    admission_index: int
    admitted: float
    started_at: float
    cancellation_sent: bool = False


class _Run:
    """Reserve admission atomically and consume every child through cleanup."""

    def __init__(
        self,
        plan: PreparedPlan,
        policy: ConcurrentExecutionPolicy,
        resources: dict[str, tuple[str, ...]],
        keys: tuple[tuple[str, ...], ...],
        targets: tuple[int, ...],
        timeout: float | None,
    ) -> None:
        self.plan, self.policy, self.resources = plan, policy, resources
        self.keys, self.targets, self.timeout = keys, targets, timeout
        self.started_at, self.started = time.time(), time.monotonic()
        self.owner = asyncio.current_task()
        self.cancellations = self.owner.cancelling() if self.owner is not None else 0
        self.records: dict[int, ConcurrentTrialRecord] = {}
        self.active: dict[int, _Owned] = {}
        self.pending = set(range(len(plan.trials)))
        self.admissions = 0
        self.stop_reason = None
        self.stopped_offset = None
        self.termination_reason = None
        self.termination_offset = None
        self.cause: BaseException | None = None
        self.cleanup_errors: list[IperfCleanupError] = []
        previous: dict[str, int] = {}
        self.predecessors: dict[int, int] = {}
        for index, spec in enumerate(plan.trials):
            if spec.cell_id in previous:
                self.predecessors[index] = previous[spec.cell_id]
            previous[spec.cell_id] = index

    def offset(self) -> float:
        return time.monotonic() - self.started

    def stop(self, reason) -> None:
        if self.stop_reason is None:
            self.stop_reason, self.stopped_offset = reason, self.offset()

    def terminate(self, reason, cause: BaseException) -> None:
        offset = self.offset()
        if self.stop_reason is None:
            self.stop_reason, self.stopped_offset = reason, offset
        if self.termination_reason is None:
            self.termination_reason, self.termination_offset = reason, offset
            self.cause = cause

    def remaining(self) -> float | None:
        return None if self.timeout is None else max(0.0, self.timeout - self.offset())

    def parent_cancel_pending(self) -> bool:
        return self.owner is not None and self.owner.cancelling() > self.cancellations

    def check_limits(self) -> None:
        if self.remaining() == 0:
            self.terminate("timeout", TimeoutError("Concurrent plan exceeded its deadline"))
        limit = self.plan.budget.stop_after_elapsed_seconds
        if limit is not None and self.offset() >= limit:
            self.stop("elapsed_admission_limit")

    def wait_timeout(self) -> float | None:
        # A hard stop is already draining owned cleanup: no expired timer may spin.
        if self.termination_reason is not None:
            return None
        remaining = self.remaining()
        limit = self.plan.budget.stop_after_elapsed_seconds
        if self.stop_reason is None and limit is not None:
            admission_remaining = max(0.0, limit - self.offset())
            remaining = (
                admission_remaining if remaining is None else min(remaining, admission_remaining)
            )
        return remaining

    def cancel_active(self) -> None:
        message = (
            self.cause.args[0]
            if isinstance(self.cause, asyncio.CancelledError) and self.cause.args
            else None
        )
        for owned in self.active.values():
            if not owned.task.done() and not owned.cancellation_sent:
                owned.cancellation_sent = True
                owned.task.cancel(message)

    def admit(self, index: int) -> None:
        events = _Events()

        async def execute() -> Result:
            # Admission may precede the first task step. Fence cancellation and
            # deadline expiry here too, before Client can acquire a worker.
            if self.termination_reason is not None or self.parent_cancel_pending():
                raise asyncio.CancelledError()
            remaining = self.remaining()
            if remaining == 0:
                raise TimeoutError("Concurrent plan exceeded its deadline")
            return await _execute(
                _copy_spec(self.plan.trials[index]), timeout=remaining, on_event=events
            )

        self.admissions += 1
        admitted, started_at = self.offset(), time.time()
        operation = execute()
        task: asyncio.Future[Result]
        try:
            task = asyncio.create_task(operation)
        except Exception as error:
            # A task factory can fail after peers were admitted. No worker was
            # acquired for this trial; close its unsubmitted coroutine and use
            # the same outcome path without abandoning any existing owners.
            operation.close()
            task = asyncio.get_running_loop().create_future()
            task.set_exception(error)
        self.active[index] = _Owned(task, events, self.admissions, admitted, started_at)
        self.pending.remove(index)
        if task.done() and not self.parent_cancel_pending():
            self.consume(index, self.active[index])
            del self.active[index]

    def admit_ready(self) -> None:
        reserved = {key for index in self.active for key in self.keys[index]}
        target = sum(self.targets[index] for index in self.active)
        for index in sorted(self.pending):
            if self.parent_cancel_pending():
                # Let the parent receive its original cancellation arguments
                # before an eager child's entry fence supplies an empty cause.
                break
            self.check_limits()
            if self.stop_reason is not None or len(self.active) >= self.policy.max_workers:
                break
            predecessor = self.predecessors.get(index)
            if predecessor is not None and predecessor not in self.records:
                continue
            if reserved.intersection(self.keys[index]):
                continue
            if target + self.targets[index] > self.policy.max_active_target_bps:
                continue
            self.admit(index)
            if index not in self.active:
                # Task factories may execute eagerly or fail before returning.
                # Keep backfilling after consuming that immediate outcome, even
                # when another live peer has not yet completed.
                reserved = {key for active in self.active for key in self.keys[active]}
                target = sum(self.targets[active] for active in self.active)
                continue
            reserved.update(self.keys[index])
            target += self.targets[index]

    def consume(self, index: int, owned: _Owned) -> None:
        artifact = None
        error_record = None
        reason = None
        result = None
        evidence = None
        diagnostics = ()
        cleanup_confirmed = True
        try:
            result = owned.task.result()
            if not isinstance(result, Result):
                raise TypeError("Concurrent plan client must return a Result")
            artifact = artifact_from_result(result)
            metadata = artifact.result.execution
            assert metadata is not None
            status = metadata.status
        except IperfCleanupError as error:
            self.cleanup_errors.append(error)
            prior = error.__cause__
            if isinstance(prior, asyncio.CancelledError):
                self.terminate("cancelled", prior)
            elif isinstance(prior, TimeoutError) and self.timeout is not None:
                self.terminate("timeout", prior)
            self.terminate("cleanup_failed", error)
            status, cleanup_confirmed = "cleanup_failed", False
            reason, error_record = self.termination_reason, _exception(error)
        except asyncio.CancelledError as error:
            self.terminate("cancelled", error)
            status = "timed_out" if self.termination_reason == "timeout" else "cancelled"
            reason, error_record = self.termination_reason, _exception(error)
        except TimeoutError as error:
            if self.timeout is None:
                status, error_record = "exception", _exception(error)
            else:
                self.terminate("timeout", error)
                status = "timed_out" if self.termination_reason == "timeout" else "cancelled"
                reason, error_record = self.termination_reason, _exception(error)
        except Exception as error:
            status, error_record = "exception", _exception(error)
            if isinstance(result, Result):
                evidence, diagnostics = _retain_unencodable_result(result)
        finished = self.offset()
        # Worker crashes, transport failures, and invalid returned results can
        # follow delivered native intervals without producing a final artifact.
        partial = status in ("exception", "cancelled", "timed_out", "cleanup_failed")
        with owned.events.lock:
            events = tuple(owned.events.events) if partial else ()
            observed = owned.events.observed if partial else 0
        self.records[index] = ConcurrentTrialRecord(
            spec=self.plan.trials[index],
            status=status,
            resource_keys=self.keys[index],
            target_bps=self.targets[index],
            admission_index=owned.admission_index,
            admitted_offset_seconds=owned.admitted,
            finished_offset_seconds=finished,
            released_offset_seconds=finished if cleanup_confirmed else None,
            artifact=artifact,
            exception=error_record,
            reason=reason,
            started_at_seconds=owned.started_at,
            completed_at_seconds=time.time(),
            elapsed_seconds=finished - owned.admitted,
            returned_result_evidence=evidence,
            diagnostics=diagnostics,
            cleanup_confirmed=cleanup_confirmed,
            partial_events=events,
            events_observed=observed,
            events_dropped=observed - len(events),
        )
        if status != "completed" and self.plan.policy.stop_on_error:
            self.stop("stop_on_error")

    def consume_done(self) -> None:
        # Retrieve the entire ready batch before refilling. A successful peer
        # must not allow admission ahead of an already-completed failure.
        done = [(index, owned) for index, owned in self.active.items() if owned.task.done()]
        for index, owned in sorted(done, key=lambda item: item[1].admission_index):
            self.consume(index, owned)
            del self.active[index]

    async def run(self) -> ConcurrentPlanResult:
        while self.pending or self.active:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError as error:
                self.terminate("cancelled", error)
            self.check_limits()
            self.consume_done()
            if self.stop_reason is None:
                self.admit_ready()
            if self.termination_reason is not None:
                self.cancel_active()
            if not self.active:
                if self.stop_reason is None and self.pending:
                    continue
                break
            try:
                await asyncio.wait(
                    [owned.task for owned in self.active.values()],
                    timeout=self.wait_timeout(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError as error:
                self.terminate("cancelled", error)
                self.cancel_active()
        for index in self.pending:
            self.records[index] = ConcurrentTrialRecord(
                self.plan.trials[index],
                "not_run",
                self.keys[index],
                self.targets[index],
                reason=self.stop_reason,
            )
        result = ConcurrentPlanResult(
            plan=self.plan,
            policy=self.policy,
            resources=self.resources,
            trials=tuple(self.records[index] for index in range(len(self.plan.trials))),
            started_at_seconds=self.started_at,
            completed_at_seconds=time.time(),
            elapsed_seconds=self.offset(),
            stop_reason=self.stop_reason,
            stopped_offset_seconds=self.stopped_offset,
            termination_reason=self.termination_reason,
            termination_offset_seconds=self.termination_offset,
            timeout_seconds=self.timeout,
        )
        if self.cleanup_errors:
            raise ConcurrentPlanCleanupError(result, tuple(self.cleanup_errors)) from self.cause
        if self.termination_reason == "cancelled":
            args = self.cause.args if self.cause is not None else ()
            raise ConcurrentPlanCancelledError(result, *args) from self.cause
        if self.termination_reason == "timeout":
            raise ConcurrentPlanTimeoutError(result) from self.cause
        from .concurrent_reports import snapshot_concurrent_execution

        return snapshot_concurrent_execution(result)


async def arun_concurrent_plan(
    plan: PreparedPlan,
    *,
    policy: ConcurrentExecutionPolicy,
    resources: Mapping[str, tuple[str, ...]] | None = None,
    timeout: float | None = None,
) -> ConcurrentPlanResult:
    """Run independent cells within explicit invocation-local admission limits.

    Zero pauses, finite positive aggregate targets, and automatic endpoint
    exclusion are required. Optional resources map cell IDs to shared exclusion
    keys. Dependencies within a cell retain declared warm-up/repetition order;
    admission scans in declared order, skipping temporarily blocked cells.
    Reservations cover executor queueing, startup, traffic, and confirmed cleanup.

    stop_on_error and the elapsed admission budget freeze new admissions while
    existing workers finish. Cancellation, deadline, or cleanup failure instead
    cancel every live child once and drain all outcomes. Repeated cancellation
    cannot shorten cleanup. Interrupted exceptions retain detached partial_result;
    cleanup errors retain every original unconfirmed process owner as well.
    Exception outcomes also retain bounded observed events as diagnostic evidence.

    The deadline begins after static validation; cleanup can extend beyond it.
    Rates cap admitted targets, not observed wire traffic or independent callers.
    DNS aliases need a shared resource key. Explicit client_port and nonzero
    pauses are rejected. Reports use schema 3 with overlap/reservation evidence;
    sequential v1/v2 APIs and comparison semantics remain unchanged.
    """
    if timeout is not None:
        _seconds(timeout, "timeout")
        if timeout == 0:
            raise ValueError("timeout must be positive")
    prepared, policy, resources_copy, keys, targets = prepare_concurrent_plan(
        plan, policy, resources
    )
    return await _Run(prepared, policy, resources_copy, keys, targets, timeout).run()
