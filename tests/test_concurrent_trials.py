"""Concurrent plan admission and cleanup remain owned across independent children."""

import asyncio
import concurrent.futures
import inspect
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from iperf3_lib.concurrent_execution import (
    ConcurrentExecutionPolicy,
    ConcurrentPlanCancelledError,
    ConcurrentPlanCleanupError,
    ConcurrentPlanTimeoutError,
)
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.events import NativeEvent
from iperf3_lib.exceptions import IperfCleanupError, IperfLibraryError
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.trials import PlanBudget, TrialPolicy, TrialSpec, prepare_plan


def _plan(cells=("a", "b", "c"), *, configs=None, repetitions=1, warmup_runs=0, **policy):
    trials = []
    for index, cell in enumerate(cells):
        config = (configs or {}).get(
            cell, ClientConfig("127.0.0.1", port=5201 + index, duration=1, rate=100)
        )
        for phase, count in (("warmup", warmup_runs), ("measured", repetitions)):
            for repetition in range(count):
                trial_id = f"{cell}:{phase}:{repetition}"
                trials.append(
                    TrialSpec(trial_id, cell, phase, repetition, replace(config, title=trial_id))
                )
    return prepare_plan(
        trials,
        policy=TrialPolicy(repetitions=repetitions, warmup_runs=warmup_runs, **policy),
        budget=PlanBudget(1000, None),
    )


def _completed():
    return result_from_iperf_json(
        {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {"sum_received": {"bytes": 100, "seconds": 1}},
        }
    )


async def _run(plan, *, workers=2, target=1000, **kwargs):
    from iperf3_lib.concurrent_trials import arun_concurrent_plan

    return await arun_concurrent_plan(
        plan, policy=ConcurrentExecutionPolicy(workers, target), **kwargs
    )


async def _settled(task):
    done, _ = await asyncio.wait((task,), timeout=5)
    assert task in done, "Concurrent plan did not finish owned cleanup"
    return task.result()


async def _finish(task):
    if not task.done():
        task.cancel()
    done, _ = await asyncio.wait((task,), timeout=5)
    assert task in done, "Failed-test cleanup did not settle the concurrent plan"
    if not task.cancelled():
        task.exception()


class _Clients:
    """Hold public client calls at entry, return, and cancellation-cleanup barriers."""

    def __init__(self, monkeypatch, client_type, plan):
        self.entered = asyncio.Queue()
        self.calls = []
        self.children = {}
        self.active = set()
        self.finished = []
        self.release = {spec.trial_id: asyncio.Event() for spec in plan.trials}
        self.cleaning = {spec.trial_id: asyncio.Event() for spec in plan.trials}
        self.cancelled = {}
        self.outcomes = {}
        self.cleanup_errors = {}
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.callbacks = {}

        async def execute(client, **kwargs):
            trial_id = client.cfg.title
            self.calls.append(trial_id)
            self.children[trial_id] = asyncio.current_task()
            self.callbacks[trial_id] = kwargs["on_event"]
            self.active.add(trial_id)
            self.entered.put_nowait(trial_id)
            try:
                await self.release[trial_id].wait()
                outcome = self.outcomes.get(trial_id)
                if isinstance(outcome, BaseException):
                    raise outcome
                return _completed() if outcome is None else outcome
            except asyncio.CancelledError as error:
                self.cancelled.setdefault(trial_id, []).append(error)
                self.cleaning[trial_id].set()
                await self.cleanup_release.wait()
                if trial_id in self.cleanup_errors:
                    raise self.cleanup_errors[trial_id] from error
                raise
            finally:
                self.active.remove(trial_id)
                self.finished.append(trial_id)

        monkeypatch.setattr(client_type, "arun", execute)

    async def admissions(self, count):
        return [await asyncio.wait_for(self.entered.get(), 5) for _ in range(count)]

    def unblock(self):
        self.cleanup_release.set()
        for event in self.release.values():
            event.set()


@pytest.fixture
def client_type():
    """Resolve the current client after native-binding tests may have reloaded it."""
    from iperf3_lib.iperf_client import Client

    return Client


