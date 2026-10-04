"""Owned sequential async plans retain evidence through interruption and cleanup."""

import asyncio
import concurrent.futures
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from iperf3_lib.config import ClientConfig
from iperf3_lib.exceptions import IperfCleanupError
from iperf3_lib.plan_execution import PlanCancelledError, PlanCleanupError, PlanTimeoutError
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials


def _plan(**policy):
    return prepare_trials(
        ClientConfig("127.0.0.1", duration=1, rate=800),
        policy=TrialPolicy(**policy),
        budget=PlanBudget(100, 10000),
    )


def _completed():
    return result_from_iperf_json(
        {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {"sum_received": {"bytes": 100, "seconds": 1}},
        }
    )


async def _run(plan, **kwargs):
    from iperf3_lib.async_trials import arun_plan

    return await arun_plan(plan, **kwargs)


async def _settled(task):
    done, _ = await asyncio.wait((task,), timeout=5)
    assert task in done, "Plan did not finish owned cleanup"
    return task.result()


async def _finish(task):
    if not task.done():
        task.cancel()
    done, _ = await asyncio.wait((task,), timeout=5)
    assert task in done, "Failed-test cleanup could not stop its plan task"
    if not task.cancelled():
        task.exception()


@pytest.fixture
def client_type():
    """Resolve the current client module after other native tests may reload it."""
    from iperf3_lib.iperf_client import Client

    return Client


@pytest_asyncio.fixture(autouse=True)
async def no_loop_errors():
    """Expected child failures must be consumed rather than logged by asyncio."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        yield
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_async_plan_runs_warmup_then_repetitions_without_overlap(monkeypatch, client_type):
    """Each native child task completes before the next trial is admitted."""
    from iperf3_lib import async_trials

    children = []
    active = 0
    clock = [0.0]
    positions = []
    pauses = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds > 0:
            assert active == 0
            assert children[-1].done()
            positions.append(("pause", len(children)))
            pauses.append(seconds)
        await real_sleep(0)
        if seconds > 0:
            clock[0] += seconds

    async def execute(self, **kwargs):
        nonlocal active
        active += 1
        assert active == 1
        children.append(asyncio.current_task())
        positions.append(("start", len(children)))
        await real_sleep(0)
        active -= 1
        positions.append(("finish", len(children)))
        return _completed()

    with monkeypatch.context() as scoped:
        scoped.setattr(client_type, "arun", execute)
        scoped.setattr(
            async_trials,
            "time",
            SimpleNamespace(time=async_trials.time.time, monotonic=lambda: clock[0]),
        )
        scoped.setattr(async_trials.asyncio, "sleep", sleep)
        result = await _run(_plan(repetitions=2, warmup_runs=1, pause_seconds=0.001))
    assert result.execution_success
    assert result.cleanup_confirmed
    assert [record.spec.phase for record in result.trials] == ["warmup", "measured", "measured"]
    assert [record.status for record in result.trials] == ["completed"] * 3
    assert all(record.artifact is not None for record in result.trials)
    assert len(set(children)) == 3
    assert all(task.done() for task in children)
    assert positions == [
        ("start", 1),
        ("finish", 1),
        ("pause", 1),
        ("start", 2),
        ("finish", 2),
        ("pause", 2),
        ("start", 3),
        ("finish", 3),
    ]
    assert pauses == [0.001, 0.001]
    assert result.observed_pause_seconds == result.elapsed_seconds == sum(pauses)
    assert [record.elapsed_seconds for record in result.trials] == [0.0] * 3


@pytest.mark.asyncio
async def test_async_plan_detaches_all_configurations_before_admission(monkeypatch, client_type):
    """Caller and client mutations cannot change later trials or retained specifications."""
    plan = _plan(repetitions=2)
    observed = []

    async def execute(self, **kwargs):
        observed.append(self.cfg.rate)
        self.cfg.rate = 999
        plan.trials[1].config.rate = 555
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    result = await _run(plan)
    assert observed == [800, 800]
    assert [record.spec.config.rate for record in result.trials] == [800, 800]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "incomplete", "exception"])
async def test_stop_on_error_preserves_unstarted_trials_without_an_extra_pause(
    monkeypatch, client_type, outcome
):
    """A recorded native failure or wrapper exception stops admission immediately."""
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        if outcome == "exception":
            raise OSError("worker could not start")
        return result_from_iperf_json({"error": "refused"} if outcome == "failed" else {})

    monkeypatch.setattr(client_type, "arun", execute)
    result = await _settled(asyncio.create_task(_run(_plan(stop_on_error=True, pause_seconds=30))))
    assert [record.status for record in result.trials] == [outcome, "not_run", "not_run"]
    assert len(calls) == 1
    assert result.observed_pause_seconds == 0
    assert result.stop_reason == "stop_on_error"
    assert not result.execution_success
    assert all(record.started_at_seconds is None for record in result.trials[1:])


@pytest.mark.asyncio
async def test_elapsed_admission_budget_zero_never_starts_client(monkeypatch, client_type):
    """The existing elapsed admission stop remains separate from hard cancellation."""
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    plan = replace(_plan(), budget=PlanBudget(100, 10000, 0))
    result = await _run(plan)
    assert result.stop_reason == "elapsed_admission_limit"
    assert [record.status for record in result.trials] == ["not_run"] * 3
    assert result.observed_pause_seconds == 0


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_active_child_cleanup(monkeypatch, client_type):
    """Repeated parent cancellation cannot interrupt the owned child's cleanup wait."""
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    child_cancellations = []

    async def execute(self, **kwargs):
        calls.append(asyncio.current_task())
        if len(calls) == 1:
            return _completed()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            child_cancellations.append(exc)
            cleaning.set()
            await release.wait()
            raise

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("first cancellation")
        await asyncio.wait_for(cleaning.wait(), 2)
        for message in ("second cancellation", "third cancellation"):
            task.cancel(message)
            await asyncio.sleep(0)
        assert not task.done()
        assert not calls[-1].done()
        assert len(calls) == 2
        release.set()
        with pytest.raises(PlanCancelledError, match="first cancellation") as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == ["completed", "cancelled", "not_run"]
        assert partial.trials[0].artifact.result.ok
        assert partial.trials[1].artifact is None
        assert partial.trials[2].started_at_seconds is None
        assert partial.cleanup_confirmed
        assert not partial.execution_success
        assert partial.stop_reason == "cancelled"
        assert len(child_cancellations) == 1
        assert child_cancellations[0].args == ("first cancellation",)
        assert all(child.done() for child in calls)
    finally:
        release.set()
        await _finish(task)


