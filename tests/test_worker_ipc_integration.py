"""Standalone installed-package qualification using real worker pipes and processes."""

import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

from iperf3_lib import _execution, _ipc
from iperf3_lib._cancellation import _run_async
from iperf3_lib.exceptions import IperfLibraryError

CASES = (
    "wrong-identity",
    "oversized-header",
    "truncated-frame",
    "saturated-events",
    "partial-frame-cancel",
)

CHILD = textwrap.dedent(
    r"""
    import importlib.metadata
    import io
    import os
    import pathlib
    import platform
    import struct
    import sys
    import time

    from iperf3_lib import _ipc

    case, marker = sys.argv[1:]
    request = _ipc.read_frame(sys.stdin.buffer)
    session = _ipc.Session.from_request(request)
    session.validate(request)

    def write(data):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def send(message):
        write(session.emit(message))

    send({
        'type': 'ready', 'pid': os.getpid(), 'producer': {
            'package_version': importlib.metadata.version('iperf3-lib'),
            'python_version': platform.python_version(),
            'native_version': 'simulated native transport producer',
            'library_selector': {'source': 'transport qualification fixture'},
        },
    })
    result = {
        'type': 'result',
        'raw': {
            'start': {'test_start': {'protocol': 'TCP'}},
            'end': {'sum_sent': {'bytes': 10, 'seconds': 1, 'bits_per_second': 80}},
        },
        'error': None, 'evidence': {},
        'native_version': 'simulated native transport producer',
        'started_at': 10, 'completed_at': 11, 'elapsed': 1,
    }
    if case == 'wrong-identity':
        message = _ipc.read_frame(io.BytesIO(session.emit(result)))
        first = '1' if message['worker_id'][0] == '0' else '0'
        message['worker_id'] = first + message['worker_id'][1:]
        write(_ipc.encode_frame(message))
    elif case == 'oversized-header':
        write(struct.pack('>I', _ipc.MAX_FRAME_BYTES + 1))
        time.sleep(30)
    elif case == 'truncated-frame':
        frame = session.emit(result)
        write(frame[:-1])
    elif case == 'partial-frame-cancel':
        write(struct.pack('>I', 12345) + b'{"type":')
        time.sleep(30)
    else:
        send({'type': 'event', 'kind': 'interval', 'data': {}, 'sequence': 1, 'time': 10})
        deadline = time.monotonic() + 10
        while not pathlib.Path(marker).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Parent callback did not enter')
            time.sleep(0.01)
        # With the first callback blocked, more than 8 MiB must traverse the
        # actual pipe and queue. Each event remains below its 1 MiB frame cap.
        for sequence in range(2, 34):
            send({
                'type': 'event', 'kind': 'interval',
                'data': {'padding': 'x' * (512 * 1024)},
                'sequence': sequence, 'time': 10,
            })
        result['events_emitted'] = 33
        send(result)
        send({'type': 'terminal', 'status': 'completed', 'runs': 1})
    """
)


@pytest.mark.parametrize("case", CASES)
def test_installed_worker_transport_contract(case, monkeypatch, record_property):
    """Faults and saturation preserve owned cleanup across installed runtimes."""
    processes = []
    real_popen = subprocess.Popen
    terminal_seen = threading.Event()
    partial_seen = threading.Event()
    receipt = {"case": case}
    with tempfile.TemporaryDirectory(prefix="iperf-worker-ipc-") as directory:
        marker = Path(directory) / "callback-entered"

        def popen(args, **kwargs):
            assert args == _execution._worker_command()
            # Windows venv redirectors spawn another PID. The fake worker must
            # be the owned process, while PYTHONPATH preserves package lookup.
            interpreter = sys._base_executable if sys.platform == "win32" else sys.executable
            process = real_popen([interpreter, "-u", "-c", CHILD, case, str(marker)], **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(_execution.subprocess, "Popen", popen)
        original_put = _execution._Inbox.put

        def observe_admission(inbox, message, size):
            original_put(inbox, message, size)
            if message["type"] == "terminal":
                terminal_seen.set()

        monkeypatch.setattr(_execution._Inbox, "put", observe_admission)
        original_feed = _ipc.FrameDecoder.feed_sized

        def observe_partial(decoder, data):
            messages = original_feed(decoder, data)
            if decoder._length == 12345 and decoder._body == b'{"type":':
                partial_seen.set()
            return messages

        monkeypatch.setattr(_ipc.FrameDecoder, "feed_sized", observe_partial)

        def on_event(event):
            if event.sequence == 1:
                assert not terminal_seen.is_set()
                marker.touch()
                assert terminal_seen.wait(10), "Child did not complete while callback was blocked"

        async def cancel_partial():
            task = asyncio.create_task(
                _run_async(lambda control: _execution.run_worker("client", {}, _control=control))
            )
            try:
                assert await asyncio.to_thread(partial_seen.wait, 10), "Partial frame was not read"
                task.cancel()
                completed, _ = await asyncio.wait((task,), timeout=5)
                assert task in completed, "Cancellation did not finish owned cleanup"
                with pytest.raises(asyncio.CancelledError):
                    task.result()
            finally:
                if not task.done():
                    # The production coroutine intentionally waits for its
                    # executor owner when cancelled. Kill a failed fixture
                    # before asyncio.run waits for that executor at shutdown.
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                            await asyncio.to_thread(process.wait, timeout=5)
                    task.cancel()
                    await asyncio.wait((task,), timeout=5)
                if task.done() and not task.cancelled():
                    task.exception()

        try:
            if case == "partial-frame-cancel":
                asyncio.run(cancel_partial())
                receipt["cancelled"] = True
            elif case == "saturated-events":
                result = _execution.run_worker("client", {}, on_event=on_event, timeout=20)
                assert result.ok
                delivery = result.extensions["iperf3_lib.event_delivery"]
                assert delivery["emitted"] == 33
                assert type(delivery["dropped"]) is int and delivery["dropped"] > 0
                receipt["dropped"] = delivery["dropped"]
                receipt["event_payload_bytes"] = 32 * 512 * 1024
            else:
                expected = {
                    "wrong-identity": "another session",
                    "oversized-header": "invalid byte length",
                    "truncated-frame": "truncated frame",
                }[case]
                with pytest.raises(IperfLibraryError, match=expected):
                    _execution.run_worker("client", {}, timeout=10)
                receipt["rejected"] = True
            assert len(processes) == 1
            process = processes[0]
            # The library must already have reaped before this test inspects
            # the process; calling poll first could reap on the test's behalf.
            assert process.returncode is not None
            assert process.stdin.closed and process.stdout.closed
            receipt.update(workers_reaped=True, pipes_closed=True)
            record_property("worker_ipc", json.dumps(receipt))
        finally:
            # Keep failed qualification bounded without mistaking this fallback
            # for evidence that library cleanup succeeded.
            terminal_seen.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
