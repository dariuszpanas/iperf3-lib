"""Standalone native qualification of owned asynchronous plans and partial reports."""

import json
import subprocess
import sys
import textwrap

import pytest

CASES = (
    "active-cancel-tcp",
    "pause-cancel-tcp",
    "active-deadline-udp",
    "completed-tcp",
    "stop-on-error-udp",
)

SCENARIO = textwrap.dedent(
    r"""
    import asyncio
    import hashlib
    import importlib.metadata
    import json
    import os
    import pathlib
    import platform
    import re
    import socket
    import subprocess
    import sys
    import tempfile
    import time

    from iperf3_lib import _execution
    from iperf3_lib import async_trials as plan_runner
    from iperf3_lib.async_trials import arun_plan
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.plan_execution import PlanCancelledError, PlanTimeoutError
    from iperf3_lib.plan_reports import (
        dumps_plan_report, loads_plan_report, plan_report_from_execution,
    )
    from iperf3_lib.server_config import ServerConfig
    from iperf3_lib.trials import PlanBudget, TrialPolicy, TrialSpec, prepare_plan

    case = sys.argv[1]
    protocol = case.rsplit('-', 1)[1]
    host = '127.0.0.1'
    with socket.socket() as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]
    processes = []
    real_popen = subprocess.Popen

    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if command == _execution._worker_command():
            processes.append(process)
        return process

    _execution.subprocess.Popen = record

    async def listener_ready(task):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if task.done():
                task.result()
                raise AssertionError('server finished before listener was ready')
            for table in ('/proc/net/tcp', '/proc/net/tcp6'):
                for line in pathlib.Path(table).read_text().splitlines()[1:]:
                    columns = line.split()
                    if columns[3] == '0A' and int(columns[1].split(':')[1], 16) == port:
                        return
            await asyncio.sleep(0.02)
        raise AssertionError('native listener did not appear')

    def assert_released():
        # Inspect before poll()/wait(): those methods could reap on the test's behalf.
        assert all(process.returncode is not None for process in processes)
        assert all(process.stdin.closed and process.stdout.closed for process in processes)
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind((host, port))

    def producer_receipt(result, process):
        from iperf3_lib.ffi.api import POSSIBLE_NAMES, ffi, lib

        receipt = result.extensions['iperf3_lib.worker']
        assert type(receipt['protocol_version']) is int and receipt['protocol_version'] == 1
        assert receipt['pid'] == process.pid
        assert receipt['run_index'] == 1
        for key in ('request_id', 'worker_id'):
            assert re.fullmatch('[0-9a-f]{32}', receipt[key])
        selector = (
            {'source': 'IPERF3_LIB', 'value': os.environ['IPERF3_LIB']}
            if os.getenv('IPERF3_LIB')
            else {'source': 'platform_search', 'candidates': list(POSSIBLE_NAMES)}
        )
        assert receipt['producer'] == {
            'package_version': importlib.metadata.version('iperf3-lib'),
            'python_version': platform.python_version(),
            'native_version': ffi.string(lib.iperf_get_iperf_version()).decode(),
            'library_selector': selector,
        }
        return receipt

    def positive_interval_bytes(events):
        return sum(
            event.data.get('sum', {}).get('bytes', 0)
            for event in events
            if event.kind == 'interval' and isinstance(event.data, dict)
            and isinstance(event.data.get('sum'), dict)
        )

    async def main():
        loop = asyncio.get_running_loop()
        active_observed = asyncio.Event()
        captured_active_event = asyncio.Event()
        pause_entered = asyncio.Event()
        server_results = []
        active_bytes = []

        original_sleep = asyncio.sleep

        async def observe_pause(seconds, *args, **kwargs):
            if seconds == 30:
                pause_entered.set()
            return await original_sleep(seconds, *args, **kwargs)

        plan_runner.asyncio.sleep = observe_pause

        original_capture = plan_runner._Events.__call__

        def observe_capture(capture, event):
            # Observe after the real bounded collector runs. Requiring the
            # second owned client excludes late callbacks from trial one.
            original_capture(capture, event)
            if len(processes) == 3 and positive_interval_bytes((event,)) > 0:
                loop.call_soon_threadsafe(captured_active_event.set)

        plan_runner._Events.__call__ = observe_capture

        def on_result(result):
            server_results.append(result)

        def on_event(event):
            if len(server_results) == 1:
                measured = positive_interval_bytes((event,))
                if measured > 0:
                    active_bytes.append(measured)
                    loop.call_soon_threadsafe(active_observed.set)

        expected_statuses = {
            'active-cancel-tcp': ['completed', 'cancelled', 'not_run'],
            'pause-cancel-tcp': ['completed', 'not_run', 'not_run'],
            'active-deadline-udp': ['completed', 'timed_out', 'not_run'],
            'completed-tcp': ['completed', 'completed', 'completed'],
            'stop-on-error-udp': ['completed', 'failed', 'not_run'],
        }[case]
        expected_stop = {
            'active-cancel-tcp': 'cancelled', 'pause-cancel-tcp': 'cancelled',
            'active-deadline-udp': 'timeout', 'completed-tcp': None,
            'stop-on-error-udp': 'stop_on_error',
        }[case]
        expected_admitted = 1 if case == 'pause-cancel-tcp' else (3 if case == 'completed-tcp' else 2)
        server_runs = 1 if case in ('pause-cancel-tcp', 'stop-on-error-udp') else expected_admitted
        pause = 30 if case == 'pause-cancel-tcp' else 0.25
        active_case = case in ('active-cancel-tcp', 'active-deadline-udp')
        server = Server(config=ServerConfig(
            bind_address=host, port=port, interval_seconds=0.25,
        ))
        server_task = asyncio.create_task(asyncio.to_thread(
            server.serve_forever, max_runs=server_runs,
            on_result=on_result, on_event=on_event, timeout=20,
        ))
        plan_task = None
        # A bound, non-listening socket deterministically rejects the second
        # client's TCP control connection, including for a UDP data trial.
        with socket.socket() as rejected_listener:
            await listener_ready(server_task)
            rejected_listener.bind((host, 0))
            rejected_port = rejected_listener.getsockname()[1]
            configs = [
                ClientConfig(
                    host,
                    port=rejected_port if case == 'stop-on-error-udp' and index == 1 else port,
                    duration=30 if active_case and index == 1 else 1,
                    rate=500_000, protocol=protocol, interval_seconds=0.25,
                    blksize=1200 if protocol == 'udp' else 4096,
                )
                for index in range(3)
            ]
            plan = prepare_plan(
                [TrialSpec(f'trial-{index}', f'cell-{index}', 'measured', 0, config)
                 for index, config in enumerate(configs)],
                policy=TrialPolicy(repetitions=1, pause_seconds=pause, stop_on_error=True),
                budget=PlanBudget(None, None),
            )
            try:
                timeout = 6 if case == 'active-deadline-udp' else None
                plan_task = asyncio.create_task(arun_plan(plan, timeout=timeout))
                if active_case:
                    await asyncio.wait_for(active_observed.wait(), timeout=5)
                    await asyncio.wait_for(captured_active_event.wait(), timeout=2)
                    assert active_bytes and active_bytes[0] > 0
                    if case == 'active-cancel-tcp':
                        plan_task.cancel('native plan cancellation qualification')
                elif case == 'pause-cancel-tcp':
                    await asyncio.wait_for(pause_entered.wait(), timeout=8)
                    # Observe the actual scheduler pause, then allow its real
                    # clock to advance before cancellation interrupts it.
                    await original_sleep(0.05)
                    assert len(processes) == 2 and not plan_task.done()
                    assert processes[1].returncode is not None
                    plan_task.cancel('native plan pause cancellation qualification')
                try:
                    execution = await asyncio.wait_for(plan_task, timeout=12)
                except PlanCancelledError as exc:
                    assert expected_stop == 'cancelled'
                    execution = exc.partial_result
                except PlanTimeoutError as exc:
                    assert expected_stop == 'timeout'
                    execution = exc.partial_result
                else:
                    assert expected_stop not in ('cancelled', 'timeout')
                await asyncio.wait_for(server_task, timeout=8)
                assert not server._run_lock.locked()
                assert_released()
                assert len(processes) == expected_admitted + 1
                assert [trial.spec.trial_id for trial in execution.trials] == [f'trial-{i}' for i in range(3)]
                assert [trial.status for trial in execution.trials] == expected_statuses
                assert execution.stop_reason == expected_stop
                assert execution.cleanup_confirmed is True
                assert execution.execution_success is (case == 'completed-tcp')
                assert all(trial.cleanup_confirmed for trial in execution.trials)
                for trial in execution.trials:
                    if trial.status in ('cancelled', 'timed_out', 'not_run'):
                        assert trial.artifact is None
                    if trial.status == 'not_run':
                        assert trial.reason == expected_stop
                        assert trial.started_at_seconds is None
                        assert trial.completed_at_seconds is None
                        assert trial.elapsed_seconds is None
                first_result = execution.trials[0].artifact.result
                completed_bytes = first_result.raw['end']['sum_received']['bytes']
                assert type(completed_bytes) is int and completed_bytes > 0
                assert first_result.raw['start']['test_start']['protocol'] == protocol.upper()
                assert first_result.extensions['iperf3_lib.rate_intent']['caller_config']['json_stream'] is False
                assert first_result.execution.configuration.requested['json_stream'] is True
                assert first_result.execution.configuration.effective['json_stream'].value is True
                first_producer = producer_receipt(first_result, processes[1])
                interrupted_bytes = 0
                if active_case:
                    interrupted = execution.trials[1]
                    interrupted_bytes = positive_interval_bytes(interrupted.partial_events)
                    assert interrupted_bytes > 0
                    assert interrupted.events_observed >= len(interrupted.partial_events) > 0
                if case == 'pause-cancel-tcp':
                    assert execution.observed_pause_seconds > 0
                if case == 'stop-on-error-udp':
                    failed = execution.trials[1].artifact.result
                    assert failed.ok is False and failed.error
                    producer_receipt(failed, processes[2])
                # Persist real returned history; the bytes become retained CI
                # evidence and must round-trip through the installed codec.
                report = plan_report_from_execution(execution)
                report_json = dumps_plan_report(report)
                with tempfile.TemporaryDirectory(prefix='iperf-plan-report-') as directory:
                    path = pathlib.Path(directory) / 'plan.json'
                    path.write_text(report_json, encoding='utf-8')
                    persisted = path.read_text(encoding='utf-8')
                    restored = loads_plan_report(persisted)
                assert restored == report
                assert restored.execution == execution
                assert restored.kind == 'iperf3-lib.plan-execution' and restored.schema_version == 2
                # Mutating the caller's original request must not alter the
                # partial/complete result or the saved report snapshot.
                plan.trials[0].config.server = 'changed.invalid'
                assert dumps_plan_report(plan_report_from_execution(execution)) == persisted
                owned_before_reuse = len(processes)
                next_server = asyncio.create_task(server.aserve_once(timeout=8))
                await listener_ready(next_server)
                reuse_result = await Client(ClientConfig(
                    host, port=port, duration=1, rate=500_000, protocol=protocol,
                )).arun(timeout=8)
                server_result = await next_server
                assert reuse_result.ok and server_result.ok
                reuse_bytes = reuse_result.raw['end']['sum_received']['bytes']
                assert type(reuse_bytes) is int and reuse_bytes > 0
                assert_released()
                reuse_producer = producer_receipt(reuse_result, processes[-1])
                print(json.dumps({
                    'case': case, 'protocol': protocol,
                    'trial_statuses': expected_statuses, 'stop_reason': expected_stop,
                    'admitted_trials': expected_admitted,
                    'plan_client_workers': owned_before_reuse - 1,
                    'total_workers_before_reuse': owned_before_reuse,
                    'workers_reaped': True, 'pipes_closed': True,
                    'listener_released': True, 'reused': True,
                    'completed_trial_bytes': completed_bytes,
                    'active_bytes_before_stop': sum(active_bytes),
                    'retained_interrupted_bytes': interrupted_bytes,
                    'reuse_bytes': reuse_bytes,
                    'report_json': persisted,
                    'report_sha256': hashlib.sha256(persisted.encode('utf-8')).hexdigest(),
                    'report_roundtrip': True, 'detached': True,
                    'completed_worker': first_producer, 'reuse_worker': reuse_producer,
                }))
            finally:
                if plan_task is not None and not plan_task.done():
                    plan_task.cancel()
                # Failed assertions must not leave native traffic or an idle
                # server thread running when asyncio.run shuts down executors.
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                pending = [task for task in (plan_task, server_task) if task is not None]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    try:
        asyncio.run(main())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    """
)


def test_async_plan_native_scenario_compiles():
    """Check the externally bounded scenario even on native-free developer hosts."""
    compile(SCENARIO, "<native-async-plan-scenario>", "exec")


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES)
def test_native_async_plan_retains_partial_history_and_releases_workers(case, record_property):
    """Owned plans retain exact partial histories and permit measured listener reuse."""
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, case],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["case"] == case
    assert receipt["report_roundtrip"] is True
    assert receipt["workers_reaped"] is True
    assert receipt["completed_trial_bytes"] > 0
    assert receipt["reuse_bytes"] > 0
    record_property("async_plan", json.dumps(receipt))