@pytest_asyncio.fixture(autouse=True)
async def no_loop_errors():
    """Every expected child failure is retrieved instead of escaping to the loop."""
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
async def test_workers_overlap_without_exceeding_capacity_and_keep_declared_order(
    monkeypatch, client_type
):
    """Out-of-order completion releases one slot and never reorders retained artifacts."""
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan))
    try:
        first, second = await clients.admissions(2)
        assert clients.active == {first, second}
        assert clients.calls == [spec.trial_id for spec in plan.trials[:2]]
        clients.release[second].set()
        (third,) = await clients.admissions(1)
        assert clients.active == {first, third}
        assert clients.children[second].done()
        clients.release[third].set()
        clients.release[first].set()
        result = await _settled(task)
        assert result.execution_success and result.cleanup_confirmed
        assert [record.spec.trial_id for record in result.trials] == [
            spec.trial_id for spec in plan.trials
        ]
        assert [record.admission_index for record in result.trials] == [1, 2, 3]
        assert all(record.artifact.result.ok for record in result.trials)
        assert result.trials[1].released_offset_seconds <= result.trials[2].admitted_offset_seconds
        assert all(child.done() for child in clients.children.values())
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_each_cell_keeps_warmup_and_repetition_dependencies_with_backfill(
    monkeypatch, client_type
):
    """A blocked predecessor does not prevent an unrelated cell from occupying a slot."""
    plan = _plan(("a", "b"), repetitions=2, warmup_runs=1)
    # Distinct endpoints within each cell prove dependency ordering itself,
    # rather than accidentally exercising the automatic endpoint exclusion.
    plan = prepare_plan(
        [
            replace(spec, config=replace(spec.config, port=5300 + index))
            for index, spec in enumerate(plan.trials)
        ],
        policy=plan.policy,
        budget=plan.budget,
    )
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, workers=4))
    try:
        assert await clients.admissions(2) == ["a:warmup:0", "b:warmup:0"]
        assert len(clients.active) == 2
        clients.release["b:warmup:0"].set()
        assert await clients.admissions(1) == ["b:measured:0"]
        clients.release["b:measured:0"].set()
        assert await clients.admissions(1) == ["b:measured:1"]
        assert clients.active == {"a:warmup:0", "b:measured:1"}
        clients.unblock()
        result = await _settled(task)
        assert result.execution_success
        assert [record.admission_index for record in result.trials] == [1, 5, 6, 2, 3, 4]
        for cell in ("a", "b"):
            records = [record for record in result.trials if record.spec.cell_id == cell]
            for previous, following in zip(records, records[1:], strict=False):
                assert previous.released_offset_seconds <= following.admitted_offset_seconds
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hosts", [("EXAMPLE.test.", "example.test"), ("2001:db8::1", "2001:0db8:0:0:0:0:0:1")]
)
async def test_canonical_endpoint_exclusion_applies_across_protocols(
    monkeypatch, client_type, hosts
):
    """Equivalent endpoint spelling excludes a peer even with a different protocol."""
    configs = {
        "a": ClientConfig(hosts[0], duration=1, rate=100),
        "b": ClientConfig(hosts[1], duration=1, rate=100, protocol=Protocol.UDP),
        "c": ClientConfig(hosts[1], port=5202, duration=1, rate=100),
    }
    plan = _plan(configs=configs)
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, workers=3))
    try:
        assert await clients.admissions(2) == ["a:measured:0", "c:measured:0"]
        assert "b:measured:0" not in clients.calls
        clients.release["a:measured:0"].set()
        assert await clients.admissions(1) == ["b:measured:0"]
        clients.unblock()
        result = await _settled(task)
        assert result.execution_success
        assert result.trials[0].resource_keys == result.trials[1].resource_keys
        assert result.trials[0].resource_keys != result.trials[2].resource_keys
        assert [record.admission_index for record in result.trials] == [1, 3, 2]
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_declared_resource_keys_are_added_to_automatic_endpoint_exclusions(
    monkeypatch, client_type
):
    """User keys serialize shared infrastructure while unrelated work backfills."""
    plan = _plan()
    resources = {"a": ("rack",), "b": ("rack",), "c": ("other",)}
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, workers=3, resources=resources))
    try:
        assert await clients.admissions(2) == ["a:measured:0", "c:measured:0"]
        resources["b"] = ("changed after admission",)
        clients.release["c:measured:0"].set()
        await asyncio.sleep(0)
        assert "b:measured:0" not in clients.calls
        clients.release["a:measured:0"].set()
        assert await clients.admissions(1) == ["b:measured:0"]
        clients.unblock()
        result = await _settled(task)
        assert result.resources["b"] == ("rack",)
        assert all(len(record.resource_keys) == 2 for record in result.trials)
        assert "user:rack" in result.trials[1].resource_keys
        assert any(key.startswith("endpoint:") for key in result.trials[1].resource_keys)
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_rate_reservations_count_streams_and_directions_and_allow_backfill(
    monkeypatch, client_type
):
    """A finite configured aggregate rate reserves both directions until release."""
    configs = {
        "a": ClientConfig(
            "127.0.0.1", port=5201, duration=1, rate=100, parallel=2, bidirectional=True
        ),
        "b": ClientConfig("127.0.0.1", port=5202, duration=1, rate=200),
        "c": ClientConfig("127.0.0.1", port=5203, duration=1, rate=100),
    }
    plan = _plan(configs=configs)
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, workers=3, target=500))
    try:
        assert await clients.admissions(2) == ["a:measured:0", "c:measured:0"]
        clients.release["c:measured:0"].set()
        await asyncio.sleep(0)
        assert "b:measured:0" not in clients.calls
        clients.release["a:measured:0"].set()
        assert await clients.admissions(1) == ["b:measured:0"]
        clients.unblock()
        result = await _settled(task)
        assert [record.target_bps for record in result.trials] == [400, 200, 100]
        assert [record.admission_index for record in result.trials] == [1, 3, 2]
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_repeated_parent_cancellation_drains_every_child_once(monkeypatch, client_type):
    """Multiple cleanup waits remain owned and retain independent partial event evidence."""
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    clients.cleanup_release.clear()
    task = asyncio.create_task(_run(plan))
    try:
        admitted = await clients.admissions(2)
        payloads = [{"cell": trial_id} for trial_id in admitted]
        for trial_id, payload in zip(admitted, payloads, strict=True):
            clients.callbacks[trial_id](NativeEvent("interval", payload, 1, 10))
        task.cancel("first concurrent cancellation")
        await asyncio.gather(
            *(asyncio.wait_for(clients.cleaning[trial_id].wait(), 5) for trial_id in admitted)
        )
        for message in ("second cancellation", "third cancellation"):
            task.cancel(message)
            await asyncio.sleep(0)
        assert not task.done()
        assert all(
            not child.done() and child.cancelling() == 1 for child in clients.children.values()
        )
        assert clients.calls == admitted
        payloads[0]["cell"] = "mutated"
        clients.cleanup_release.set()
        with pytest.raises(
            ConcurrentPlanCancelledError, match="first concurrent cancellation"
        ) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == ["cancelled", "cancelled", "not_run"]
        assert partial.stop_reason == partial.termination_reason == "cancelled"
        assert partial.stopped_offset_seconds == partial.termination_offset_seconds
        assert partial.cleanup_confirmed and not partial.execution_success
        assert [record.partial_events[0].data["cell"] for record in partial.trials[:2]] == admitted
        assert all(
            record.events_observed == 1 and record.events_dropped == 0
            for record in partial.trials[:2]
        )
        assert partial.trials[2].admission_index is None
        assert all(
            errors[0].args == ("first concurrent cancellation",)
            for errors in clients.cancelled.values()
        )
        assert all(child.done() for child in clients.children.values())
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_all_cleanup_errors_keep_original_owners_and_reservations(monkeypatch, client_type):
    """A cleanup aggregate retains every failed owner without falsely releasing capacity."""
    from iperf3_lib._cancellation import _ExecutionControl

    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    failures = tuple(
        IperfCleanupError(f"cleanup {index}", control=_ExecutionControl()) for index in range(2)
    )
    task = asyncio.create_task(_run(plan))
    try:
        admitted = await clients.admissions(2)
        clients.cleanup_errors.update(zip(admitted, failures, strict=True))
        task.cancel("owned aggregate")
        with pytest.raises(ConcurrentPlanCleanupError) as caught:
            await _settled(task)
        assert caught.value.cleanup_errors == failures
        assert all(
            actual is expected
            for actual, expected in zip(caught.value.cleanup_errors, failures, strict=True)
        )
        assert all(isinstance(error.__cause__, asyncio.CancelledError) for error in failures)
        assert all(error.__cause__.args == ("owned aggregate",) for error in failures)
        partial = caught.value.partial_result
        assert [record.status for record in partial.trials] == [
            "cleanup_failed",
            "cleanup_failed",
            "not_run",
        ]
        assert partial.stop_reason == partial.termination_reason == "cancelled"
        assert not partial.cleanup_confirmed
        assert all(
            record.finished_offset_seconds is not None and record.released_offset_seconds is None
            for record in partial.trials[:2]
        )
        assert clients.calls == admitted
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_spontaneous_cleanup_failure_cancels_peer_even_without_stop_on_error(
    monkeypatch, client_type
):
    """Unconfirmed cleanup stops every owned worker and prevents all new admission."""
    from iperf3_lib._cancellation import _ExecutionControl

    plan = _plan(stop_on_error=False)
    clients = _Clients(monkeypatch, client_type, plan)
    failure = IperfCleanupError("worker remains owned", control=_ExecutionControl())
    clients.outcomes["a:measured:0"] = failure
    task = asyncio.create_task(_run(plan))
    try:
        await clients.admissions(2)
        clients.release["a:measured:0"].set()
        with pytest.raises(ConcurrentPlanCleanupError) as caught:
            await _settled(task)
        assert caught.value.cleanup_errors == (failure,)
        partial = caught.value.partial_result
        assert partial.stop_reason == partial.termination_reason == "cleanup_failed"
        assert [record.status for record in partial.trials] == [
            "cleanup_failed",
            "cancelled",
            "not_run",
        ]
        assert list(clients.cancelled) == ["b:measured:0"]
        assert len(clients.calls) == 2
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "incomplete", "exception"])
async def test_stop_on_error_drains_started_peer_and_does_not_refill_done_batch(
    monkeypatch, client_type, failure
):
    """A success in the same completion batch cannot hide a peer failure from admission."""
    plan = _plan(("a", "b", "c", "d"), stop_on_error=True)
    clients = _Clients(monkeypatch, client_type, plan)
    clients.outcomes["b:measured:0"] = (
        OSError("failed worker")
        if failure == "exception"
        else result_from_iperf_json({"error": "refused"} if failure == "failed" else {})
    )
    task = asyncio.create_task(_run(plan, workers=3))
    try:
        admitted = await clients.admissions(3)
        # Both futures finish before their waiting supervisor is made runnable.
        clients.release["a:measured:0"].set()
        clients.release["b:measured:0"].set()
        await asyncio.wait((clients.children[admitted[0]], clients.children[admitted[1]]))
        await asyncio.sleep(0)
        assert clients.calls == admitted
        assert not clients.children["c:measured:0"].done()
        assert clients.cancelled == {}
        clients.release["c:measured:0"].set()
        result = await _settled(task)
        assert [record.status for record in result.trials] == [
            "completed",
            failure,
            "completed",
            "not_run",
        ]
        assert result.stop_reason == "stop_on_error"
        assert result.termination_reason is None and result.termination_offset_seconds is None
        assert result.trials[3].reason == "stop_on_error"
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_cancellation_after_soft_stop_retains_both_stop_decisions(monkeypatch, client_type):
    """A soft admission stop does not suppress later caller cancellation of its peers."""
    plan = _plan(stop_on_error=True)
    clients = _Clients(monkeypatch, client_type, plan)
    clients.outcomes["a:measured:0"] = OSError("first soft stop")
    task = asyncio.create_task(_run(plan))
    try:
        admitted = await clients.admissions(2)
        clients.release[admitted[0]].set()
        await asyncio.wait((clients.children[admitted[0]],))
        # Allow the supervisor to consume the returned failure before cancellation.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel("later hard stop")
        with pytest.raises(ConcurrentPlanCancelledError, match="later hard stop") as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert partial.stop_reason == "stop_on_error"
        assert partial.termination_reason == "cancelled"
        assert partial.stopped_offset_seconds <= partial.termination_offset_seconds
        assert [record.status for record in partial.trials] == ["exception", "cancelled", "not_run"]
        assert partial.trials[1].reason == "cancelled"
        assert partial.trials[2].reason == "stop_on_error"
    finally:
        clients.unblock()
        await _finish(task)


