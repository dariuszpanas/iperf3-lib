"""Standalone installed qualification of bounded, owned concurrent plans."""

import json
import subprocess
import sys
import textwrap

import pytest

CASES = (
    "overlap-rate-cap-tcp",
    "conflicting-resources-tcp",
    "two-active-cancel-tcp",
    "two-active-deadline-udp",
    "two-active-worker-crash-tcp",
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
    import signal
    import socket
    import subprocess
    import sys
    import tempfile
    import threading
    import time

    from iperf3_lib import _execution
    from iperf3_lib import concurrent_trials as runner
    from iperf3_lib.concurrent_trials import arun_concurrent_plan
    from iperf3_lib.concurrent_execution import ConcurrentExecutionPolicy
    from iperf3_lib.concurrent_reports import (
        concurrent_report_from_execution, dumps_concurrent_report, loads_concurrent_report,
    )
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.server_config import ServerConfig
    from iperf3_lib.trials import PlanBudget, TrialPolicy, TrialSpec, prepare_plan

    case = sys.argv[1]
    protocol = case.rsplit('-', 1)[1]
    host = '127.0.0.1'
    origin = time.monotonic()
    processes = []
    ownership = {}
    ready = {}
    measured = {}
    liveness = []
    thread_owner = threading.local()
    real_popen = subprocess.Popen
    real_worker = _execution.run_worker
    real_accept = _execution._Responses.accept
    real_execute = runner._execute

    def elapsed():
        return time.monotonic() - origin

    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if command == _execution._worker_command():
            processes.append(process)
            trial = getattr(thread_owner, 'trial', None)
            if trial is not None:
                assert trial not in ownership
                ownership[trial] = {'pid': process.pid, 'started': elapsed(), 'finished': None}
        return process

    def observe_worker(role, options, **kwargs):
        callback = kwargs.get('on_event')
        thread_owner.trial = getattr(callback, 'trial_id', None)
        try:
            return real_worker(role, options, **kwargs)
        finally:
            thread_owner.trial = None

    def observe_accept(responses, message):
        outcome = real_accept(responses, message)
        if message['type'] == 'ready':
            ready[responses.pid] = {
                key: message[key]
                for key in ('protocol_version', 'request_id', 'worker_id', 'run_index', 'pid', 'producer')
            }
        return outcome

    def interval_bytes(event):
        if event.kind != 'interval' or not isinstance(event.data, dict):
            return 0
        summary = event.data.get('sum')
        count = summary.get('bytes', 0) if isinstance(summary, dict) else 0
        assert type(count) is int and count >= 0
        return count

    _execution.subprocess.Popen = record
    _execution.run_worker = observe_worker
    _execution._Responses.accept = observe_accept

    async def listener_ready(task, port):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if task.done():
                task.result()
                raise AssertionError('native server finished before listening')
            for table in ('/proc/net/tcp', '/proc/net/tcp6'):
                for line in pathlib.Path(table).read_text().splitlines()[1:]:
                    columns = line.split()
                    if columns[3] == '0A' and int(columns[1].split(':')[1], 16) == port:
                        return
            await asyncio.sleep(0.02)
        raise AssertionError('native listener did not appear')

    def assert_released(ports):
        # Do not poll: execution must already have consumed every child exit.
        assert all(process.returncode is not None for process in processes)
        assert all(process.stdin.closed and process.stdout.closed for process in processes)
        for port in ports:
            with socket.socket() as released:
                released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                released.bind((host, port))

    def producer_receipt(receipt):
        from iperf3_lib.ffi.api import POSSIBLE_NAMES, ffi, lib

        assert type(receipt['protocol_version']) is int and receipt['protocol_version'] == 1
        assert type(receipt['run_index']) is int and receipt['run_index'] == 1
        assert type(receipt['pid']) is int and receipt['pid'] > 0
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

    def overlap(first, second):
        left, right = ownership[first], ownership[second]
        start, finish = max(left['started'], right['started']), min(left['finished'], right['finished'])
        assert start < finish
        assert all(any(start <= item['at'] < finish and item['bytes'] > 0
                       for item in measured[trial]) for trial in (first, second))
        assert any(start <= sample['at'] < finish and
                   [member['trial_id'] for member in sample['members']] == [first, second]
                   for sample in liveness)
        return [first, second]

    async def main():
        loop = asyncio.get_running_loop()
        both_active = asyncio.Event()
        interrupted = case.startswith('two-active-')
        crash = case == 'two-active-worker-crash-tcp'
        count = 2 if interrupted else 3
        ports = []
        # Keep all reservations bound until every unique endpoint was selected.
        reservations = [socket.socket() for _ in range(count)]
        try:
            for reservation in reservations:
                reservation.bind((host, 0))
                ports.append(reservation.getsockname()[1])
        finally:
            for reservation in reservations:
                reservation.close()
        servers, server_tasks, server_results = [], [], []
        plan_task = None
        resources = None
        positive_pair = ('trial-0', 'trial-1') if case == 'overlap-rate-cap-tcp' else ('trial-0', 'trial-2')
        if interrupted:
            statuses = [
                'exception' if crash else ('timed_out' if protocol == 'udp' else 'cancelled'),
                'not_run', 'completed' if crash else ('timed_out' if protocol == 'udp' else 'cancelled'),
                'not_run',
            ]
            stop = 'stop_on_error' if crash else ('timeout' if protocol == 'udp' else 'cancelled')
            hard = None if crash else stop
            specs = []
            for cell in range(2):
                for phase in ('warmup', 'measured'):
                    specs.append(TrialSpec(
                        f'trial-{len(specs)}', f'cell-{cell}', phase, 0,
                        ClientConfig(host, port=ports[cell], protocol=protocol,
                                     duration=4 if crash and cell == 1 else 30,
                                     rate=500_000, interval_seconds=0.25,
                                     blksize=1200 if protocol == 'udp' else 4096),
                    ))
            policy = TrialPolicy(repetitions=1, warmup_runs=1, stop_on_error=True)
            limits = ConcurrentExecutionPolicy(max_workers=2, max_active_target_bps=1_500_000)
            server_runs = [1, 1]
        else:
            endpoint_indices = [0, 1, 2] if case == 'overlap-rate-cap-tcp' else [0, 0, 1, 2]
            specs = [TrialSpec(
                f'trial-{index}', f'cell-{index}', 'measured', 0,
                ClientConfig(host, port=ports[endpoint], duration=2, rate=500_000,
                             interval_seconds=0.25, blksize=4096),
            ) for index, endpoint in enumerate(endpoint_indices)]
            statuses, stop, hard = ['completed'] * len(specs), None, None
            policy = TrialPolicy(repetitions=1, stop_on_error=True)
            limits = ConcurrentExecutionPolicy(
                max_workers=3 if case == 'overlap-rate-cap-tcp' else 4,
                max_active_target_bps=1_000_000 if case == 'overlap-rate-cap-tcp' else 2_000_000,
            )
            if case == 'conflicting-resources-tcp':
                resources = {'cell-2': ('shared-link',), 'cell-3': ('shared-link',)}
            server_runs = [1, 1, 1] if case == 'overlap-rate-cap-tcp' else [2, 1, 1]
        plan = prepare_plan(specs, policy=policy, budget=PlanBudget(None, None))

        async def observe_execute(spec, *, timeout, on_event):
            def observe(event):
                on_event(event)
                count = interval_bytes(event)
                if count:
                    measured.setdefault(spec.trial_id, []).append({
                        'at': elapsed(), 'bytes': count,
                        'worker_event_at_seconds': event.received_at_seconds,
                    })
                    if not liveness and all(measured.get(name) for name in positive_pair):
                        members = []
                        for name in positive_pair:
                            pid = ownership[name]['pid']
                            try:
                                state = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
                            except FileNotFoundError:
                                break
                            if state in ('Z', 'X', 'x'):
                                break
                            members.append({'trial_id': name, 'pid': pid, 'state': state})
                        if len(members) == 2:
                            liveness.append({'at': elapsed(), 'members': members})
                            loop.call_soon_threadsafe(both_active.set)
            observe.trial_id = spec.trial_id
            try:
                return await real_execute(spec, timeout=timeout, on_event=observe)
            finally:
                if spec.trial_id in ownership:
                    ownership[spec.trial_id]['finished'] = elapsed()

        runner._execute = observe_execute
        try:
            for port, runs in zip(ports, server_runs):
                server = Server(config=ServerConfig(bind_address=host, port=port, interval_seconds=0.25))
                servers.append(server)
                results = []
                server_results.append(results)
                task = asyncio.create_task(asyncio.to_thread(
                    server.serve_forever, max_runs=runs, on_result=results.append, timeout=25,
                ))
                server_tasks.append(task)
                await listener_ready(task, port)
            timeout = 8 if case == 'two-active-deadline-udp' else None
            plan_task = asyncio.create_task(arun_concurrent_plan(
                plan, policy=limits, resources=resources, timeout=timeout,
            ))
            killed_pid = None
            if interrupted:
                await asyncio.wait_for(both_active.wait(), timeout=6)
                active = [next(process for process in processes if process.pid == ownership[name]['pid'])
                          for name in ('trial-0', 'trial-2')]
                assert all(process.returncode is None for process in active)
                assert set(ownership) == {'trial-0', 'trial-2'}
                if crash:
                    killed_pid = active[0].pid
                    os.kill(killed_pid, signal.SIGKILL)
                elif protocol == 'tcp':
                    plan_task.cancel('native concurrent qualification')
            try:
                execution = await asyncio.wait_for(plan_task, timeout=18)
            except (asyncio.CancelledError, TimeoutError) as error:
                assert hard in ('cancelled', 'timeout') and hasattr(error, 'partial_result')
                execution = error.partial_result
            else:
                assert hard is None
            await asyncio.wait_for(asyncio.gather(*server_tasks), timeout=8)
            assert [len(results) for results in server_results] == server_runs
            assert all(not server._run_lock.locked() for server in servers)
            assert_released(ports)
            assert [record.status for record in execution.trials] == statuses
            assert execution.stop_reason == stop and execution.termination_reason == hard
            assert execution.cleanup_confirmed is True
            assert execution.execution_success is (not interrupted)
            admitted = {record.spec.trial_id for record in execution.trials if record.status != 'not_run'}
            assert set(ownership) == admitted
            assert len(processes) == len(ports) + len(admitted)
            native_workers = {name: producer_receipt(ready[owner['pid']]) for name, owner in ownership.items()}
            retained, completed = {}, {}
            for record in execution.trials:
                if record.status == 'not_run':
                    assert record.started_at_seconds is None and record.artifact is None
                elif record.artifact is not None:
                    result = record.artifact.result
                    count = result.raw['end']['sum_received']['bytes']
                    assert type(count) is int and count > 0
                    assert result.raw['start']['test_start']['protocol'] == protocol.upper()
                    assert result.extensions['iperf3_lib.worker'] == native_workers[record.spec.trial_id]
                    assert result.extensions['iperf3_lib.rate_intent']['caller_config']['json_stream'] is False
                    assert result.execution.configuration.requested['json_stream'] is True
                    assert result.execution.configuration.effective['json_stream'].value is True
                    completed[record.spec.trial_id] = count
                else:
                    if record.status == 'exception':
                        assert crash and record.spec.trial_id == 'trial-0' and record.exception is not None
                    count = sum(interval_bytes(event) for event in record.partial_events)
                    assert count > 0
                    assert record.events_observed >= len(record.partial_events) > 0
                    assert record.events_dropped == record.events_observed - len(record.partial_events)
                    retained[record.spec.trial_id] = count
            overlaps, exclusions = [], []
            if interrupted:
                overlaps.append(overlap('trial-0', 'trial-2'))
            elif case == 'overlap-rate-cap-tcp':
                overlaps.append(overlap('trial-0', 'trial-1'))
                assert ownership['trial-2']['started'] >= min(ownership[name]['finished'] for name in ('trial-0', 'trial-1'))
            else:
                overlaps.append(overlap('trial-0', 'trial-2'))
                for first, second in (('trial-0', 'trial-1'), ('trial-2', 'trial-3')):
                    assert ownership[first]['finished'] <= ownership[second]['started']
                    exclusions.append([first, second])
            report = concurrent_report_from_execution(execution)
            encoded = dumps_concurrent_report(report)
            with tempfile.TemporaryDirectory(prefix='iperf-concurrent-report-') as directory:
                path = pathlib.Path(directory) / 'concurrent.json'
                path.write_text(encoded, encoding='utf-8')
                persisted = path.read_text(encoding='utf-8')
                restored = loads_concurrent_report(persisted)
            assert restored == report and restored.execution == execution
            assert restored.schema_version == 3
            plan.trials[0].config.server = 'changed.invalid'
            assert dumps_concurrent_report(concurrent_report_from_execution(execution)) == persisted
            before_reuse = len(processes)
            reuse = []
            for server, port in zip(servers, ports):
                task = asyncio.create_task(server.aserve_once(timeout=8))
                server_tasks.append(task)
                await listener_ready(task, port)
                result = await Client(ClientConfig(host, port=port, duration=1, rate=500_000,
                                                  protocol=protocol)).arun(timeout=8)
                server_result = await task
                assert result.ok and server_result.ok
                assert result.extensions['iperf3_lib.worker']['pid'] == processes[-1].pid
                count = result.raw['end']['sum_received']['bytes']
                assert type(count) is int and count > 0
                reuse.append({'port': port, 'bytes': count,
                              'worker': producer_receipt(result.extensions['iperf3_lib.worker'])})
                assert len(processes) == before_reuse + 2 * len(reuse)
                assert_released(ports)
            if crash:
                assert next(process for process in processes if process.pid == killed_pid).returncode == -9
            print(json.dumps({
                'case': case, 'protocol': protocol, 'trial_statuses': statuses,
                'stop_reason': stop, 'termination_reason': hard,
                'workers_reaped': True, 'pipes_closed': True, 'listeners_released': True,
                'report_roundtrip': True, 'detached': True,
                'plan_client_workers': len(admitted), 'server_workers': len(ports),
                'total_workers_before_reuse': before_reuse, 'ports': ports,
                'server_attempts': [len(results) for results in server_results],
                'ownership': ownership, 'native_workers': native_workers,
                'measurements': measured, 'liveness_snapshots': liveness,
                'overlaps': overlaps, 'exclusions': exclusions,
                'completed_bytes': completed, 'retained_bytes': retained,
                'killed_pid': killed_pid, 'killed_returncode': -9 if crash else None, 'reuse': reuse,
                'report_json': persisted, 'report_sha256': hashlib.sha256(persisted.encode()).hexdigest(),
            }))
        finally:
            if plan_task is not None and not plan_task.done():
                plan_task.cancel()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
            pending = [*server_tasks, *([plan_task] if plan_task is not None else [])]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(main())
    """
)


def test_concurrent_plan_native_scenario_compiles():
    """Keep the externally bounded native scenario parseable without libiperf."""
    compile(SCENARIO, "<native-concurrent-plan-scenario>", "exec")


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES)
def test_native_concurrent_plan_preserves_bounds_and_owned_cleanup(case, record_property):
    """Real overlapping traffic obeys admission, resource and cleanup boundaries."""
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, case],
        capture_output=True,
        text=True,
        timeout=65,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["case"] == case and receipt["workers_reaped"] is True
    assert receipt["overlaps"] and all(item["bytes"] > 0 for item in receipt["reuse"])
    record_property("concurrent_plan", json.dumps(receipt))