@pytest.mark.asyncio
async def test_cancellation_in_pause_preserves_completed_artifact_and_partial_pause(
    monkeypatch, client_type
):
    """A between-trial stop has no fabricated active trial and retains elapsed pause."""
    from iperf3_lib import async_trials

    pause_entered = asyncio.Event()
    original_sleep = asyncio.sleep
    calls = []

    async def sleep(delay):
        if delay == 30:
            pause_entered.set()
        await original_sleep(delay)

    async def execute(self, **kwargs):
        calls.append(self)
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    monkeypatch.setattr(async_trials.asyncio, "sleep", sleep)
    task = asyncio.create_task(_run(_plan(pause_seconds=30)))
    try:
        await asyncio.wait_for(pause_entered.wait(), 2)
        # Windows 3.12 can expose a coarser monotonic clock. Observe an actual
        # positive pause interval, after entry, before delivering cancellation.
        await original_sleep(0.05)
        task.cancel("pause cancelled")
        with pytest.raises(PlanCancelledError, match="pause cancelled") as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == ["completed", "not_run", "not_run"]
        assert len(calls) == 1
        assert 0 < partial.observed_pause_seconds < 30
        assert partial.observed_pause_seconds <= partial.elapsed_seconds
        assert partial.trials[0].artifact.result.ok
        assert all(record.started_at_seconds is None for record in partial.trials[1:])
    finally:
        await _finish(task)


@pytest.mark.asyncio
async def test_hard_deadline_cancels_active_task_and_retains_completed_trials(
    monkeypatch, client_type
):
    """A plan deadline stops an admitted child and records unstarted work separately."""
    calls, cleaned = [], []

    async def execute(self, **kwargs):
        calls.append(asyncio.current_task())
        if len(calls) == 1:
            return _completed()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan(), timeout=0.2))
    try:
        with pytest.raises(PlanTimeoutError) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == ["completed", "timed_out", "not_run"]
        assert cleaned == [True]
        assert len(calls) == 2 and all(child.done() for child in calls)
        assert partial.cleanup_confirmed
        assert not partial.execution_success
        assert partial.stop_reason == "timeout"
    finally:
        await _finish(task)


