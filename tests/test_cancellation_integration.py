"""Real Linux libiperf cancellation, listener reuse and async configuration parity."""

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

SCENARIO = textwrap.dedent(
    r"""
    import asyncio
    import json
    import pathlib
    import socket
    import subprocess
    import sys
    import time

    from iperf3_lib import _execution
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.server_config import ServerConfig

    case = json.loads(sys.argv[1])
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

    if case.get('recorder_only'):
        # Exercise this exact recorder without libiperf. Other Python helpers
        # can be launched by platform probes or subprocess coverage hooks.
        subprocess.run(
            [sys.executable, '-c', 'pass'], check=True, timeout=5,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with subprocess.Popen(
            _execution._worker_command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        ) as worker:
            output, errors = worker.communicate('invalid JSON\n', timeout=5)
        assert worker.returncode == 0, errors
        assert json.loads(output.splitlines()[0])['class'] == 'JSONDecodeError'
        assert processes == [worker]
        assert worker.stdin.closed and worker.stdout.closed
        print(json.dumps({'owned_workers': len(processes), 'pipes_closed': True}))
        raise SystemExit(0)

    async def listener_ready(task):
        deadline = time.monotonic() + 5
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
        raise AssertionError('listener did not appear')

    def assert_released():
        assert all(process.poll() is not None for process in processes)
        assert all(process.stdin.closed and process.stdout.closed for process in processes)
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind((host, port))

    async def main():
        loop = asyncio.get_running_loop()
        traffic_observed = asyncio.Event()
        measured_bytes = []
        def observe(event):
            if event.kind == 'interval' and isinstance(event.data, dict):
                summary = event.data.get('sum', {})
                if isinstance(summary, dict) and summary.get('bytes', 0) > 0:
                    measured_bytes.append(summary['bytes'])
                    loop.call_soon_threadsafe(traffic_observed.set)
        server = Server(config=ServerConfig(
            bind_address=host, port=port, interval_seconds=0.25,
        ))
        client = Client(ClientConfig(
            host, port=port, duration=30, rate=500_000, interval_seconds=0.25,
            protocol=case.get('protocol', 'tcp'),
        ))
        role = case['role']
        server_task = asyncio.create_task(server.aserve_once(
            timeout=10, on_event=observe if role == 'server' else None,
        ))
        await listener_ready(server_task)
        target = server_task
        peer = None
        if case['active']:
            client_task = asyncio.create_task(client.arun(
                timeout=10, on_event=observe if role == 'client' else None,
            ))
            target, peer = (client_task, server_task) if role == 'client' else (server_task, client_task)
            # A start event confirms setup only; qualify cancellation after
            # this endpoint has reported actual TCP/UDP measurement bytes.
            await asyncio.wait_for(traffic_observed.wait(), timeout=5)
            assert measured_bytes[0] > 0
        target.cancel('native cancellation qualification')
        try:
            await asyncio.wait_for(target, timeout=6)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('cancelled native run returned a result')
        if peer is not None:
            # Native peer interruption is a measured failed/incomplete result.
            # Waiting for it avoids racing the next run against remote cleanup.
            await asyncio.wait_for(peer, timeout=6)
        assert not server._run_lock.locked()
        assert_released()
        cancelled_children = len(processes)
        client.cfg.duration = 1
        next_server = asyncio.create_task(server.aserve_once(timeout=8))
        await listener_ready(next_server)
        client_result = await client.arun(timeout=8)
        server_result = await next_server
        assert client_result.ok, client_result.error
        assert server_result.ok, server_result.error
        assert client_result.raw['end']['sum_received']['bytes'] > 0
        assert_released()
        print(json.dumps({
            'cancelled_children': cancelled_children,
            'reused': True,
            'measured_bytes_before_cancel': measured_bytes[0] if measured_bytes else 0,
        }))

    try:
        asyncio.run(main())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    """
)


def test_cancellation_native_scenario_script_compiles():
    """Keep the externally bounded qualification program syntactically checked."""
    compile(SCENARIO, "<native-cancellation-scenario>", "exec")


def test_cancellation_scenario_tracks_only_owned_worker_processes():
    """Unrelated real subprocesses must not enter worker counts or pipe assertions."""
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, json.dumps({"recorder_only": True})],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"owned_workers": 1, "pipes_closed": True}


@pytest.mark.integration
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
def test_native_cancellation_reaps_worker_releases_listener_and_allows_reuse(case, record_property):
    """Cancellation ends owned native work and permits a measured subsequent run."""
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, json.dumps(case)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    record_property("cancellation", json.dumps(receipt))
    assert receipt["cancelled_children"] == (2 if case["active"] else 1)
    assert receipt["reused"] is True
    if case["active"]:
        assert receipt["measured_bytes_before_cancel"] > 0
    else:
        assert receipt["measured_bytes_before_cancel"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["tcp", "udp", "sctp"])
@pytest.mark.parametrize("mode", ["forward", "reverse", "bidirectional"])
async def test_arun_legacy_configuration_matches_native_evidence(iperf3_server, protocol, mode):
    """The always-isolated async path applies legacy options on both native endpoints."""
    from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    host, port = iperf3_server
    cfg = ClientConfig(
        host,
        port=port,
        duration=1,
        protocol=protocol,
        rate=500_000,
        parallel=2,
        blksize=1200 if protocol == "udp" else 4096,
        reverse=mode == "reverse",
        bidirectional=mode == "bidirectional",
        tos=0 if protocol == "sctp" else 16,
        omit=1,
    )
    # Omit the method's timeout deliberately: default arun must choose the worker.
    result = await asyncio.wait_for(Client(cfg).arun(), timeout=15)
    assert result.ok, result.error
    native = result.raw["start"]["test_start"]
    assert native["protocol"] == protocol.upper()
    assert native["num_streams"] == 2
    assert native["target_bitrate"] == 500_000
    assert native["blksize"] == cfg.blksize
    assert native["reverse"] == (mode == "reverse")
    assert native["bidir"] == (mode == "bidirectional")
    assert native["omit"] == 1
    assert result.raw["end"]["sum_received"]["bytes"] > 0
    evidence = result.extensions["iperf3_lib.native_configuration"]
    assert evidence["tos"]["value"] == cfg.tos
    assert loads_artifact(dumps_artifact(artifact_from_result(result))).result == result


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["tcp", "udp", "sctp"])
async def test_arun_protocol_default_block_size_has_native_evidence(iperf3_server, protocol):
    """Default async worker configuration preserves established protocol block sizes."""
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    host, port = iperf3_server
    config = ClientConfig(host, port=port, duration=1, protocol=protocol, rate=500_000)
    result = await asyncio.wait_for(Client(config).arun(), timeout=15)
    assert result.ok, result.error
    native = result.raw["start"]["test_start"]
    assert native["protocol"] == protocol.upper()
    assert native["duration"] == 1
    if protocol == "udp":
        assert 16 <= native["blksize"] <= 65507
    else:
        assert native["blksize"] == (64 * 1024 if protocol == "sctp" else 128 * 1024)
    evidence = result.extensions["iperf3_lib.native_configuration"]
    assert evidence["blksize"]["value"] == native["blksize"]
    assert result.raw["end"]["sum_received"]["bytes"] > 0