def _clock(monkeypatch):
    from iperf3_lib import concurrent_trials

    clock = [100.0]
    monkeypatch.setattr(
        concurrent_trials,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=concurrent_trials.time.time),
    )
    return clock


@pytest.mark.asyncio
async def test_elapsed_budget_freezes_admission_without_cancelling_active_peers(
    monkeypatch, client_type
):
    """The elapsed budget admits no replacement while existing work finishes naturally."""
    clock = _clock(monkeypatch)
    plan = replace(_plan(), budget=PlanBudget(1000, None, 5))
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan))
    try:
        admitted = await clients.admissions(2)
        clock[0] += 6
        clients.release[admitted[0]].set()
        await asyncio.wait((clients.children[admitted[0]],))
        await asyncio.sleep(0)
        assert clients.calls == admitted and clients.cancelled == {}
        assert not clients.children[admitted[1]].done()
        clients.release[admitted[1]].set()
        result = await _settled(task)
        assert result.stop_reason == "elapsed_admission_limit"
        assert result.termination_reason is None
        assert [record.status for record in result.trials] == ["completed", "completed", "not_run"]
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["cancelled", "timeout"])
async def test_first_hard_stop_survives_later_cancellation_or_deadline(
    monkeypatch, client_type, first
):
    """Cleanup extends past a deadline without changing the first hard-stop decision."""
    clock = _clock(monkeypatch)
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    clients.cleanup_release.clear()
    task = asyncio.create_task(_run(plan, timeout=10))
    try:
        admitted = await clients.admissions(2)
        if first == "cancelled":
            task.cancel("earliest cancellation")
        else:
            clock[0] += 11
            clients.release[admitted[0]].set()
        await asyncio.wait_for(clients.cleaning[admitted[1]].wait(), 5)
        clock[0] += 11
        task.cancel("late cancellation during cleanup")
        await asyncio.sleep(0)
        assert not task.done()
        clients.cleanup_release.set()
        with pytest.raises(
            ConcurrentPlanCancelledError if first == "cancelled" else ConcurrentPlanTimeoutError
        ) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert partial.stop_reason == partial.termination_reason == first
        assert partial.trials[1].status == ("cancelled" if first == "cancelled" else "timed_out")
        assert partial.trials[0].status == ("cancelled" if first == "cancelled" else "completed")
        assert partial.trials[2].status == "not_run"
        if first == "cancelled":
            assert caught.value.args == ("earliest cancellation",)
        assert all(child.done() for child in clients.children.values())
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_cancelled_delivery_retains_completed_artifacts_without_refilling(
    monkeypatch, client_type
):
    """Completed children remain evidence when cancellation arrives before delivery."""
    parent = None
    calls = []

    async def execute(self, **kwargs):
        calls.append(self.cfg.title)
        if len(calls) == 2:
            asyncio.get_running_loop().call_soon(parent.cancel, "delivery boundary")
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    parent = asyncio.create_task(_run(_plan()))
    try:
        with pytest.raises(ConcurrentPlanCancelledError, match="delivery boundary") as caught:
            await _settled(parent)
        assert len(calls) == 2
        assert [record.status for record in caught.value.partial_result.trials] == [
            "completed",
            "completed",
            "not_run",
        ]
        assert all(record.artifact.result.ok for record in caught.value.partial_result.trials[:2])
    finally:
        await _finish(parent)