@pytest.mark.asyncio
async def test_deadline_during_pause_does_not_admit_another_trial(monkeypatch, client_type):
    """Plan time includes pauses, and an expired pause never starts another worker."""
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan(pause_seconds=30), timeout=0.2))
    try:
        with pytest.raises(PlanTimeoutError) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == ["completed", "not_run", "not_run"]
        assert len(calls) == 1
        assert 0 < partial.observed_pause_seconds < 30
    finally:
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_cleanup_failure_outranks_stop_and_retains_original_owner(
    monkeypatch, client_type, stop
):
    """An unconfirmed cleanup is never downgraded to a normal cancellation or timeout."""
    from iperf3_lib._cancellation import _ExecutionControl

    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owner = _ExecutionControl()
    original = IperfCleanupError("cleanup could not be confirmed", control=owner)
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cause:
            cleaning.set()
            await release.wait()
            raise original from cause

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan(), timeout=0.05 if stop == "deadline" else None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "cancel":
            task.cancel("cleanup cancellation")
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel("repeated during cleanup")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(PlanCleanupError) as caught:
            await _settled(task)
        assert caught.value.cleanup_errors == (original,)
        assert original._control is owner
        assert isinstance(original.__cause__, asyncio.CancelledError)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == [
            "cleanup_failed",
            "not_run",
            "not_run",
        ]
        assert not partial.cleanup_confirmed
        assert not partial.trials[0].cleanup_confirmed
        assert partial.stop_reason == ("cancelled" if stop == "cancel" else "timeout")
        assert len(calls) == 1
    finally:
        release.set()
        await _finish(task)


@pytest.mark.asyncio
async def test_completed_child_survives_parent_cancellation_before_delivery(
    monkeypatch, client_type
):
    """A returned child artifact survives an outward cancellation at its delivery boundary."""
    parent = None
    calls = []

    async def execute(self, **kwargs):
        calls.append(asyncio.current_task())
        asyncio.get_running_loop().call_soon(parent.cancel, "completed child delivery")
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    parent = asyncio.create_task(_run(_plan()))
    try:
        with pytest.raises(PlanCancelledError, match="completed child delivery") as caught:
            await _settled(parent)
        assert [record.status for record in caught.value.partial_result.trials] == [
            "completed",
            "not_run",
            "not_run",
        ]
        assert caught.value.partial_result.trials[0].artifact.result.ok
        assert len(calls) == 1
    finally:
        await _finish(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_stop_while_executor_is_queued_fences_native_admission(
    monkeypatch, client_type, stop
):
    """The real Client.arun control suppresses a queued invocation after the plan stops."""
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    released, submitted = threading.Event(), asyncio.Event()
    native_calls = []
    monkeypatch.setattr(
        client_type, "_run", lambda *args, **kwargs: native_calls.append(True) or _completed()
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(released.wait, 5)

        def submit(executor, function, *args):
            future = original(pool, function, *args)
            submitted.set()
            return future

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(_run(_plan(), timeout=0.05 if stop == "deadline" else None))
        try:
            await asyncio.wait_for(submitted.wait(), 2)
            if stop == "cancel":
                task.cancel("queued plan cancellation")
            with pytest.raises(
                PlanCancelledError if stop == "cancel" else PlanTimeoutError
            ) as caught:
                await _settled(task)
            assert not blocked.done()
            partial = caught.value.partial_result
            assert [record.status for record in partial.trials] == [
                "cancelled" if stop == "cancel" else "timed_out",
                "not_run",
                "not_run",
            ]
            assert partial.cleanup_confirmed
            assert native_calls == []
        finally:
            released.set()
            await _finish(task)
        await asyncio.wait_for(asyncio.wrap_future(pool.submit(lambda: None)), 2)
    assert native_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "1"])
async def test_invalid_timeout_never_admits_client(monkeypatch, client_type, timeout):
    """Invalid overall deadlines fail before native admission or task creation."""
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    with pytest.raises((TypeError, ValueError)):
        await _run(_plan(), timeout=timeout)


@pytest.mark.asyncio
async def test_mutated_plan_is_revalidated_without_native_admission(monkeypatch, client_type):
    """Mutable nested configs cannot bypass finite-plan admission requirements."""
    plan = _plan()
    plan.trials[0].config.duration = 0
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    with pytest.raises(ValueError, match="duration"):
        await _run(plan)


@pytest.mark.asyncio
async def test_returned_unencodable_result_retains_json_safe_evidence(monkeypatch, client_type):
    """Artifact conversion failures preserve trustworthy returned fields without retry."""
    returned = _completed()
    returned.extensions["bad"] = object()

    async def execute(self, **kwargs):
        return returned

    monkeypatch.setattr(client_type, "arun", execute)
    result = await _run(_plan(repetitions=1))
    record = result.trials[0]
    assert record.status == "exception"
    assert record.artifact is None
    assert record.returned_result_evidence["raw"] == returned.raw
    assert record.diagnostics


