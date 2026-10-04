"""Repeated installed native lifecycles with measured Linux resource inventories."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CASES = ("normal-tcp", "normal-udp", "cancel-deadline-tcp", "crash-transport-udp", "parent-death")

COMMON = textwrap.dedent(
    r"""
    import asyncio
    import ctypes
    import gc
    import json
    import os
    import pathlib
    import signal
    import socket
    import subprocess
    import sys
    import threading
    import time

    sys.path.insert(0, sys.argv[2])
    import _native_resource_inventory as inventory
    from iperf3_lib import Client, ClientConfig, Server, ServerConfig, _execution
    from iperf3_lib.exceptions import IperfLibraryError

    processes = []
    workers = {}
    owner = threading.local()
    real_popen, real_worker, real_accept = subprocess.Popen, _execution.run_worker, _execution._Responses.accept
    def notify(record):
        pass
    def observe_worker(role, options, **kwargs):
        owner.role = role
        try:
            return real_worker(role, options, **kwargs)
        finally:
            owner.role = None
    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if (args[0] if args else kwargs.get('args')) == _execution._worker_command():
            processes.append(process)
            workers[process.pid] = {'pid': process.pid, 'role': owner.role, 'ready': None,
                                    'snapshots': [], 'traffic_bytes': 0}
        return process
    def positive_bytes(kind, data):
        if kind != 'interval' or not isinstance(data, dict):
            return 0
        summary = data.get('sum')
        count = summary.get('bytes', 0) if isinstance(summary, dict) else 0
        assert type(count) is int and count >= 0
        return count
    def observe_accept(responses, message):
        outcome = real_accept(responses, message)
        record = workers[responses.pid]
        phase = None
        if message['type'] == 'ready':
            record['ready'] = {key: message[key] for key in
                              ('protocol_version', 'request_id', 'worker_id', 'run_index', 'pid', 'producer')}
            phase = 'ready'
        elif message['type'] == 'event':
            count = positive_bytes(message['kind'], message['data'])
            if count and not record['traffic_bytes']:
                phase = 'active'
            record['traffic_bytes'] += count
        if phase is not None:
            snapshot = inventory.snapshot_process(responses.pid)
            assert snapshot['children'] == []
            assert all(children == [] for children in snapshot['thread_children'].values())
            record['snapshots'].append({'phase': phase, 'inventory': snapshot})
            notify(record)
        return outcome
    _execution.run_worker = observe_worker
    _execution.subprocess.Popen = popen
    _execution._Responses.accept = observe_accept

    def reserve_port():
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            return reservation.getsockname()[1]
    async def until(predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, message
            await asyncio.sleep(0.02)
    def listening(port):
        for table in ('/proc/net/tcp', '/proc/net/tcp6'):
            for line in pathlib.Path(table).read_text().splitlines()[1:]:
                columns = line.split()
                if columns[3] == '0A' and int(columns[1].split(':')[1], 16) == port:
                    return True
        return False
    async def listener_ready(task, port):
        def ready():
            if task.done():
                task.result()
                raise AssertionError('native server ended before listener readiness')
            return listening(port)
        await until(ready, 'native listener did not appear')
    def released(port):
        assert not listening(port)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
    def assert_library_reaped():
        # Calling poll/wait here could hide a library reaping failure.
        assert all(process.returncode is not None for process in processes)
        assert all(process.stdin.closed and process.stdout.closed for process in processes)
        for record in workers.values():
            identity = record['snapshots'][0]['inventory']['identity']
            assert not inventory.process_identity_matches(record['pid'], identity['start_ticks'])
            record['returncode'] = next(process.returncode for process in processes if process.pid == record['pid'])
            record['reaping_owner'] = 'library'
            record['identity_absent_after'] = True
    async def restored_inventory(baseline):
        gc.collect()
        result = None
        def restored():
            nonlocal result
            result = inventory.snapshot_process(os.getpid())
            return result['fds'] == baseline['fds'] and result['children'] == baseline['children']
        await until(restored, 'process or descriptor inventory did not return to baseline', timeout=3)
        return result
    async def measured_reuse(server, port, protocol):
        task = asyncio.create_task(server.aserve_once(timeout=8, on_event=lambda event: None))
        await listener_ready(task, port)
        result = await Client(ClientConfig('127.0.0.1', port=port, protocol=protocol,
                                          duration=1, rate=500_000, interval_seconds=0.25)).arun(
                                              timeout=8, on_event=lambda event: None)
        peer = await task
        assert result.ok and peer.ok
        count = result.raw['end']['sum_received']['bytes']
        assert type(count) is int and count > 0
        assert result.extensions['iperf3_lib.worker'] == workers[processes[-1].pid]['ready']
        assert_library_reaped()
        released(port)
        return {'bytes': count, 'worker': result.extensions['iperf3_lib.worker']}
    def emergency_cleanup():
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        # This isolated scenario is a subreaper. Only after assertions have
        # finished may failed-test cleanup collect its own adopted descendants.
        try:
            adopted = inventory.snapshot_process(os.getpid())['children']
        except inventory.InventoryRaceError:
            return
        for child in adopted:
            if child['ppid'] == os.getpid() and inventory.process_identity_matches(child['pid'], child['start_ticks']):
                try:
                    os.kill(child['pid'], signal.SIGKILL)
                    os.waitpid(child['pid'], 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
    """
)

PARENT = COMMON + textwrap.dedent(
    r"""
    job = json.loads(sys.argv[1])
    def notify(record):
        print(json.dumps({'worker': record}), flush=True)
    async def main():
        if job['role'] == 'server':
            await Server(config=ServerConfig(bind_address='127.0.0.1', port=job['port'],
                                             interval_seconds=0.25)).aserve_once(on_event=lambda event: None)
        else:
            await Client(ClientConfig('127.0.0.1', port=job['port'], protocol=job['protocol'],
                                      duration=30, rate=500_000, interval_seconds=0.25)).arun(on_event=lambda event: None)
    asyncio.run(main())
    """
)

SCENARIO = COMMON + textwrap.dedent(
    r"""
    case = sys.argv[1]
    parent_program = sys.argv[3]
    class ConsumerStopped(RuntimeError):
        pass

    async def normal_cycle(index, baseline):
        protocol = 'udp' if case.endswith('udp') else 'tcp'
        mode = {
            'normal-tcp': ('complete', 'consumer-error', 'complete', 'consumer-error'),
            'normal-udp': ('complete',) * 4,
            'cancel-deadline-tcp': ('cancel', 'deadline', 'cancel', 'deadline'),
            'crash-transport-udp': ('crash', 'transport', 'crash', 'transport'),
        }[case][index]
        port = reserve_port()
        server = Server(config=ServerConfig(bind_address='127.0.0.1', port=port, interval_seconds=0.25))
        peer = asyncio.create_task(server.aserve_once(timeout=12, on_event=lambda event: None))
        await listener_ready(peer, port)
        loop = asyncio.get_running_loop()
        active = asyncio.Event()
        delivered = []
        def observe(event):
            count = positive_bytes(event.kind, event.data)
            if count:
                delivered.append(count)
                loop.call_soon_threadsafe(active.set)
                if mode == 'consumer-error':
                    raise ConsumerStopped('consumer stopped after positive native traffic')
        client = asyncio.create_task(Client(ClientConfig(
            '127.0.0.1', port=port, protocol=protocol,
            duration=1 if mode in ('complete', 'consumer-error') else 30,
            rate=500_000, interval_seconds=0.25, blksize=1200 if protocol == 'udp' else 4096,
        )).arun(timeout=3 if mode == 'deadline' else 10, on_event=observe))
        try:
            await asyncio.wait_for(active.wait(), timeout=6)
            await until(lambda: len(workers) == 2 and all(
                            [sample['phase'] for sample in record['snapshots']] == ['ready', 'active']
                            for record in workers.values()),
                        'both native endpoints did not report positive traffic')
            target = next(process for process in processes if workers[process.pid]['role'] == 'client')
            if mode == 'cancel':
                client.cancel('native resource stress')
            elif mode == 'crash':
                os.kill(target.pid, signal.SIGKILL)
            elif mode == 'transport':
                await asyncio.wait_for(asyncio.to_thread(target.stdout.close), timeout=3)
            result_bytes = 0
            try:
                result = await client
            except asyncio.CancelledError:
                assert mode == 'cancel'
                outcome = 'cancelled'
            except TimeoutError:
                assert mode == 'deadline'
                outcome = 'timeout'
            except ConsumerStopped:
                assert mode == 'consumer-error' and len(delivered) == 1
                outcome = 'consumer_error'
            except IperfLibraryError:
                assert mode in ('crash', 'transport')
                outcome = 'worker_error'
            else:
                assert mode == 'complete' and result.ok
                result_bytes = result.raw['end']['sum_received']['bytes']
                assert type(result_bytes) is int and result_bytes > 0
                assert result.extensions['iperf3_lib.worker'] == workers[target.pid]['ready']
                outcome = 'completed'
            await asyncio.wait_for(peer, timeout=8)
            assert_library_reaped()
            if mode == 'crash':
                assert target.returncode == -9
            released(port)
            after_operation = await restored_inventory(baseline)
            reuse = await measured_reuse(server, port, protocol)
            after_reuse = await restored_inventory(baseline)
            assert len(workers) == 4
            return {'index': index, 'mode': mode, 'protocol': protocol, 'port': port,
                    'outcome': outcome, 'target_pid': target.pid,
                    'delivered_bytes': sum(delivered), 'delivered_intervals': len(delivered),
                    'result_bytes': result_bytes, 'workers': list(workers.values()),
                    'after_operation': after_operation, 'after_reuse': after_reuse, 'reuse': reuse}
        finally:
            for task in (client, peer):
                if not task.done():
                    task.cancel()
            emergency_cleanup()
            await asyncio.gather(client, peer, return_exceptions=True)

    async def parent_cycle(index, baseline):
        role, protocol = (('client', 'tcp'), ('server', 'tcp'), ('client', 'udp'), ('server', 'udp'))[index]
        port = reserve_port()
        server = Server(config=ServerConfig(bind_address='127.0.0.1', port=port, interval_seconds=0.25))
        peer = None
        if role == 'client':
            peer = asyncio.create_task(server.aserve_once(timeout=12, on_event=lambda event: None))
            await listener_ready(peer, port)
        job = {'role': role, 'protocol': protocol, 'port': port}
        parent = real_popen([sys.executable, '-u', '-c', parent_program, json.dumps(job), sys.argv[2]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.set_blocking(parent.stdout.fileno(), False)
        pending = b''
        remote = None
        reaped = False
        async def read_worker(active):
            nonlocal pending, remote
            def received():
                nonlocal pending, remote
                assert parent.poll() is None, 'operation parent exited before forced death'
                try:
                    pending += os.read(parent.stdout.fileno(), 65536)
                except BlockingIOError:
                    pass
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    remote = json.loads(line)['worker']
                return remote is not None and (remote['traffic_bytes'] > 0 if active else remote['ready'] is not None)
            await until(received, 'operation parent did not report native activity')
        try:
            await read_worker(False)
            if role == 'server':
                await until(lambda: listening(port), 'child-owned native server did not listen')
                peer = asyncio.create_task(Client(ClientConfig('127.0.0.1', port=port, protocol=protocol,
                                        duration=30, rate=500_000, interval_seconds=0.25)).arun(
                                            timeout=12, on_event=lambda event: None))
            await read_worker(True)
            await until(lambda: len(workers) == 1 and all(
                            [sample['phase'] for sample in record['snapshots']] == ['ready', 'active']
                            for record in workers.values()),
                        'native peer did not report positive traffic')
            target_pid = remote['pid']
            parent_identity = inventory.process_identity(parent.pid)
            parent.kill()
            parent.wait(timeout=5)
            assert parent.returncode == -9
            wait_status = None
            def collect_orphan():
                nonlocal wait_status
                pid, status = os.waitpid(target_pid, os.WNOHANG)
                if pid:
                    assert pid == target_pid and os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                    wait_status = status
                    return True
                return False
            await until(collect_orphan, 'parent-death native worker was not reaped')
            reaped = True
            assert not inventory.process_identity_matches(target_pid, remote['snapshots'][0]['inventory']['identity']['start_ticks'])
            remote.update(returncode=-9, reaping_owner='subreaper', identity_absent_after=True)
            await asyncio.wait_for(peer, timeout=8)
            assert_library_reaped()
            parent.stdout.close()
            parent.stderr.close()
            released(port)
            after_operation = await restored_inventory(baseline)
            reuse = await measured_reuse(server, port, protocol)
            after_reuse = await restored_inventory(baseline)
            assert len(workers) == 3
            return {'index': index, 'mode': 'parent-death', 'protocol': protocol, 'port': port,
                    'outcome': 'parent_killed', 'target_pid': target_pid,
                    'parent_identity': parent_identity, 'parent_returncode': parent.returncode,
                    'adopted_wait_status': wait_status, 'workers': [remote, *workers.values()],
                    'after_operation': after_operation, 'after_reuse': after_reuse, 'reuse': reuse}
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if remote is not None and not reaped:
                pid = remote['pid']
                try:
                    birth = remote['snapshots'][0]['inventory']['identity']['start_ticks']
                    if (inventory.process_identity_matches(pid, birth)
                            and inventory.process_identity(pid)['ppid'] == os.getpid()):
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
            parent.stdout.close()
            parent.stderr.close()
            if peer is not None:
                if not peer.done():
                    peer.cancel()
                emergency_cleanup()
                await asyncio.gather(peer, return_exceptions=True)

    async def main():
        # Adopt unexpected surviving descendants in every mode. Procfs
        # samples alone cannot observe a descendant after its parent exits.
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(36, 1, 0, 0, 0) == 0
        # Create executor threads and initialize imports before measuring a
        # stable parent inventory; native operations must return to this state.
        await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.02) for _ in range(3)))
        baseline = inventory.snapshot_process(os.getpid())
        assert baseline['children'] == []
        cycles = []
        for index in range(4):
            processes.clear()
            workers.clear()
            cycle = await (parent_cycle(index, baseline) if case == 'parent-death' else normal_cycle(index, baseline))
            cycles.append(cycle)
        final = await restored_inventory(baseline)
        print(json.dumps({'case': case, 'cycles': cycles, 'baseline': baseline, 'final': final}))
    try:
        asyncio.run(main())
    finally:
        emergency_cleanup()
    """
)


def test_resource_stress_native_scenarios_compile():
    """Check both subprocess programs without requiring native Linux execution."""
    for source in (PARENT, SCENARIO):
        compile(source, "<native-resource-stress>", "exec")


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc resource accounting")
@pytest.mark.parametrize("case", CASES)
def test_repeated_native_lifecycles_restore_owned_resources(case, record_property):
    """Every cycle retains measured process, descriptor, descendant and reuse evidence."""
    scenario = subprocess.Popen(
        [sys.executable, "-c", SCENARIO, case, str(Path(__file__).resolve().parent), PARENT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = scenario.communicate(timeout=150)
    finally:
        if scenario.poll() is None:
            os.killpg(scenario.pid, signal.SIGKILL)
        scenario.wait(timeout=5)
        scenario.stdout.close()
        scenario.stderr.close()
    assert scenario.returncode == 0, stdout + stderr
    receipt = json.loads(stdout)
    assert receipt["case"] == case and len(receipt["cycles"]) == 4
    record_property("resource_stress", json.dumps(receipt))