@pytest.mark.asyncio
async def test_stop_while_multiple_executor_calls_are_queued_fences_native_admission(
    monkeypatch, client_type
):
    """Actual Client.arun controls fence every queued invocation after caller cancellation."""
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    released, submitted = threading.Event(), asyncio.Event()
    native_calls = []
    submissions = 0
    monkeypatch.setattr(
        client_type, "_run", lambda *args, **kwargs: native_calls.append(True) or _completed()
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(released.wait, 5)

        def submit(executor, function, *args):
            nonlocal submissions
            future = original(pool, function, *args)
            submissions += 1
            if submissions == 2:
                submitted.set()
            return future

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(_run(_plan()))
        try:
            await asyncio.wait_for(submitted.wait(), 5)
            task.cancel("queued concurrent workers")
            with pytest.raises(ConcurrentPlanCancelledError) as caught:
                await _settled(task)
            assert not blocked.done()
            assert native_calls == []
            assert caught.value.partial_result.cleanup_confirmed
            assert [record.status for record in caught.value.partial_result.trials] == [
                "cancelled",
                "cancelled",
                "not_run",
            ]
        finally:
            released.set()
            await _finish(task)
        await asyncio.wait_for(asyncio.wrap_future(pool.submit(lambda: None)), 5)
    assert native_calls == []


@pytest.mark.asyncio
async def test_pending_cancel_at_first_checkpoint_admits_nothing(monkeypatch, client_type):
    """The initial checkpoint observes queued cancellation before allocating client tasks."""
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    task = asyncio.create_task(_run(_plan()))
    asyncio.get_running_loop().call_soon(task.cancel, "before concurrent admission")
    try:
        with pytest.raises(
            ConcurrentPlanCancelledError, match="before concurrent admission"
        ) as caught:
            await _settled(task)
        assert [record.status for record in caught.value.partial_result.trials] == ["not_run"] * 3
    finally:
        await _finish(task)


@pytest.mark.asyncio
async def test_detached_configs_cannot_change_later_reservations(monkeypatch, client_type):
    """Mutations after entry cannot alter queued configuration, rate, or resource evidence."""
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, workers=1))
    try:
        (first,) = await clients.admissions(1)
        plan.trials[1].config.rate = 999
        plan.trials[1].config.server = "changed.invalid"
        clients.release[first].set()
        assert await clients.admissions(1) == ["b:measured:0"]
        clients.unblock()
        result = await _settled(task)
        assert result.trials[1].spec.config.rate == result.trials[1].target_bps == 100
        assert str(result.trials[1].spec.config.server) == "127.0.0.1"
        assert all("changed.invalid" not in key for key in result.trials[1].resource_keys)
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resources",
    [
        {"unknown": ("shared",)},
        {"a": ("",)},
        {"a": ("  ",)},
        {"a": ("same", "same")},
        {"a": ["not a tuple"]},
        {"a": (True,)},
    ],
)
async def test_invalid_resources_reject_entire_plan_before_admission(
    monkeypatch, client_type, resources
):
    """Invalid declarations never cause partial traffic before static validation finishes."""
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    with pytest.raises(ValueError):
        await _run(_plan(), resources=resources)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["pause", "client_port", "unlimited", "unknown_rate", "oversized_rate"]
)
async def test_unsupported_concurrent_plan_rejects_before_any_client(
    monkeypatch, client_type, invalid
):
    """The complete plan must fit explicit finite concurrency constraints before traffic."""
    config = ClientConfig("127.0.0.1", port=5202, duration=1, rate=100)
    if invalid == "client_port":
        config.client_port = 5400
    elif invalid == "unlimited":
        config.rate = 0
    elif invalid == "unknown_rate":
        config.rate = None
    elif invalid == "oversized_rate":
        config.rate = 1001
    plan = _plan(configs={"b": config}, pause_seconds=1 if invalid == "pause" else 0)
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    with pytest.raises(ValueError):
        await _run(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "1"])