@pytest.mark.asyncio
async def test_ordinary_failures_continue_without_hidden_retries(monkeypatch, client_type):
    """The async runner preserves the existing stop_on_error=False trial policy."""
    outcomes = iter(
        [OSError("startup failed"), result_from_iperf_json({"error": "refused"}), _completed()]
    )
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client_type, "arun", execute)
    result = await _run(_plan())
    assert [record.status for record in result.trials] == ["exception", "failed", "completed"]
    assert len(calls) == 3
    assert result.trials[0].exception.message == "startup failed"
    assert all(record.artifact is not None for record in result.trials[1:])
    assert result.cleanup_confirmed and not result.execution_success


@pytest.mark.asyncio
async def test_cleanup_failure_without_external_stop_still_freezes_admission(
    monkeypatch, client_type
):
    """stop_on_error=False cannot admit another trial while worker cleanup is unconfirmed."""
    from iperf3_lib._cancellation import _ExecutionControl

    original = IperfCleanupError("unexpected cleanup failure", control=_ExecutionControl())
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        raise original

    monkeypatch.setattr(client_type, "arun", execute)
    with pytest.raises(PlanCleanupError) as caught:
        await _run(_plan())
    assert caught.value.cleanup_errors == (original,)
    assert [record.status for record in caught.value.partial_result.trials] == [
        "cleanup_failed",
        "not_run",
        "not_run",
    ]
    assert caught.value.partial_result.stop_reason == "cleanup_failed"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_bytes", [1, 60000])
async def test_cancelled_trial_partial_events_obey_count_and_byte_bounds(
    monkeypatch, client_type, payload_bytes
):
    """Partial event evidence is detached, bounded and excluded from final artifacts."""
    from iperf3_lib import _ipc
    from iperf3_lib.events import NativeEvent

    entered = asyncio.Event()
    payloads = []

    async def execute(self, **kwargs):
        callback = kwargs["on_event"]
        for sequence in range(1, 71):
            data = {"payload": "x" * payload_bytes}
            payloads.append(data)
            callback(NativeEvent("interval", data, sequence, 10))
        callback(NativeEvent("interval", {"payload": "x" * 65536}, 71, 10))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        payloads[0]["payload"] = "mutated after callback"
        task.cancel("retain bounded events")
        with pytest.raises(PlanCancelledError) as caught:
            await _settled(task)
        record = caught.value.partial_result.trials[0]
        assert record.status == "cancelled" and record.artifact is None
        assert record.events_observed == 71
        assert record.events_dropped == 71 - len(record.partial_events)
        assert 0 < len(record.partial_events) <= 64
        assert record.partial_events[0].data["payload"] == "x" * payload_bytes
        assert [event.sequence for event in record.partial_events] == list(
            range(1, len(record.partial_events) + 1)
        )
        frames = [
            _ipc.encode_frame(
                {
                    "kind": event.kind,
                    "data": event.data,
                    "sequence": event.sequence,
                    "received_at_seconds": event.received_at_seconds,
                }
            )
            for event in record.partial_events
        ]
        assert max(map(len, frames)) <= 65536
        assert sum(map(len, frames)) <= 1024 * 1024
        if payload_bytes == 1:
            assert len(record.partial_events) == 64
        else:
            assert len(record.partial_events) < 64
        assert all(not pending.partial_events for pending in caught.value.partial_result.trials[1:])
    finally:
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,sequence", [("", 1), (" ", 1), ("interval", 0)])
async def test_invalid_event_metadata_cannot_replace_plan_cancellation(
    monkeypatch, client_type, kind, sequence
):
    """Collector rejects metadata that could otherwise make a partial snapshot invalid."""
    from iperf3_lib.events import NativeEvent

    entered = asyncio.Event()

    async def execute(self, **kwargs):
        kwargs["on_event"](NativeEvent(kind, {}, sequence, 10))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("bad event still permits cancellation")
        with pytest.raises(PlanCancelledError) as caught:
            await _settled(task)
        record = caught.value.partial_result.trials[0]
        assert record.partial_events == ()
        assert record.events_observed == record.events_dropped == 1
    finally:
        await _finish(task)


@pytest.mark.asyncio
async def test_pending_cancellation_at_first_checkpoint_admits_no_child(monkeypatch, client_type):
    """Cancellation observed before first admission retains a complete unstarted plan."""
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    task = asyncio.create_task(_run(_plan()))
    asyncio.get_running_loop().call_soon(task.cancel, "before first admission")
    try:
        with pytest.raises(PlanCancelledError, match="before first admission") as caught:
            await _settled(task)
        assert calls == []
        assert [record.status for record in caught.value.partial_result.trials] == ["not_run"] * 3
    finally:
        await _finish(task)


