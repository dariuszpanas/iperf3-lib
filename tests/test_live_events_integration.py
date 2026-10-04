"""Native typed event qualification with both endpoints and measured worker reuse."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

CASES = tuple(
    f"{protocol}-{direction}"
    for protocol in ("tcp", "udp", "sctp")
    for direction in ("forward", "reverse", "bidir")
) + ("native-error", "close-client", "close-server")

SCENARIO = textwrap.dedent(
    r"""
    import asyncio
    import errno
    import importlib.metadata
    import json
    import math
    import pathlib
    import platform
    import socket
    import subprocess
    import sys
    import time
    from dataclasses import asdict

    from iperf3_lib import _execution
    from iperf3_lib.artifacts import artifact_from_result, dumps_artifact
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.ffi.api import ffi, lib
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.server_config import ServerConfig

    case, diagnostic = sys.argv[1:]
    mode = 'close' if case.startswith('close-') else 'native-error' if case == 'native-error' else 'matrix'
    protocol, direction = case.split('-') if mode == 'matrix' else ('tcp', 'forward')
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
    readiness = {}
    real_accept = _execution._Responses.accept
    def accept(responses, message):
        outcome = real_accept(responses, message)
        if message['type'] == 'ready':
            readiness[responses.pid] = {key: message[key] for key in (
                'protocol_version', 'request_id', 'worker_id', 'run_index', 'pid', 'producer',
            )}
        return outcome
    _execution._Responses.accept = accept
    receipt = {
        'schema_version': 1, 'case': case, 'mode': mode, 'protocol': protocol,
        'direction': direction, 'port': port,
        'producer': {
            'package_version': importlib.metadata.version('iperf3-lib'),
            'python_version': platform.python_version(),
            'native_version': ffi.string(lib.iperf_get_iperf_version()).decode(),
        },
        'support': None, 'endpoints': {}, 'cleanup': None, 'reuse': None,
    }

    def save():
        pathlib.Path(diagnostic).write_text(json.dumps(receipt, allow_nan=False))

    def artifact(result):
        return dumps_artifact(artifact_from_result(result))

    def cleanup(server, owned):
        workers = [{
            # Inspect the return code consumed by the library before any
            # test-side poll/wait could perform otherwise missing reaping.
            'pid': process.pid, 'returncode': process.returncode,
            'stdin_closed': process.stdin.closed, 'stdout_closed': process.stdout.closed,
            'ready': readiness.get(process.pid),
        } for process in owned]
        assert all(item['returncode'] is not None for item in workers), workers
        assert all(item['stdin_closed'] and item['stdout_closed'] for item in workers), workers
        assert not server._run_lock.locked()
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind((host, port))
        return {'workers': workers, 'listener_released': True, 'server_lock_released': True}

    async def listener_ready(owner):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if owner.done():
                owner.result()
                raise AssertionError('server stopped before listen')
            for table in ('/proc/net/tcp', '/proc/net/tcp6'):
                for line in pathlib.Path(table).read_text().splitlines()[1:]:
                    columns = line.split()
                    if columns[3] == '0A' and int(columns[1].split(':')[1], 16) == port:
                        return
            await asyncio.sleep(0.01)
        raise AssertionError('owned listener did not appear')

    def check_envelopes(events, result=None):
        assert events and events[0]['kind'] == 'worker_state'
        assert events[-1]['kind'] == 'terminal'
        assert sum(event['kind'] == 'terminal' for event in events) == 1
        assert [event['delivery_sequence'] for event in events] == list(range(1, len(events)+1))
        assert events[-1]['payload']['cleanup_confirmed'] is True
        native = [event for event in events if event['capture_sequence'] is not None]
        sequences = [event['capture_sequence'] for event in native]
        assert sequences == sorted(set(sequences)), sequences
        offsets = [event['arrival_offset_seconds'] for event in native]
        assert offsets == sorted(offsets) and all(math.isfinite(v) and v >= 0 for v in offsets)
        assert all(math.isfinite(event['received_at_seconds']) for event in native)
        assert all(event['kind'] not in ('malformed', 'delivery_gap') for event in events)
        if result is not None:
            worker = result.extensions['iperf3_lib.worker']
            for event in events:
                assert event['request_id'] == worker['request_id']
                assert event['worker_id'] == worker['worker_id']
                assert event['run_index'] in (None, worker['run_index'])
            assert events[-1]['payload']['native_status'] == result.execution.status
            assert events[-1]['payload']['result_available'] is True

    def check_measurements(events, result):
        assert result.ok, result.error
        assert result.protocol == protocol
        assert result.execution.method == ('bidirectional' if direction == 'bidir' else direction)
        native_events = [event for event in events if event['kind'] == 'interval']
        live = [measurement for event in native_events for measurement in event['payload']['measurements']]
        canonical = [asdict(interval) for interval in result.intervals]
        assert live and any((item['bytes'] or 0) > 0 for item in live), events
        def measured(value):
            # Projection normalizes each native fragment independently, so
            # pointer indices are relative to that fragment rather than the
            # final document. Compare all measured values and associations.
            if isinstance(value, dict):
                return {key: measured(item) for key, item in value.items() if key != 'evidence_paths'}
            if isinstance(value, list):
                return [measured(item) for item in value]
            return value
        fields = tuple(canonical[0])
        assert all(any(all(measured(measurement[key]) == measured(interval[key]) for key in fields) for interval in canonical) for measurement in live), (live, canonical)
        assert all(item['direction'] != 'unknown' and item['observation'] is not None for item in live)
        assert any((flow.sender and (flow.sender.bytes or 0) > 0) or (flow.receiver and (flow.receiver.bytes or 0) > 0) for flow in result.flows)
        assert any(event['kind'] in ('native_end', 'native_document') for event in events)

    async def collect(stream, role, *, close=False):
        endpoint = {'events': [], 'artifact_json': None, 'error_type': None}
        receipt['endpoints'][role] = endpoint
        did_close = False
        async for event in stream:
            item = asdict(event)
            endpoint['events'].append(item)
            if close and not did_close and event.kind == 'interval' and any(
                (measurement.bytes or 0) > 0 for measurement in event.payload.measurements
            ):
                did_close = True
                await stream.aclose()
        result = None
        try:
            result = await stream.result()
            endpoint['artifact_json'] = artifact(result)
        except asyncio.CancelledError:
            endpoint['error_type'] = 'CancelledError'
            assert close and did_close
        save()
        check_envelopes(endpoint['events'], result)
        if close:
            assert did_close and endpoint['events'][-1]['payload']['outcome'] == 'cancelled'
        return result

    async def reuse(server):
        start = len(processes)
        server_task = asyncio.create_task(server.aserve_once(timeout=10))
        await listener_ready(server_task)
        result = await Client(ClientConfig(
            host, port=port, duration=1, rate=500_000, blksize=1200,
        )).arun(timeout=10)
        server_result = await server_task
        assert result.ok and server_result.ok, (result.error, server_result.error)
        assert result.raw['end']['sum_received']['bytes'] > 0
        return {
            'client_artifact_json': artifact(result),
            'server_artifact_json': artifact(server_result),
            'cleanup': cleanup(server, processes[start:]),
        }

    async def main():
        server = Server(config=ServerConfig(bind_address=host, port=port, interval_seconds=0.25))
        unsupported = False
        if protocol == 'sctp':
            support = {
                'status': 'supported', 'family': int(socket.AF_INET),
                'type': int(socket.SOCK_STREAM), 'protocol': 132,
                'errno': None, 'error': None, 'kernel_release': platform.release(),
            }
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132)
                probe.close()
            except OSError as error:
                assert error.errno in (errno.EPROTONOSUPPORT, errno.EAFNOSUPPORT, errno.ESOCKTNOSUPPORT), error
                unsupported = True
                support.update(status='kernel_unsupported', errno=error.errno, error=str(error))
            receipt['support'] = support
        client = Client(ClientConfig(
            host, port=port, protocol=protocol, duration=30 if mode == 'close' else 1,
            rate=500_000, parallel=2, blksize=1200, interval_seconds=0.25,
            reverse=direction == 'reverse', bidirectional=direction == 'bidir',
        ))
        if not unsupported:
            if mode == 'native-error':
                async with client.events(timeout=10) as stream:
                    result = await collect(stream, 'client')
                    assert result is not None and not result.ok and result.error
                    assert any(event['kind'] == 'native_error' for event in receipt['endpoints']['client']['events'])
            else:
                async with server.events_once(timeout=15) as server_stream:
                    await listener_ready(server_stream._owner)
                    async with client.events(timeout=15) as client_stream:
                        results = await asyncio.gather(
                            collect(client_stream, 'client', close=case == 'close-client'),
                            collect(server_stream, 'server', close=case == 'close-server'),
                        )
                        if mode == 'matrix':
                            for role, result in zip(('client', 'server'), results, strict=True):
                                check_measurements(receipt['endpoints'][role]['events'], result)
        receipt['cleanup'] = cleanup(server, processes[:])
        receipt['reuse'] = await reuse(server)
        save()
        assert len(json.dumps(receipt)) < 1_000_000
        print(json.dumps(receipt, allow_nan=False))

    try:
        asyncio.run(main())
    finally:
        save()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    """
)


def test_live_events_native_scenario_compiles():
    """Parse the bounded subprocess program without importing native code."""
    compile(SCENARIO, "<native-live-events-scenario>", "exec")


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES)
def test_live_events_roundtrip(case, record_property, tmp_path):
    """Compare actual typed observations, full results, owned cleanup and reuse."""
    diagnostic = tmp_path / "live-events-failure.json"
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, case, str(diagnostic)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if completed.returncode != 0 and diagnostic.is_file():
        record_property("live_events_failure", diagnostic.read_text())
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["case"] == case
    record_property("live_events", json.dumps(receipt, allow_nan=False))


def test_live_events_native_failure_retains_partial_evidence(monkeypatch, tmp_path):
    """CI keeps partial live observations even when a native assertion fails."""
    evidence = {"case": "tcp-forward", "endpoints": {"client": {"events": []}}}

    def fail(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(evidence))
        return SimpleNamespace(returncode=1, stdout="", stderr="native failure")

    monkeypatch.setattr(subprocess, "run", fail)
    properties = []
    with pytest.raises(AssertionError, match="native failure"):
        test_live_events_roundtrip("tcp-forward", lambda *args: properties.append(args), tmp_path)
    assert properties == [("live_events_failure", json.dumps(evidence))]