async def test_invalid_deadline_never_admits_client(monkeypatch, client_type, timeout):
    """Overall deadlines remain positive finite numeric values without boolean coercion."""
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("admitted"))
    with pytest.raises((ValueError, TypeError)):
        await _run(_plan(), timeout=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("soft_stop", ["stop_on_error", "elapsed_admission_limit"])
async def test_hard_deadline_still_applies_after_soft_admission_stop(
    monkeypatch, client_type, soft_stop
):
    """Soft admission stops cannot leave surviving peers outside the plan deadline."""
    clock = _clock(monkeypatch)
    plan = _plan(stop_on_error=soft_stop == "stop_on_error")
    if soft_stop == "elapsed_admission_limit":
        plan = replace(plan, budget=PlanBudget(1000, None, 5))
    clients = _Clients(monkeypatch, client_type, plan)
    if soft_stop == "stop_on_error":
        clients.outcomes["a:measured:0"] = OSError("admission stopped")
    task = asyncio.create_task(_run(plan, timeout=10))
    try:
        admitted = await clients.admissions(2)
        clock[0] += 6
        clients.release[admitted[0]].set()
        await asyncio.wait((clients.children[admitted[0]],))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert clients.calls == admitted and clients.cancelled == {}
        clock[0] += 5
        # Deliver an ordinary completion after the hard deadline. Its artifact
        # remains evidence even though the plan must now report the deadline.
        clients.release[admitted[1]].set()
        with pytest.raises(ConcurrentPlanTimeoutError) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert partial.stop_reason == soft_stop
        assert partial.termination_reason == "timeout"
        assert partial.stopped_offset_seconds < partial.termination_offset_seconds
        assert [record.status for record in partial.trials] == [
            "exception" if soft_stop == "stop_on_error" else "completed",
            "completed",
            "not_run",
        ]
        assert partial.trials[2].reason == soft_stop
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_final_completed_batch_cannot_hide_an_expired_plan_deadline(monkeypatch, client_type):
    """Completion evidence is retained while deadline arbitration still runs for the final batch."""
    clock = _clock(monkeypatch)
    plan = _plan(("a", "b"))
    clients = _Clients(monkeypatch, client_type, plan)
    task = asyncio.create_task(_run(plan, timeout=10))
    try:
        await clients.admissions(2)
        clock[0] += 11
        clients.unblock()
        with pytest.raises(ConcurrentPlanTimeoutError) as caught:
            await _settled(task)
        partial = caught.value.partial_result
        assert partial.stop_reason == partial.termination_reason == "timeout"
        assert [record.status for record in partial.trials] == ["completed", "completed"]
        assert all(record.artifact.result.ok for record in partial.trials)
        assert partial.cleanup_confirmed and not partial.execution_success
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 10])
async def test_child_timeout_requires_explicit_deadline_to_terminate_peers(
    monkeypatch, client_type, timeout
):
    """An unrelated TimeoutError is an ordinary trial failure without a plan deadline."""
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    clients.outcomes["a:measured:0"] = TimeoutError("worker timeout")
    task = asyncio.create_task(_run(plan, timeout=timeout))
    try:
        await clients.admissions(2)
        clients.release["a:measured:0"].set()
        if timeout is None:
            assert await clients.admissions(1) == ["c:measured:0"]
            assert clients.cancelled == {}
            clients.unblock()
            result = await _settled(task)
            assert result.stop_reason is result.termination_reason is None
            assert [record.status for record in result.trials] == [
                "exception",
                "completed",
                "completed",
            ]
        else:
            with pytest.raises(ConcurrentPlanTimeoutError) as caught:
                await _settled(task)
            partial = caught.value.partial_result
            assert partial.stop_reason == partial.termination_reason == "timeout"
            assert [record.status for record in partial.trials] == [
                "timed_out",
                "timed_out",
                "not_run",
            ]
            assert list(clients.cancelled) == ["b:measured:0"]
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_child_entry_fence_observes_cancel_during_admission(monkeypatch, client_type):
    """Already-created but unstarted child tasks cannot enter Client after cancellation."""
    from iperf3_lib import concurrent_trials

    original_create = asyncio.create_task
    parent = original_create(_run(_plan()))
    children = []

    def create(coroutine, **kwargs):
        child = original_create(coroutine, **kwargs)
        children.append(child)
        if len(children) == 1:
            parent.cancel("cancel before child entry")
        return child

    monkeypatch.setattr(concurrent_trials.asyncio, "create_task", create)
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("entered Client"))
    try:
        with pytest.raises(
            ConcurrentPlanCancelledError, match="cancel before child entry"
        ) as caught:
            await _settled(parent)
        assert children and all(child.done() for child in children)
        assert all(record.artifact is None for record in caught.value.partial_result.trials)
        assert caught.value.partial_result.cleanup_confirmed
    finally:
        await _finish(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["worker_crash", "transport", "invalid_result"])