@pytest.mark.asyncio
async def test_real_client_cleanup_error_keeps_first_cancel_cause_and_owner(
    monkeypatch, client_type
):
    """The executor bridge and plan supervisor preserve the same cleanup failure once."""
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    failures = []

    def execute(self, **kwargs):
        control = kwargs["_control"]
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        try:
            control.check()
        except BaseException as cause:
            error = IperfCleanupError("retained native owner", control=control)
            failures.append(error)
            raise error from cause
        pytest.fail("Plan cancellation did not reach the native owner")

    monkeypatch.setattr(client_type, "_run", execute)
    task = asyncio.create_task(_run(_plan()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("native owner's original cancellation")
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel("later cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(PlanCleanupError) as caught:
            await _settled(task)
        assert caught.value.cleanup_errors == tuple(failures)
        assert len(failures) == 1
        assert isinstance(failures[0].__cause__, asyncio.CancelledError)
        assert str(failures[0].__cause__) == "native owner's original cancellation"
        assert caught.value.partial_result.stop_reason == "cancelled"
        assert caught.value.partial_result.trials[0].status == "cleanup_failed"
    finally:
        release.set()
        await _finish(task)


@pytest.mark.asyncio
async def test_final_child_completion_after_deadline_retains_result_but_reports_timeout(
    monkeypatch, client_type
):
    """A completed last child cannot bypass the overall deadline's post-wait check."""
    from iperf3_lib import async_trials

    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(
        async_trials,
        "time",
        SimpleNamespace(time=lambda: 1000 + clock.elapsed, monotonic=lambda: clock.elapsed),
    )

    async def execute(self, **kwargs):
        clock.elapsed = 2.0
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    with pytest.raises(PlanTimeoutError) as caught:
        await _run(_plan(repetitions=1), timeout=1)
    partial = caught.value.partial_result
    assert partial.stop_reason == "timeout"
    assert partial.trials[0].status == "completed"
    assert partial.trials[0].artifact.result.ok
    assert partial.elapsed_seconds == 2
    assert not partial.execution_success


@pytest.mark.asyncio
async def test_child_timeout_without_plan_deadline_is_an_ordinary_exception(
    monkeypatch, client_type
):
    """A child TimeoutError cannot fabricate an unconfigured overall deadline."""

    async def execute(self, **kwargs):
        raise TimeoutError("unrelated child timeout")

    monkeypatch.setattr(client_type, "arun", execute)
    result = await _run(_plan(repetitions=1))
    assert result.stop_reason is None
    assert result.trials[0].status == "exception"
    assert result.trials[0].exception.message == "unrelated child timeout"


@pytest.mark.asyncio
async def test_cleanup_cause_without_plan_deadline_does_not_mask_retained_owner(
    monkeypatch, client_type
):
    """A cleanup cause cannot create an invalid timeout snapshot and hide the live owner."""
    from iperf3_lib._cancellation import _ExecutionControl

    failure = IperfCleanupError("cleanup with unrelated timeout", control=_ExecutionControl())
    cause = TimeoutError("unrelated lower-level timeout")

    async def execute(self, **kwargs):
        raise failure from cause

    monkeypatch.setattr(client_type, "arun", execute)
    with pytest.raises(PlanCleanupError) as caught:
        await _run(_plan())
    assert caught.value.cleanup_errors == (failure,)
    assert failure.__cause__ is cause
    assert caught.value.partial_result.stop_reason == "cleanup_failed"
    assert caught.value.partial_result.trials[0].status == "cleanup_failed"


@pytest.mark.asyncio
async def test_cancellation_between_task_creation_and_child_entry_starts_no_client(
    monkeypatch, client_type
):
    """A queued child checks its owner's new cancellation before native admission."""
    from iperf3_lib import async_trials

    loop = asyncio.get_running_loop()
    original = asyncio.create_task
    calls = []

    async def execute(self, **kwargs):
        calls.append(self)
        return _completed()

    def create_child(coroutine, **kwargs):
        owner = asyncio.current_task()
        loop.call_soon(owner.cancel, "cancel queued child entry")
        return original(coroutine, **kwargs)

    monkeypatch.setattr(client_type, "arun", execute)
    task = loop.create_task(_run(_plan()))
    monkeypatch.setattr(async_trials.asyncio, "create_task", create_child)
    try:
        with pytest.raises(PlanCancelledError, match="cancel queued child entry") as caught:
            await _settled(task)
        assert calls == []
        assert [record.status for record in caught.value.partial_result.trials] == [
            "cancelled",
            "not_run",
            "not_run",
        ]
        assert caught.value.partial_result.cleanup_confirmed
    finally:
        await _finish(task)
