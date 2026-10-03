"""Linux native parent-death evidence with a dedicated reaping supervisor."""

import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest

PARENT_PROGRAM = textwrap.dedent(
    r"""
    import asyncio
    import json
    import subprocess
    import sys

    from iperf3_lib import Client, ClientConfig, Server, ServerConfig, _execution

    case = json.loads(sys.argv[1])
    port = int(sys.argv[2])
    real_popen = subprocess.Popen
    def record(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if command == _execution._worker_command():
            print(json.dumps({'worker_pid': child.pid}), flush=True)
        return child
    _execution.subprocess.Popen = record
    def observe(event):
        if event.kind == 'interval' and isinstance(event.data, dict):
            summary = event.data.get('sum', {})
            if isinstance(summary, dict) and summary.get('bytes', 0) > 0:
                print(json.dumps({'traffic_bytes': summary['bytes']}), flush=True)
    async def main():
        if case['role'] == 'server':
            instance = Server(config=ServerConfig(
                bind_address='127.0.0.1', port=port, interval_seconds=0.25,
            ))
            await instance.aserve_once(on_event=observe)
        else:
            instance = Client(ClientConfig(
                '127.0.0.1', port=port, duration=30, rate=500_000,
                protocol=case['protocol'], interval_seconds=0.25,
            ))
            await instance.arun(on_event=observe)
    asyncio.run(main())
    """
)

SUPERVISOR_PROGRAM = textwrap.dedent(
    r"""
    import asyncio
    import ctypes
    import json
    import os
    import pathlib
    import signal
    import socket
    import subprocess
    import sys
    import time

    from iperf3_lib import Client, ClientConfig, Server, ServerConfig

    case = json.loads(sys.argv[1])
    parent_program = sys.argv[2]
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    host = '127.0.0.1'
    with socket.socket() as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]
    parent = None
    worker_pid = None
    pending = b''
    evidence = {}
    async def read_until(key):
        global pending, worker_pid
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if parent.poll() is not None:
                raise AssertionError('operation owner exited before qualification')
            try:
                pending += os.read(parent.stdout.fileno(), 65536)
            except BlockingIOError:
                pass
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                evidence.update(json.loads(line))
                worker_pid = evidence.get('worker_pid')
            if key in evidence:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f'parent did not report {key}: {evidence}')
    async def listener_ready(task=None):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if task is not None and task.done():
                task.result()
                raise AssertionError('peer server ended before listener readiness')
            for table in ('/proc/net/tcp', '/proc/net/tcp6'):
                for line in pathlib.Path(table).read_text().splitlines()[1:]:
                    columns = line.split()
                    if columns[3] == '0A' and int(columns[1].split(':')[1], 16) == port:
                        return
            await asyncio.sleep(0.02)
        raise AssertionError('listener did not appear')
    async def reap_worker():
        global worker_pid
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reaped, status = os.waitpid(worker_pid, os.WNOHANG)
            if reaped:
                worker_pid = None
                assert os.WIFSIGNALED(status)
                assert os.WTERMSIG(status) == signal.SIGKILL
                return status
            await asyncio.sleep(0.02)
        raise AssertionError('orphan worker did not exit and reap')
    def assert_listener_released():
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind((host, port))
    async def main():
        global parent, worker_pid
        server = Server(config=ServerConfig(bind_address=host, port=port))
        client = Client(ClientConfig(
            host, port=port, duration=30, rate=500_000,
            protocol=case.get('protocol', 'tcp'),
        ))
        peer = None
        if case['role'] == 'client':
            peer = asyncio.create_task(server.aserve_once(timeout=15))
            await listener_ready(peer)
        parent = subprocess.Popen(
            [sys.executable, '-u', '-c', parent_program, json.dumps(case), str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        os.set_blocking(parent.stdout.fileno(), False)
        try:
            await read_until('worker_pid')
            if case['role'] == 'server':
                await listener_ready()
                if case['active']:
                    peer = asyncio.create_task(client.arun(timeout=15))
            if case['active']:
                await read_until('traffic_bytes')
                assert evidence['traffic_bytes'] > 0
            parent.kill()
            parent.wait(timeout=5)
            assert parent.returncode == -signal.SIGKILL
            owned_worker = worker_pid
            await reap_worker()
            worker_pid = None
            if peer is not None:
                await asyncio.wait_for(peer, timeout=6)
            assert_listener_released()
            client.cfg.duration = 1
            next_server = asyncio.create_task(server.aserve_once(timeout=8))
            await listener_ready(next_server)
            client_result = await client.arun(timeout=8)
            server_result = await next_server
            assert client_result.ok, client_result.error
            assert server_result.ok, server_result.error
            assert client_result.raw['end']['sum_received']['bytes'] > 0
            assert_listener_released()
            print(json.dumps({
                'parent_pid': parent.pid,
                'worker_pid': owned_worker,
                'worker_signal': signal.SIGKILL,
                'parent_returncode': parent.returncode,
                'worker_reaped': True,
                'traffic_bytes': evidence.get('traffic_bytes', 0),
                'listener_released': True,
                'reused': True,
                'reuse_bytes': client_result.raw['end']['sum_received']['bytes'],
            }))
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                    await reap_worker()
                except (ProcessLookupError, ChildProcessError):
                    pass
            if peer is not None and not peer.done():
                peer.cancel()
                try:
                    await peer
                except (asyncio.CancelledError, Exception):
                    pass
            parent.stdout.close()
            parent.stderr.close()
    asyncio.run(main())
    """
)


def test_native_lifetime_scenario_scripts_compile():
    """Check externally bounded scenario syntax without loading libiperf."""
    for source in (PARENT_PROGRAM, SUPERVISOR_PROGRAM):
        compile(source, "<native-parent-lifetime-qualification>", "exec")


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel parent-death protection")
@pytest.mark.parametrize(
    "case",
    [
        {"role": "server", "active": False},
        {"role": "server", "active": True, "protocol": "tcp"},
        {"role": "client", "active": True, "protocol": "tcp"},
        {"role": "server", "active": True, "protocol": "udp"},
        {"role": "client", "active": True, "protocol": "udp"},
    ],
    ids=[
        "idle-server",
        "active-server-tcp",
        "active-client-tcp",
        "active-server-udp",
        "active-client-udp",
    ],
)
def test_parent_death_stops_native_worker_and_releases_listener(case, record_property):
    """Kill an operation's parent and independently reap its actual native worker."""
    with subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR_PROGRAM, json.dumps(case), PARENT_PROGRAM],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as scenario:
        try:
            output, errors = scenario.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            os.killpg(scenario.pid, signal.SIGKILL)
            scenario.communicate(timeout=5)
            raise
    assert scenario.returncode == 0, output + errors
    receipt = json.loads(output)
    assert receipt["worker_signal"] == 9
    assert receipt["parent_returncode"] == -9 and receipt["worker_reaped"] is True
    assert receipt["parent_pid"] != receipt["worker_pid"]
    assert receipt["listener_released"] is True and receipt["reused"] is True
    assert receipt["reuse_bytes"] > 0
    assert (receipt["traffic_bytes"] > 0) is case["active"]
    record_property("worker_lifetime", json.dumps(receipt))