async def test_exception_retains_bounded_native_events_without_final_artifact(
    monkeypatch, client_type, failure
):
    """A failed worker retains its delivered prefix while admitted peers finish normally."""
    plan = _plan(stop_on_error=True)
    clients = _Clients(monkeypatch, client_type, plan)
    clients.outcomes["a:measured:0"] = {
        "worker_crash": IperfLibraryError("Native worker exited with code -9"),
        "transport": BrokenPipeError("Native worker transport closed"),
        "invalid_result": object(),
    }[failure]
    task = asyncio.create_task(_run(plan))
    try:
        await clients.admissions(2)
        payload = {"sum": {"bytes": 20}}
        for sequence in range(1, 67):
            clients.callbacks["a:measured:0"](NativeEvent("interval", payload, sequence, 10))
        clients.callbacks["b:measured:0"](NativeEvent("interval", payload, 1, 10))
        payload["sum"]["bytes"] = 999
        clients.unblock()
        result = await _settled(task)
        assert [record.status for record in result.trials] == ["exception", "completed", "not_run"]
        assert result.stop_reason == "stop_on_error" and result.termination_reason is None
        failed = result.trials[0]
        assert failed.artifact is None and failed.exception is not None
        assert failed.cleanup_confirmed and failed.released_offset_seconds is not None
        assert [event.sequence for event in failed.partial_events] == list(range(1, 65))
        assert all(event.data == {"sum": {"bytes": 20}} for event in failed.partial_events)
        assert failed.events_observed == 66 and failed.events_dropped == 2
        assert result.trials[1].partial_events == ()
        assert result.trials[1].events_observed == result.trials[1].events_dropped == 0
        assert result.trials[1].artifact.result.ok
        assert result.cleanup_confirmed and not result.execution_success
        assert clients.cancelled == {}
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
async def test_unencodable_result_preserves_evidence_while_other_workers_finish(
    monkeypatch, client_type
):
    """A serialization failure is retained once and does not discard another worker's artifact."""
    plan = _plan(("a", "b"))
    clients = _Clients(monkeypatch, client_type, plan)
    returned = _completed()
    returned.extensions["not.portable"] = object()
    clients.outcomes["a:measured:0"] = returned
    task = asyncio.create_task(_run(plan))
    try:
        await clients.admissions(2)
        event = NativeEvent("interval", {"sum": {"bytes": 20}}, 1, 10)
        clients.callbacks["a:measured:0"](event)
        clients.unblock()
        result = await _settled(task)
        assert [record.status for record in result.trials] == ["exception", "completed"]
        assert result.trials[0].returned_result_evidence["raw"] == returned.raw
        assert result.trials[0].diagnostics
        assert result.trials[0].artifact is None
        assert result.trials[0].partial_events == (event,)
        assert result.trials[0].events_observed == 1 and result.trials[0].events_dropped == 0
        assert result.trials[1].artifact.result.ok
        assert result.cleanup_confirmed and not result.execution_success
    finally:
        clients.unblock()
        await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_error", [False, True])
async def test_second_task_creation_failure_closes_coroutine_and_keeps_peer_owned(
    monkeypatch, client_type, stop_on_error
):
    """A factory failure is one retained trial failure with no leaked coroutine or peer."""
    from iperf3_lib import concurrent_trials

    plan = _plan(stop_on_error=stop_on_error)
    clients = _Clients(monkeypatch, client_type, plan)
    original_create = asyncio.create_task
    parent = original_create(_run(plan))
    attempted = []

    def create(coroutine, **kwargs):
        attempted.append(coroutine)
        if len(attempted) == 2:
            raise RuntimeError("second task factory failed")
        return original_create(coroutine, **kwargs)

    monkeypatch.setattr(concurrent_trials.asyncio, "create_task", create)
    try:
        assert await clients.admissions(1) == ["a:measured:0"]
        if not stop_on_error:
            assert await clients.admissions(1) == ["c:measured:0"]
        assert not parent.done()
        assert inspect.getcoroutinestate(attempted[1]) == inspect.CORO_CLOSED
        assert "b:measured:0" not in clients.calls
        assert not clients.children["a:measured:0"].done()
        clients.unblock()
        result = await _settled(parent)
        assert [record.status for record in result.trials] == [
            "completed",
            "exception",
            "not_run" if stop_on_error else "completed",
        ]
        assert result.trials[1].exception.message == "second task factory failed"
        assert result.trials[1].admission_index == 2
        assert result.trials[1].artifact is None
        assert result.trials[1].released_offset_seconds is not None
        assert result.cleanup_confirmed and clients.cancelled == {}
        assert all(child.done() for child in clients.children.values())
        assert result.stop_reason == ("stop_on_error" if stop_on_error else None)
    finally:
        clients.unblock()
        await _finish(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_eager_task_factory_consumes_immediate_outcome_before_further_admission(
    monkeypatch, client_type, fail
):
    """Immediate task completion cannot stall dependent work or bypass stop_on_error."""
    calls = []

    async def execute(self, **kwargs):
        calls.append(self.cfg.title)
        if fail:
            raise OSError("eager failure")
        return _completed()

    monkeypatch.setattr(client_type, "arun", execute)
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        result = await _run(_plan(("a",), repetitions=3, stop_on_error=True))
    finally:
        loop.set_task_factory(original_factory)
    assert [record.status for record in result.trials] == (
        ["exception", "not_run", "not_run"] if fail else ["completed"] * 3
    )
    assert len(calls) == (1 if fail else 3)
    assert result.execution_success is not fail
    assert result.cleanup_confirmed


@pytest.mark.asyncio
async def test_eager_completed_peer_releases_slot_while_another_worker_is_held(
    monkeypatch, client_type
):
    """Immediate success must refill its own slot without waiting for a slower peer."""
    plan = _plan()
    clients = _Clients(monkeypatch, client_type, plan)
    clients.release["b:measured:0"].set()
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    parent = asyncio.create_task(_run(plan))
    try:
        assert await clients.admissions(3) == ["a:measured:0", "b:measured:0", "c:measured:0"]
        assert clients.active == {"a:measured:0", "c:measured:0"}
        clients.unblock()
        result = await _settled(parent)
        assert result.execution_success
        assert result.trials[1].released_offset_seconds <= result.trials[2].admitted_offset_seconds
    finally:
        loop.set_task_factory(original_factory)
        clients.unblock()
        await _finish(parent)


@pytest.mark.asyncio
async def test_eager_child_entry_fence_preserves_pending_parent_cancel_arguments(
    monkeypatch, client_type
):
    """An eager child's fence must not replace its parent's pending cancellation message."""
    from iperf3_lib import concurrent_trials

    original_create = asyncio.create_task
    parent = original_create(_run(_plan()))
    children = []

    def create(coroutine, **kwargs):
        parent.cancel("pending eager cancellation")
        child = asyncio.eager_task_factory(asyncio.get_running_loop(), coroutine, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(concurrent_trials.asyncio, "create_task", create)
    monkeypatch.setattr(client_type, "arun", lambda *args, **kwargs: pytest.fail("entered Client"))
    try:
        with pytest.raises(
            ConcurrentPlanCancelledError, match="pending eager cancellation"
        ) as caught:
            await _settled(parent)
        assert len(children) == 1
        assert all(child.done() for child in children)
        assert caught.value.partial_result.cleanup_confirmed
        assert [record.status for record in caught.value.partial_result.trials] == [
            "cancelled",
            "not_run",
            "not_run",
        ]
    finally:
        await _finish(parent)
