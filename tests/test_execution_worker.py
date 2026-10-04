"""Process lifecycle, bounded event delivery and native ownership regression tests."""

import asyncio
import concurrent.futures
import io
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from iperf3_lib import _execution, _ipc, _worker
from iperf3_lib.exceptions import IperfLibraryError, UnsupportedFeatureError

RESULT = {
    "type": "result",
    "raw": {
        "start": {"test_start": {"protocol": "TCP"}},
        "end": {"sum_sent": {"bytes": 10, "seconds": 1, "bits_per_second": 80}},
    },
    "error": None,
    "evidence": {},
    "native_version": "iperf 3.21",
    "started_at": 10,
    "completed_at": 11,
    "elapsed": 1,
}


def capture_receipt(**updates):
    """Build the exact bounded native-capture receipt with overridable evidence."""
    receipt = {
        "schema_version": 1,
        "callbacks": 1,
        "copied": 1,
        "capture_dropped": 0,
        "malformed": 0,
        "retention_dropped": 0,
        "live_emitted": 1,
        "live_dropped": 0,
        "complete_document": True,
        "complete_document_source": "callback",
        "getter_error": None,
        "reconstruction_complete": True,
        "diagnostics": [],
        "limits": dict(_execution._CAPTURE_LIMITS),
    }
    receipt.update(updates)
    return receipt


def live_frame(kind="native_document", data=None, *, sequence=1, offset=0.1):
    """Build a native-origin typed frame without Session-owned transport metadata."""
    return {
        "type": "live_event",
        "kind": kind,
        "data": RESULT["raw"] if data is None else data,
        "capture_sequence": sequence,
        "arrival_offset_seconds": offset,
        "time": 10.5,
    }


def test_typed_and_legacy_callbacks_preserve_separate_sequences_and_identity(monkeypatch):
    """Duplicated delivery modes keep legacy envelope counts and admitted identities."""
    processes = child(
        monkeypatch,
        "assert request['events'] and request['live_events']\n"
        "send({'type':'event','kind':'end','data':result['raw']['end'],'sequence':1,'time':10.5})\n"
        f"send({live_frame('native_end', RESULT['raw']['end'])!r})\n"
        f"send({live_frame(sequence=2, offset=0.2)!r})\n"
        f"result['capture']={capture_receipt(callbacks=2, copied=2, live_emitted=2)!r}\n"
        "result.update(events_emitted=1,events_dropped=0)\nsend(result)\nsend({'type':'done'})\n",
    )
    live, legacy = [], []
    result = _execution.run_worker("client", {}, on_event=legacy.append, _on_live_event=live.append)
    assert [event.kind for event in live] == [
        "worker_state",
        "worker_state",
        "native_end",
        "native_document",
    ]
    assert [event.delivery_sequence for event in live] == [1, 2, 3, 4]
    assert [event.capture_sequence for event in live] == [None, None, 1, 2]
    assert [(event.kind, event.sequence, event.received_at_seconds) for event in legacy] == [
        ("end", 1, 10.5)
    ]
    worker = result.extensions["iperf3_lib.worker"]
    assert all(
        (event.request_id, event.worker_id, event.run_index)
        == (worker["request_id"], worker["worker_id"], 1)
        for event in live
    )
    assert result.extensions["iperf3_lib.event_delivery"]["emitted"] == 1
    assert result.extensions["iperf3_lib.live_delivery"]["emitted"] == 2
    live[-1].payload.raw["end"].clear()
    legacy[0].data.clear()
    assert result.raw["end"] and live[-2].payload.raw
    assert processes[0].poll() == 0


def test_typed_projected_oversize_is_counted_without_losing_complete_result(monkeypatch):
    """Canonical projection expansion is advisory loss, even when the wire frame fits."""
    processes = child(
        monkeypatch,
        "payload={'sum':{'bytes':1,'seconds':1},'padding':'x'*(_ipc.MAX_EVENT_BYTES-550)}\n"
        "send({'type':'live_event','kind':'interval','data':payload,'capture_sequence':1,'arrival_offset_seconds':0.1,'time':10.5})\n"
        f"result['capture']={capture_receipt()!r}\nsend(result)\nsend({{'type':'done'}})\n",
    )
    live = []
    result = _execution.run_worker("client", {}, _on_live_event=live.append)
    assert result.ok and processes[0].poll() == 0
    assert result.extensions["iperf3_lib.live_delivery"]["projection_dropped"] == 1
    gaps = [event.payload for event in live if event.kind == "delivery_gap"]
    assert [(gap.stage, gap.dropped) for gap in gaps] == [("projection", 1)]


def test_typed_semantic_malformed_event_preserves_independent_complete_result(monkeypatch):
    """Valid JSON with unusable native fields becomes an explicit advisory malformed event."""
    child(
        monkeypatch,
        f"send({live_frame('interval', {'streams': 'bad'})!r})\n"
        f"result['capture']={capture_receipt()!r}\nsend(result)\nsend({{'type':'done'}})\n",
    )
    live = []
    result = _execution.run_worker("client", {}, _on_live_event=live.append)
    malformed = next(event for event in live if event.kind == "malformed")
    assert malformed.payload.reason == "invalid_native_shape" and malformed.capture_sequence == 1
    assert result.ok
    assert result.extensions["iperf3_lib.live_delivery"]["projection_malformed"] == 1
    assert any(d.code == "execution.malformed_live_event" for d in result.diagnostics)


@pytest.mark.parametrize("native_error", [None, "original native failure"])
def test_malformed_final_native_document_retains_raw_and_native_failure_precedence(native_error):
    """Normalization failure remains inspectable and cannot replace the original native error."""
    message = {**RESULT, "raw": {"intervals": [{"streams": "bad"}]}, "error": native_error}
    result = _execution._decode_result(message, "client")
    assert not result.ok and result.raw == message["raw"]
    assert result.execution.status == ("failed" if native_error else "incomplete")
    assert result.error == (native_error or "Native JSON could not be normalized")
    assert any(d.code == "execution.malformed_native_json" for d in result.diagnostics)


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("native_error", [None, "original failure"])
def test_capture_loss_only_invalidates_reconstruction_and_preserves_native_error(
    complete, native_error
):
    """Capture quality and native failure stay independent from complete-document recovery."""
    capture = capture_receipt(
        callbacks=2,
        capture_dropped=1,
        reconstruction_complete=False,
        complete_document=complete,
        complete_document_source="getter" if complete else None,
    )
    result = _execution._decode_result(
        {**RESULT, "capture": capture, "error": native_error}, "client"
    )
    assert result.ok is (complete and native_error is None)
    assert result.execution.status == (
        "failed" if native_error else "completed" if complete else "incomplete"
    )
    if native_error:
        assert result.error == native_error
    assert result.extensions["iperf3_lib.event_capture"] == capture
    capture["callbacks"] = 999
    assert result.extensions["iperf3_lib.event_capture"]["callbacks"] == 2


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": True},
        {"callbacks": True},
        {"copied": -1},
        {"callbacks": 0},
        {"malformed": 2},
        {"retention_dropped": 2},
        {"live_dropped": 2},
        {"complete_document": 1},
        {"complete_document_source": None},
        {"reconstruction_complete": False},
        {"getter_error": "x" * 257},
        {"diagnostics": ["x"] * 9},
        {"diagnostics": [True]},
        {"limits": {**_execution._CAPTURE_LIMITS, "diagnostics": True}},
        {"unexpected": 1},
    ],
)
def test_capture_receipt_is_strict_and_rejects_contradictions(update):
    """Loss receipts cannot be fabricated by bools, missing bounds or inconsistent algebra."""
    with pytest.raises(_ipc.IPCError):
        _execution._validate_capture(capture_receipt(**update))


@pytest.mark.parametrize(
    "update",
    [
        {"capture_sequence": True},
        {"capture_sequence": 0},
        {"capture_sequence": None},
        {"arrival_offset_seconds": True},
        {"arrival_offset_seconds": -1},
        {"arrival_offset_seconds": float("inf")},
        {"time": None},
        {"time": -1},
        {"kind": "terminal"},
        {"kind": "worker_state"},
        {"kind": "interval", "data": 42},
    ],
)
def test_typed_native_provenance_rejects_invalid_fields(update):
    """Only native callback kinds with positive capture sequence and finite arrivals pass."""
    with pytest.raises(_ipc.IPCError):
        _execution._validate_live_frame({**live_frame(), **update})


@pytest.mark.parametrize(
    "second",
    [
        live_frame(sequence=1, offset=0.2),
        live_frame(sequence=2, offset=0.01),
    ],
)
def test_typed_capture_replay_and_monotonic_time_regressions_abort_worker(monkeypatch, second):
    """Native sequence replay and arrival-clock reversal fail before application admission."""
    processes = child(monkeypatch, f"send({live_frame()!r})\nsend({second!r})\ntime.sleep(20)\n")
    with pytest.raises(IperfLibraryError, match="Out-of-order"):
        _execution.run_worker("client", {}, _on_live_event=lambda event: None, timeout=3)
    assert processes[0].poll() is not None


def test_typed_result_counts_cannot_contradict_received_native_events(monkeypatch):
    """Result receipts account for every accepted live frame independently of legacy counts."""
    processes = child(
        monkeypatch,
        f"send({live_frame()!r})\nresult['capture']={capture_receipt(live_emitted=0)!r}\n"
        "send(result)\nsend({'type':'done'})\n",
    )
    with pytest.raises(IperfLibraryError, match="contradict typed delivery"):
        _execution.run_worker("client", {}, _on_live_event=lambda event: None)
    assert processes[0].poll() is not None


def test_typed_and_legacy_inbox_share_capacity_with_separate_per_run_losses(monkeypatch):
    """Two delivery modes cannot double queue capacity or alter each other's counters."""
    monkeypatch.setattr(_ipc, "MAX_PENDING_EVENTS", 2)
    inbox = _execution._Inbox()
    for kind in ("event", "live_event", "live_event", "event"):
        inbox.put({"type": kind}, 100)
    assert inbox.events == 2 and inbox.dropped == inbox.live_dropped == 1
    inbox.put({"type": "result"}, 200)
    assert inbox.dropped == inbox.live_dropped == 0
    assert inbox.get(0)["type"] == "event"
    assert inbox.get(0)["type"] == "live_event"
    assert inbox.get(0) == {"type": "result", "_parent_dropped": 1, "_parent_live_dropped": 1}


def test_typed_callback_error_keeps_process_cleanup_precedence(monkeypatch):
    """An application typed callback failure is raised after the worker has been reaped."""
    processes = child(
        monkeypatch,
        f"send({live_frame()!r})\nresult['capture']={capture_receipt()!r}\n"
        "time.sleep(.1)\nsend(result)\nsend({'type':'done'})\n",
    )
    calls = []

    def fail(event):
        calls.append(event.kind)
        if event.kind == "native_document":
            raise LookupError("consumer failure")

    with pytest.raises(LookupError, match="consumer failure"):
        _execution.run_worker("client", {}, _on_live_event=fail)
    assert calls == ["worker_state", "worker_state", "native_document"]
    assert processes[0].poll() == 0 and processes[0].stdout.closed


@pytest.mark.parametrize("tamper", [False, True])
def test_capture_gap_has_no_native_arrival_and_matches_final_loss_count(monkeypatch, tamper):
    """Synthetic capture loss keeps explicit absent arrival and cannot contradict receipts."""
    gap = {
        "type": "live_event",
        "kind": "delivery_gap",
        "data": {"stage": "capture", "dropped": 2 if tamper else 1},
        "capture_sequence": None,
        "arrival_offset_seconds": None,
        "time": None,
    }
    receipt = capture_receipt(
        callbacks=1,
        copied=0,
        capture_dropped=1,
        reconstruction_complete=False,
        complete_document=False,
        complete_document_source=None,
    )
    processes = child(
        monkeypatch,
        f"send({gap!r})\nresult['capture']={receipt!r}\nsend(result)\nsend({{'type':'done'}})\n",
    )
    live = []
    if tamper:
        with pytest.raises(IperfLibraryError, match="contradict typed delivery"):
            _execution.run_worker("client", {}, _on_live_event=live.append)
    else:
        result = _execution.run_worker("client", {}, _on_live_event=live.append)
        assert not result.ok and result.execution.status == "incomplete"
        event = next(event for event in live if event.kind == "delivery_gap")
        assert (
            event.capture_sequence
            is event.arrival_offset_seconds
            is event.received_at_seconds
            is None
        )
    assert processes[0].poll() is not None


def test_typed_server_continuation_resets_native_sequence_and_projection_provenance(monkeypatch):
    """Each server run has independent capture counters/start metadata and stable session identity."""
    message = live_frame("native_start", {"test_start": {"protocol": "TCP", "reverse": 1}})
    interval = live_frame(
        "interval", {"sum": {"sender": False, "bytes": 1, "seconds": 1}}, sequence=1
    )
    child(
        monkeypatch,
        f"result['capture']={capture_receipt()!r}\nsend({message!r})\nsend(result)\n"
        "assert receive()['continue']\n"
        f"send({interval!r})\nsend(result)\nassert not receive()['continue']\nsend({{'type':'done'}})\n",
    )
    live = []
    result = _execution.run_worker("server", {}, max_runs=2, _on_live_event=live.append)
    assert result.ok and result.extensions["iperf3_lib.worker"]["run_index"] == 2
    interval_event = next(event for event in live if event.kind == "interval")
    assert interval_event.run_index == 2 and interval_event.capture_sequence == 1
    assert interval_event.payload.measurements[0].direction == "unknown"
    assert len({event.request_id for event in live}) == 1


@pytest_asyncio.fixture
async def loop_error_reports():
    """Capture unsolicited asyncio diagnostics without changing their delivery."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports = []
    loop.set_exception_handler(lambda loop, context: reports.append(context))
    try:
        yield reports
    finally:
        loop.set_exception_handler(previous)


def async_call(role, **kwargs):
    """Build a public async call without retaining reloaded client classes."""
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server

    instance = Client(ClientConfig("127.0.0.1")) if role == "client" else Server()
    operation = instance.arun if role == "client" else instance.aserve_once
    return instance, operation(**kwargs)


async def finish_child_test(task, processes):
    """Bound failed-test cleanup so a regression cannot leak its test worker."""
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if not task.done():
        task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, Exception):
        pass


def child(monkeypatch, body, *, read_request=True, ready=True):
    """Replace only the child program, retaining actual pipe/process behavior."""
    original = subprocess.Popen
    processes = []
    program = (
        "import io,json,os,struct,sys,time,platform,importlib.metadata\n"
        "from iperf3_lib import _ipc\n"
        "from iperf3_lib.ffi.api import POSSIBLE_NAMES\n"
        + (
            "request=_ipc.read_frame(sys.stdin.buffer)\n"
            "session=_ipc.Session.from_request(request)\n"
            "session.validate(request)\n"
            if read_request
            else ""
        )
        + f"result={RESULT!r}\n"
        "runs=0\nfailed=False\n"
        "def receive():\n return session.validate(_ipc.read_frame(sys.stdin.buffer))\n"
        "def send(message):\n"
        " global runs,failed\n"
        " message=dict(message)\n"
        " kind=message['type']\n"
        " index=runs+1\n"
        " if kind=='result': runs+=1\n"
        " if kind=='error': failed=True\n"
        " if kind=='done':\n"
        "  message={'type':'terminal','status':'failed' if failed else 'completed','runs':runs}\n"
        "  index=max(1,runs)\n"
        " sys.stdout.buffer.write(session.emit(message,run_index=index))\n"
        " sys.stdout.buffer.flush()\n"
        "ready_message={'type':'ready','pid':os.getpid(),'producer':{"
        "'python_version':platform.python_version(),"
        "'package_version':importlib.metadata.version('iperf3-lib'),"
        "'native_version':'iperf 3.21','library_selector':"
        "({'source':'IPERF3_LIB','value':os.environ['IPERF3_LIB']} if os.getenv('IPERF3_LIB') "
        "else {'source':'platform_search','candidates':list(POSSIBLE_NAMES)})}}\n"
        + ("send(ready_message)\n" if read_request and ready else "")
        + body.replace("json.loads(sys.stdin.readline())", "receive()")
    )

    def popen(args, **kwargs):
        assert args == _execution._worker_command()
        # Windows venv launchers create another process. Use the actual base
        # interpreter so the fake worker's PID matches the owned OS process.
        interpreter = sys._base_executable if sys.platform == "win32" else sys.executable
        process = original([interpreter, "-u", "-c", program], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(_execution.subprocess, "Popen", popen)
    return processes


def worker_request(role="client", **options):
    """Construct one real protocol request and its sequenced command writer."""
    session = _ipc.Session.new()
    request = _ipc.read_frame(
        io.BytesIO(
            session.emit(
                {
                    "type": "request",
                    "role": role,
                    "options": {},
                    "password": None,
                    "events": False,
                    "max_runs": None if role == "server" else 1,
                    **options,
                }
            )
        )
    )
    return request, session


def execute_worker(request, output=None, *, continuation=()):
    """Exercise the worker's complete framed session with native stand-ins."""
    request, session = worker_request(**request)
    commands = io.BytesIO(
        b"".join(
            session.emit({"type": "continue", "continue": value}, run_index=index)
            for index, value in enumerate(continuation, 1)
        )
    )
    return _worker.execute(request, io.BytesIO() if output is None else output, commands)


def worker_messages(output):
    """Decode every complete response frame and require clean stream exhaustion."""
    stream = io.BytesIO(output.getvalue())
    messages = []
    session = None
    while (message := _ipc.read_frame(stream)) is not None:
        if session is None:
            session = _ipc.Session(message["request_id"], message["worker_id"])
        session.validate(message)
        messages.append(message)
    return messages


def test_client_result_is_copied_and_child_is_reaped(monkeypatch):
    """Native-shaped JSON passes through the transport without Python pickles."""
    processes = child(monkeypatch, "send(result)\nsend({'type':'done'})\n")
    result = _execution.run_worker("client", {})
    assert result.ok and result.reporting_role == "client"
    assert result.execution.native_version == "iperf 3.21"
    assert processes[0].poll() == 0


def test_server_callback_controls_next_iteration(monkeypatch):
    """Parent decisions are acknowledged between complete server results."""
    processes = child(
        monkeypatch,
        "for i in range(5):\n send(result)\n if not json.loads(sys.stdin.readline())['continue']: break\nsend({'type':'done'})\n",
    )
    results = []
    result = _execution.run_worker("server", {"port": 5201}, max_runs=2, on_result=results.append)
    assert len(results) == 2 and result is results[-1]
    assert result.extensions["iperf3_lib.server_config"] == {"port": 5201}
    assert processes[0].poll() == 0


def test_deadline_terminates_and_reaps_worker(monkeypatch):
    """A blocked child cannot outlive a caller's explicit process deadline."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    with pytest.raises(TimeoutError, match="process deadline"):
        _execution.run_worker("client", {}, timeout=0.15)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("role", ["client", "server"])
def test_deadline_reaps_child_while_callback_is_blocked(monkeypatch, role):
    """The deadline watchdog remains independent of the calling callback thread."""
    body = (
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\ntime.sleep(30)\n"
        if role == "client"
        else "send(result)\ntime.sleep(30)\n"
    )
    processes = child(monkeypatch, body)

    def callback(_):
        time.sleep(0.5)
        assert processes[0].poll() is not None

    callback_name = "on_event" if role == "client" else "on_result"
    with pytest.raises(TimeoutError, match="process deadline"):
        _execution.run_worker(role, {}, timeout=0.25, **{callback_name: callback})
    assert processes[0].poll() is not None


def test_callback_mutation_does_not_change_native_continuation(monkeypatch):
    """A consumer cannot accidentally hide native failure by editing its Result."""
    child(
        monkeypatch,
        "result['error']='native failed'\nsend(result)\nassert not json.loads(sys.stdin.readline())['continue']\nsend({'type':'done'})\n",
    )

    def callback(result):
        result.ok = True

    _execution.run_worker("server", {}, on_result=callback, max_runs=3)


def test_reconstructed_events_are_retained_with_provenance():
    """A streamed projection is clearly labelled and preserves original event envelopes."""
    event = {"event": "future_extension", "data": {"value": 4}}
    result = _execution._decode_result(
        {**RESULT, "raw_representation": "reconstructed_events", "native_events": [event]}, "client"
    )
    assert result.extensions["iperf3_lib.native_json"] == {
        "representation": "reconstructed_events",
        "events": [event],
    }
    assert any(item.code == "execution.reconstructed_json" for item in result.diagnostics)


def test_failed_native_return_without_error_code_remains_explicit():
    """A zero error register does not hide a negative native return code."""
    result = _execution._decode_result(
        {
            **RESULT,
            "native_returncode": -1,
            "native_error_code": 0,
            "error": "Native call failed without a specific native error code",
        },
        "server",
    )
    assert not result.ok and result.execution.status == "failed"
    assert result.extensions["iperf3_lib.native_status"] == {"returncode": -1, "error_code": 0}
    assert any(d.code == "execution.native_error_detail_missing" for d in result.diagnostics)


@pytest.mark.parametrize("body", ["sys.exit(7)\n", "print('bad json',flush=True)\n"])
def test_abnormal_native_exit_does_not_terminate_host(monkeypatch, body):
    """Parser exit and malformed IPC become explicit parent exceptions."""
    processes = child(monkeypatch, body)
    with pytest.raises(IperfLibraryError) as captured:
        _execution.run_worker("client", {})
    assert processes[0].poll() is not None
    if body.startswith("sys.exit"):
        assert "exit 7" in str(captured.value)
        assert processes[0].returncode == 7


@pytest.mark.parametrize(
    "body,ready",
    [
        ("send(result)\nsend({'type':'done'})\n", False),
        ("ready_message['pid'] += 1\nsend(ready_message)\n", False),
        ("send(ready_message)\n", True),
        (
            "message=_ipc.read_frame(io.BytesIO(session.emit(result)))\nmessage['type']='unknown'\nsys.stdout.buffer.write(_ipc.encode_frame(message))\nsys.stdout.buffer.flush()\n",
            True,
        ),
        ("send({'type':'done'})\n", True),
        ("send(result)\nsend(result)\n", True),
        ("send(result)\nsend({'type':'done'})\nsend(result)\n", True),
        (
            "send(result)\nsend({'type':'done'})\nsys.stdout.buffer.write(b'\\x00')\nsys.stdout.buffer.flush()\n",
            True,
        ),
        ("sys.stdout.buffer.write(b'\\x00\\x00')\nsys.stdout.buffer.flush()\n", True),
        ("sys.stdout.buffer.write(struct.pack('!I',100)+b'{}')\nsys.stdout.buffer.flush()\n", True),
        ("sys.stdout.buffer.write(struct.pack('!I',2**31))\nsys.stdout.buffer.flush()\n", True),
    ],
    ids=[
        "no-ready",
        "wrong-pid",
        "repeated-ready",
        "unknown-kind",
        "early-terminal",
        "duplicate-result",
        "after-terminal",
        "terminal-trailing-byte",
        "truncated-header",
        "truncated-body",
        "oversized-header",
    ],
)
def test_real_pipe_protocol_faults_reject_results_and_release_process(monkeypatch, body, ready):
    """Malformed, premature and trailing child output cannot become a successful Result."""
    processes = child(monkeypatch, body, ready=ready)
    with pytest.raises(IperfLibraryError):
        _execution.run_worker("client", {}, timeout=3)
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "f" * 32),
        ("worker_id", "e" * 32),
        ("protocol_version", 999),
        ("transport_sequence", 1),
        ("transport_sequence", 99),
        ("run_index", 2),
    ],
    ids=[
        "wrong-request",
        "wrong-worker",
        "wrong-version",
        "replayed-sequence",
        "sequence-gap",
        "wrong-run",
    ],
)
def test_real_pipe_replayed_or_unbound_results_are_rejected(monkeypatch, field, value):
    """A plausible native result must belong to this exact ready worker and run."""
    processes = child(
        monkeypatch,
        "envelope=_ipc.read_frame(io.BytesIO(session.emit(result,run_index=1)))\n"
        f"envelope[{field!r}]={value!r}\n"
        "sys.stdout.buffer.write(_ipc.encode_frame(envelope))\nsys.stdout.buffer.flush()\n",
    )
    with pytest.raises(IperfLibraryError):
        _execution.run_worker("client", {}, timeout=3)
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize(
    "emitted,dropped", [(0, 1), (0, 0)], ids=["drops-exceed-emitted", "observed-exceeds-emitted"]
)
def test_result_cannot_contradict_received_event_delivery(monkeypatch, emitted, dropped):
    """A final native receipt cannot erase observed events or invent impossible losses."""
    event = (
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\n"
        if dropped == 0
        else ""
    )
    processes = child(
        monkeypatch,
        event + f"result.update(events_emitted={emitted},events_dropped={dropped})\n"
        "send(result)\nsend({'type':'done'})\n",
    )
    with pytest.raises(IperfLibraryError, match="worker result"):
        _execution.run_worker("client", {}, timeout=3)
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize(
    "name,expected",
    [
        ("UnsupportedFeatureError", UnsupportedFeatureError),
        ("ValueError", ValueError),
        ("UnknownNativeError", IperfLibraryError),
    ],
)
def test_child_error_classes_are_allowlisted(monkeypatch, name, expected):
    """Transport never imports or executes a child-named exception class."""
    processes = child(
        monkeypatch,
        f"send({{'type':'error','class':{name!r},'message':'unavailable'}})\nsend({{'type':'done'}})\n",
    )
    with pytest.raises(expected, match="unavailable"):
        _execution.run_worker("client", {})
    assert processes[0].poll() == 0


def test_callback_exception_waits_for_active_native_completion(monkeypatch):
    """Callback failures are raised after the child completes and is reaped."""
    processes = child(
        monkeypatch,
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\ntime.sleep(.05)\nresult['events_emitted']=1\nsend(result)\nsend({'type':'done'})\n",
    )

    def callback(_):
        raise LookupError("consumer failure")

    with pytest.raises(LookupError, match="consumer failure"):
        _execution.run_worker("client", {}, on_event=callback)
    assert processes[0].poll() == 0


def test_slow_callback_has_bounded_delivery_and_explicit_drops(monkeypatch):
    """Event drops preserve the final result and retain monotonic sequence numbers."""
    processes = child(
        monkeypatch,
        "for i in range(3000):\n send({'type':'event','kind':'interval','data':{},'sequence':i+1,'time':10})\nresult['events_emitted']=3000\nsend(result)\nsend({'type':'done'})\n",
    )
    seen = []

    def callback(event):
        seen.append(event.sequence)
        if len(seen) == 1:
            processes[0].wait(timeout=8)

    result = _execution.run_worker("client", {}, on_event=callback)
    assert result.ok and seen == sorted(set(seen))
    delivery = result.extensions["iperf3_lib.event_delivery"]
    assert delivery["dropped"] > 0
    assert len(seen) + delivery["dropped"] == 3000


def test_large_event_byte_saturation_preserves_result_and_terminal(monkeypatch):
    """A blocked callback cannot turn a byte-full event queue into lost terminal state."""
    processes = child(
        monkeypatch,
        "for i in range(64):\n send({'type':'event','kind':'interval','data':{'payload':'x'*262144},'sequence':i+1,'time':10})\n"
        "result['events_emitted']=64\nsend(result)\nsend({'type':'done'})\n",
    )
    seen = []

    def callback(event):
        seen.append(event.sequence)
        assert len(event.data["payload"]) == 262144
        if len(seen) == 1:
            processes[0].wait(timeout=8)

    result = _execution.run_worker("client", {}, on_event=callback, timeout=10)
    delivery = result.extensions["iperf3_lib.event_delivery"]
    assert result.ok and processes[0].poll() == 0
    assert delivery["dropped"] > 0 and len(seen) + delivery["dropped"] == 64
    assert seen == sorted(set(seen))
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_parent_inbox_byte_budget_reserves_result_and_terminal_capacity():
    """Decoded queue accounting bounds bytes even before the event-count limit."""
    inbox = _execution._Inbox()
    admitted = _ipc.MAX_PENDING_EVENT_BYTES // _ipc.MAX_EVENT_BYTES
    assert admitted < _ipc.MAX_PENDING_EVENTS
    for index in range(admitted + 1):
        inbox.put({"type": "event", "sequence": index}, _ipc.MAX_EVENT_BYTES)
    assert inbox.events == admitted and inbox.event_bytes <= _ipc.MAX_PENDING_EVENT_BYTES
    assert inbox.dropped == 1
    inbox.put({"type": "result"}, _ipc.MAX_FRAME_BYTES)
    inbox.put({"type": "terminal"}, 200)
    inbox.finish()
    for _ in range(admitted):
        assert inbox.get(0)["type"] == "event"
    assert inbox.get(0) == {"type": "result", "_parent_dropped": 1, "_parent_live_dropped": 0}
    assert inbox.get(0) == {"type": "terminal"}
    assert inbox.get(0) == {"type": "eof"}
    assert (inbox.events, inbox.event_bytes, inbox.controls, inbox.control_bytes) == (0, 0, 0, 0)


def test_deadline_interrupts_a_partial_response_frame(monkeypatch):
    """An incomplete advertised payload never blocks the operation deadline."""
    processes = child(
        monkeypatch,
        "sys.stdout.buffer.write(struct.pack('!I',10000)+b'{')\nsys.stdout.buffer.flush()\ntime.sleep(30)\n",
    )
    with pytest.raises(TimeoutError, match="process deadline"):
        _execution.run_worker("client", {}, timeout=0.3)
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.asyncio
async def test_cancellation_interrupts_an_observed_partial_response_frame(monkeypatch):
    """Cancelling after a partial payload arrives reaps the worker without waiting for bytes."""
    processes = child(
        monkeypatch,
        "sys.stdout.buffer.write(struct.pack('!I',10000)+b'{')\nsys.stdout.buffer.flush()\ntime.sleep(30)\n",
    )
    partial = asyncio.Event()
    loop = asyncio.get_running_loop()
    feed = _ipc.FrameDecoder.feed_sized

    def observe(decoder, data):
        frames = feed(decoder, data)
        if decoder._length == 10000 and decoder._body == b"{":
            loop.call_soon_threadsafe(partial.set)
        return frames

    monkeypatch.setattr(_ipc.FrameDecoder, "feed_sized", observe)
    _, call = async_call("client")
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(partial.wait(), timeout=5)
        task.cancel("cancel incomplete IPC")
        with pytest.raises(asyncio.CancelledError, match="cancel incomplete IPC"):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        await finish_child_test(task, processes)


@pytest.mark.asyncio
async def test_protocol_failure_stops_worker_while_callback_is_blocked(monkeypatch, tmp_path):
    """A rejected frame stops native work before the running user callback returns."""
    marker = tmp_path / "callback-entered"
    processes = child(
        monkeypatch,
        "import pathlib\n"
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\n"
        f"while not pathlib.Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        "sys.stdout.buffer.write(struct.pack('!I',2**31))\nsys.stdout.buffer.flush()\ntime.sleep(30)\n",
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def callback(event):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(6)

    _, call = async_call("client", on_event=callback)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        marker.touch()
        await asyncio.wait_for(asyncio.to_thread(processes[0].wait, 3), timeout=4)
        assert not task.done()
        release.set()
        with pytest.raises(IperfLibraryError, match="byte length"):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        release.set()
        await finish_child_test(task, processes)


@pytest.mark.asyncio
async def test_clean_terminal_eof_bounds_child_exit_while_callback_is_blocked(
    monkeypatch, tmp_path
):
    """A terminal worker cannot linger behind a user callback after closing its output."""
    from iperf3_lib._cancellation import _ExecutionControl

    marker = tmp_path / "callback-entered"
    processes = child(
        monkeypatch,
        "import pathlib\n"
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\n"
        f"while not pathlib.Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        "result['events_emitted']=1\nsend(result)\nsend({'type':'done'})\n"
        "os.close(sys.stdout.fileno())\ntime.sleep(30)\n",
    )
    wait_for_exit = _ExecutionControl.wait_for_exit
    limits = []

    def bounded_wait(control, timeout):
        limits.append(timeout)
        assert timeout == 5
        return wait_for_exit(control, 0.2)

    monkeypatch.setattr(_ExecutionControl, "wait_for_exit", bounded_wait)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def callback(event):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(6)

    _, call = async_call("client", on_event=callback)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        marker.touch()
        await asyncio.wait_for(asyncio.to_thread(processes[0].wait, 3), timeout=4)
        assert limits == [5]
        assert not task.done()
        release.set()
        with pytest.raises(IperfLibraryError, match="did not exit"):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        release.set()
        await finish_child_test(task, processes)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"timeout": True}, TypeError),
        ({"timeout": 0}, ValueError),
        ({"timeout": float("inf")}, ValueError),
        ({"on_event": 1}, TypeError),
        ({"max_runs": 0}, ValueError),
    ],
)
def test_transport_rejects_invalid_controls_before_spawn(monkeypatch, kwargs, error):
    """Invalid execution controls do not start a native worker."""
    monkeypatch.setattr(_execution.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(error):
        _execution.run_worker("client", {}, **kwargs)


def test_pre_stopped_server_never_spawns(monkeypatch):
    """Cooperative stop before admission returns an explicit incomplete outcome."""
    monkeypatch.setattr(_execution.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    result = _execution.run_worker("server", {}, should_stop=lambda: True)
    assert not result.ok and result.execution.status == "incomplete"


class NativeDouble:
    """Native lifecycle double that emits copied callbacks before each reset."""

    i_errno = 1

    def __init__(self, *, fail=False):
        """Record free/reset counts independently of the test outcome."""
        self.frees = 0
        self.resets = 0
        self.fail = fail
        self.allocations = []
        self.freed = []

    def iperf_new_test(self):
        """Allocate a recognizable non-null native token."""
        test = object()
        self.allocations.append(test)
        return test

    def iperf_defaults(self, test):
        """Accept native default setup."""
        return 0

    def iperf_free_test(self, test):
        """Record exactly-once resource ownership."""
        self.frees += 1
        self.freed.append(test)

    def iperf_get_iperf_version(self):
        """Return a fixed native version token."""
        return b"iperf 3.21"

    def iperf_set_test_json_callback(self, test, callback):
        """Retain the installed C callback stand-in."""
        self.callback = callback

    def iperf_run_client(self, test):
        """Emit streaming native JSON and an optional native failure."""
        for kind, data in [
            ("start", {"test_start": {"protocol": "TCP"}}),
            ("interval", {"sum": {"bytes": 10}}),
            ("end", {}),
        ]:
            self.callback(test, json.dumps({"event": kind, "data": data}).encode())
        return -1 if self.fail else 0

    iperf_run_server = iperf_run_client

    def iperf_reset_test(self, test):
        """Reset only after the corresponding JSON was copied."""
        self.resets += 1

    def iperf_strerror(self, code):
        """Return the current native error before cleanup."""
        return b"native test error"


@pytest.mark.parametrize("role,fail", [("client", False), ("client", True), ("server", False)])
def test_worker_copies_json_before_reset_and_frees_once(monkeypatch, role, fail):
    """Success/failure/sequential paths preserve each native lifetime exactly once."""
    import iperf3_lib.native_options as native_options

    native = NativeDouble(fail=fail)
    monkeypatch.setattr(_worker, "lib", native)
    monkeypatch.setattr(
        _worker,
        "ffi",
        SimpleNamespace(
            NULL=None,
            string=lambda value, maxlen=None: value[:maxlen],
            callback=lambda signature, callback: callback,
        ),
    )
    monkeypatch.setattr(
        native_options, "configure_native", lambda *a, **k: SimpleNamespace(evidence={})
    )
    monkeypatch.setattr(native_options, "observe_native_options", lambda *a: {})
    output = io.BytesIO()
    execute_worker(
        {"role": role, "options": {}, "events": True},
        output,
        continuation=(True, False),
    )
    messages = worker_messages(output)
    results = [item for item in messages if item["type"] == "result"]
    assert messages[0]["type"] == "ready" and messages[-1]["type"] == "terminal"
    assert messages[-1]["status"] == "completed"
    assert len(results) == (2 if role == "server" else 1)
    assert all(len(item["raw"]["intervals"]) == 1 for item in results)
    assert bool(results[0]["error"]) is fail
    assert native.frees == (2 if role == "server" else 1) and native.resets == 0


def test_worker_setup_exception_frees_allocated_test(monkeypatch):
    """Configuration failure still releases the non-null test exactly once."""
    import iperf3_lib.native_options as native_options

    native = NativeDouble()
    monkeypatch.setattr(_worker, "lib", native)
    monkeypatch.setattr(_worker, "ffi", SimpleNamespace(NULL=None))

    def configure(*args, **kwargs):
        raise ValueError("invalid native setup")

    monkeypatch.setattr(native_options, "configure_native", configure)
    with pytest.raises(ValueError, match="invalid native setup"):
        execute_worker({"role": "client", "options": {}})
    assert native.frees == 1


@pytest.fixture
def worker_native(monkeypatch):
    """Install a native stand-in and retain every worker-created thread for inspection."""
    import iperf3_lib.native_options as native_options

    native = NativeDouble()
    monkeypatch.setattr(_worker, "lib", native)
    monkeypatch.setattr(
        _worker,
        "ffi",
        SimpleNamespace(
            NULL=None,
            string=lambda value, maxlen=None: value[:maxlen],
            callback=lambda signature, callback: callback,
        ),
    )
    monkeypatch.setattr(
        native_options, "configure_native", lambda *a, **k: SimpleNamespace(evidence={})
    )
    monkeypatch.setattr(native_options, "observe_native_options", lambda *a: {})
    original_thread = _worker.threading.Thread
    threads = []

    def create_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(_worker.threading, "Thread", create_thread)
    return native, threads


@pytest.mark.parametrize("stage", ["construct", "start"])
def test_worker_writer_startup_failure_never_allocates_native_test(
    monkeypatch, worker_native, stage
):
    """A transport that cannot start acquires no native lifetime to leak."""
    native, threads = worker_native

    def fail(*args, **kwargs):
        raise RuntimeError("thread startup unavailable")

    if stage == "construct":
        monkeypatch.setattr(_worker.threading, "Thread", fail)
    else:
        factory = _worker.threading.Thread

        def fail_start(*args, **kwargs):
            thread = factory(*args, **kwargs)
            monkeypatch.setattr(thread, "start", fail)
            return thread

        monkeypatch.setattr(_worker.threading, "Thread", fail_start)
    with pytest.raises(RuntimeError, match="startup unavailable"):
        execute_worker({"role": "client", "options": {}})
    assert native.allocations == native.freed == []
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("stage", ["construct", "start"])
def test_parent_reader_startup_failure_reaps_child_and_closes_pipes(monkeypatch, stage):
    """Host thread resource exhaustion still closes and reaps the started process."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    original_thread = _execution.threading.Thread

    def fail(*args, **kwargs):
        raise RuntimeError("reader startup unavailable")

    def create_thread(*args, **kwargs):
        is_reader = kwargs["target"].__name__ == "read_messages"
        if is_reader and stage == "construct":
            fail()
        thread = original_thread(*args, **kwargs)
        if is_reader:
            monkeypatch.setattr(thread, "start", fail)
        return thread

    monkeypatch.setattr(_execution.threading, "Thread", create_thread)
    try:
        with pytest.raises(RuntimeError, match="startup unavailable"):
            _execution.run_worker("client", {})
        if stage == "construct":
            assert not processes
        else:
            assert len(processes) == 1
            assert processes[0].poll() is not None
            assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        # Keep a failing regression from leaking the very child under test.
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


@pytest.mark.parametrize("stage", ["construct", "start"])
def test_parent_watchdog_startup_failure_reaps_child(monkeypatch, stage):
    """A configured timeout cannot leak a worker when its timer cannot start."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    original_timer = _execution.threading.Timer

    def timer(*args, **kwargs):
        if stage == "construct":
            raise RuntimeError("watchdog startup unavailable")
        instance = original_timer(*args, **kwargs)

        def fail():
            raise RuntimeError("watchdog startup unavailable")

        monkeypatch.setattr(instance, "start", fail)
        return instance

    monkeypatch.setattr(_execution.threading, "Timer", timer)
    with pytest.raises(RuntimeError, match="watchdog startup unavailable"):
        _execution.run_worker("client", {}, timeout=10)
    if stage == "construct":
        assert not processes
    else:
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed


def test_child_creation_failure_preserves_original_error(monkeypatch):
    """No transport thread starts when the operating system rejects process creation."""
    failure = OSError("process creation unavailable")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(_execution.subprocess, "Popen", fail)
    monkeypatch.setattr(_execution.threading.Thread, "start", lambda *a: pytest.fail("thread"))
    with pytest.raises(OSError, match="process creation unavailable") as caught:
        _execution.run_worker("client", {})
    assert caught.value is failure


@pytest.mark.parametrize("stage", ["write", "partial_write"])
def test_parent_broken_startup_input_still_reaps_child(monkeypatch, stage):
    """Initial pipe failure and a failing close cannot bypass process cleanup."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    original_popen = _execution.subprocess.Popen
    original_write = _execution.os.write
    writes = []

    class BrokenInput:
        """Wrap the actual subprocess pipe and fail one admitted write operation."""

        def __init__(self, stream):
            """Retain the real pipe so closing and cleanup assertions remain meaningful."""
            self.stream = stream

        @property
        def closed(self):
            """Report the real pipe's final resource state."""
            return self.stream.closed

        def fileno(self):
            """Expose the real descriptor used by nonblocking IPC writes."""
            return self.stream.fileno()

        def close(self):
            """Close the real handle before reproducing a buffered pipe close failure."""
            try:
                self.stream.close()
            finally:
                raise BrokenPipeError("close failed")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        process.stdin = BrokenInput(process.stdin)
        return process

    def write(descriptor, data):
        if not processes or descriptor != processes[0].stdin.fileno():
            return original_write(descriptor, data)
        writes.append(len(data))
        if stage == "partial_write" and len(writes) == 1:
            return original_write(descriptor, data[:1])
        raise BrokenPipeError("closed child input")

    monkeypatch.setattr(_execution.subprocess, "Popen", popen)
    monkeypatch.setattr(_execution.os, "write", write)
    with pytest.raises(IperfLibraryError, match="input during startup"):
        _execution.run_worker("client", {})
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed
    assert len(writes) == (2 if stage == "partial_write" else 1)


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_worker_broken_output_frees_native_test_and_joins_writer(worker_native, stage):
    """Broken output propagates explicitly after exactly-once native cleanup."""
    native, threads = worker_native

    class BrokenOutput(io.BytesIO):
        """Fail the selected pipe operation without depending on scheduling."""

        def write(self, value):
            """Raise the same error a broken IPC pipe would report."""
            if stage == "write":
                raise BrokenPipeError("closed consumer")
            return super().write(value)

        def flush(self):
            """Fail flushing independently of a successful write."""
            if stage == "flush":
                raise BrokenPipeError("closed consumer")
            super().flush()

    with pytest.raises(IperfLibraryError, match="output transport failed"):
        execute_worker({"role": "client", "options": {}}, BrokenOutput())
    assert native.freed == native.allocations and len(native.freed) == 1
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("stage", ["allocate", "defaults", "callback", "run", "observe"])
def test_worker_native_failures_close_writer_and_free_only_allocated_tests(
    monkeypatch, worker_native, stage
):
    """Every native-boundary failure has one cleanup owner and a stopped writer."""
    import iperf3_lib.native_options as native_options

    native, threads = worker_native

    def fail(*args, **kwargs):
        raise RuntimeError("native boundary failed")

    if stage == "allocate":
        monkeypatch.setattr(native, "iperf_new_test", lambda: None)
        error = IperfLibraryError
        message = "iperf_new_test failed"
    else:
        error = RuntimeError
        message = "native boundary failed"
        if stage == "observe":
            monkeypatch.setattr(native_options, "observe_native_options", fail)
        else:
            target = {
                "defaults": "iperf_defaults",
                "callback": "iperf_set_test_json_callback",
                "run": "iperf_run_client",
            }[stage]
            monkeypatch.setattr(native, target, fail)
    with pytest.raises(error, match=message):
        execute_worker({"role": "client", "options": {}})
    assert native.freed == native.allocations
    assert len(native.freed) == (0 if stage == "allocate" else 1)
    assert all(not thread.is_alive() for thread in threads)


def test_second_server_setup_failure_frees_both_distinct_tests(monkeypatch, worker_native):
    """A failed later session neither leaks its allocation nor frees the first twice."""
    native, threads = worker_native
    defaults = native.iperf_defaults

    def fail_second(test):
        if len(native.allocations) == 2:
            raise RuntimeError("second native setup failed")
        return defaults(test)

    monkeypatch.setattr(native, "iperf_defaults", fail_second)
    output = io.BytesIO()
    with pytest.raises(RuntimeError, match="second native setup failed"):
        execute_worker(
            {"role": "server", "options": {}},
            output,
            continuation=(True,),
        )
    assert native.freed == native.allocations
    assert len({id(test) for test in native.freed}) == 2
    assert all(not thread.is_alive() for thread in threads)
    messages = worker_messages(output)
    assert [message["type"] for message in messages] == ["ready", "result", "error", "terminal"]
    assert messages[-1]["status"] == "failed" and messages[-1]["runs"] == 1


@pytest.mark.parametrize("complete", [False, True])
def test_worker_stream_provenance_preserves_authoritative_document(
    monkeypatch, worker_native, complete
):
    """Full native JSON wins; event-only capture retains every original envelope."""
    from copy import deepcopy

    native, threads = worker_native
    full = deepcopy(RESULT["raw"])
    full["title"] = "native title"
    full["extra_data"] = "metadata absent from event projections"
    envelopes = [
        {"event": "start", "data": full["start"], "native_extra": 1},
        {"event": "future_extension", "data": {"nested": [1, 2]}},
        {"event": "interval", "data": {"sum": {"bytes": 10}}},
        {"event": "end", "data": full["end"]},
    ]

    def run(test):
        for envelope in envelopes:
            native.callback(test, json.dumps(envelope).encode())
        if complete:
            native.callback(test, json.dumps(full).encode())
        return 0

    monkeypatch.setattr(native, "iperf_run_client", run)
    output = io.BytesIO()
    execute_worker({"role": "client", "options": {}, "events": False}, output)
    messages = worker_messages(output)
    assert [message["type"] for message in messages] == ["ready", "result", "terminal"]
    message = messages[1]
    assert message["events_emitted"] == len(envelopes)
    assert message["events_dropped"] == 0
    assert native.freed == native.allocations
    assert all(not thread.is_alive() for thread in threads)
    result = _execution._decode_result(message, "client")
    if complete:
        assert message["raw_representation"] == "native_complete"
        assert result.raw == full
        assert "iperf3_lib.native_json" not in result.extensions
        assert not any(item.code == "execution.reconstructed_json" for item in result.diagnostics)
    else:
        assert message["raw_representation"] == "reconstructed_events"
        assert message["native_events"] == envelopes
        assert result.extensions["iperf3_lib.native_json"]["events"] == envelopes
        assert result.raw["end"] == full["end"]


def test_unknown_only_native_stream_preserves_events_without_fabricating_result():
    """Unrecognized native output remains inspectable even without a reconstructable start."""
    event = {"event": "future_extension", "data": {"measurement": 4}, "native_extra": True}
    result = _execution._decode_result(
        {
            **RESULT,
            "raw": {},
            "raw_representation": "reconstructed_events",
            "native_events": [event],
        },
        "client",
    )
    assert not result.ok and result.execution.status == "incomplete"
    assert result.raw == {}
    assert result.extensions["iperf3_lib.native_json"]["events"] == [event]


@pytest.mark.parametrize("role", ["client", "server"])
@pytest.mark.parametrize("streaming", [False, True])
def test_copied_authentication_error_survives_cleared_native_error_state(
    monkeypatch, worker_native, role, streaming
):
    """A specific copied failure outranks a cleared process-global native error."""
    native, threads = worker_native
    original_error = "test authorization failed"
    native.i_errno = 0
    monkeypatch.setattr(native, "iperf_strerror", lambda code: b"no error")

    def fail(test):
        payload = (
            {"event": "error", "data": original_error} if streaming else {"error": original_error}
        )
        native.callback(test, json.dumps(payload).encode())
        return -1

    target = "iperf_run_client" if role == "client" else "iperf_run_server"
    monkeypatch.setattr(native, target, fail)
    output = io.BytesIO()
    execute_worker({"role": role, "options": {}}, output, continuation=(False,))
    messages = worker_messages(output)
    assert [message["type"] for message in messages] == ["ready", "result", "terminal"]
    message = messages[1]
    assert message["error"] == message["raw"]["error"] == original_error
    result = _execution._decode_result(message, role)
    assert not result.ok and result.execution.status == "failed"
    assert result.error == result.raw["error"] == original_error
    assert native.freed == native.allocations and len(native.freed) == 1
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["client", "server"])
async def test_async_cancellation_reaps_worker_and_closes_pipes(monkeypatch, role):
    """A cancelled await returns only after the isolated operation is released."""
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    processes = child(
        monkeypatch,
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\ntime.sleep(30)\n",
    )
    instance, call = async_call(role, on_event=lambda event: loop.call_soon_threadsafe(ready.set))
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel("requested cancellation")
        with pytest.raises(asyncio.CancelledError, match="requested cancellation"):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
        if role == "server":
            assert not instance._run_lock.locked()
    finally:
        await finish_child_test(task, processes)


@pytest.mark.asyncio
async def test_cancellation_before_executor_admission_never_runs_operation(
    monkeypatch, loop_error_reports
):
    """Cancellation records intent while executor capacity is unavailable."""
    from iperf3_lib._cancellation import _run_async
    from iperf3_lib.result import Result

    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    release = threading.Event()
    submitted = asyncio.Event()
    calls = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(release.wait, 5)

        def submit(executor, function, *args):
            future = original(pool, function, *args)
            submitted.set()
            return future

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(
            _run_async(lambda control: calls.append(control) or Result(ok=True))
        )
        try:
            await asyncio.wait_for(submitted.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert not blocked.done()
        finally:
            release.set()
        await asyncio.wait_for(asyncio.wrap_future(pool.submit(lambda: None)), timeout=2)
    assert calls == []
    await asyncio.sleep(0)
    assert loop_error_reports == []


@pytest.mark.asyncio
async def test_cancellation_during_spawn_reaps_late_child(monkeypatch):
    """A Popen call returning after cancellation cannot orphan its acquired child."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    spawn = _execution.subprocess.Popen
    loop = asyncio.get_running_loop()
    spawned = asyncio.Event()
    release = threading.Event()

    def delayed_spawn(*args, **kwargs):
        process = spawn(*args, **kwargs)
        loop.call_soon_threadsafe(spawned.set)
        assert release.wait(5)
        return process

    monkeypatch.setattr(_execution.subprocess, "Popen", delayed_spawn)
    _, call = async_call("client")
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        release.set()
        await finish_child_test(task, processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["client", "server"])
async def test_repeated_cancellation_stops_worker_during_blocked_callback(
    monkeypatch, role, loop_error_reports
):
    """Callback quiescence may delay the await, but cannot delay stopping native work."""
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    observed = []
    processes = child(
        monkeypatch,
        "for i in range(2):\n send({'type':'event','kind':'interval','data':{},'sequence':i+1,'time':10})\ntime.sleep(30)\n",
    )

    def callback(event):
        observed.append(event.sequence)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    instance, call = async_call(role, on_event=callback)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        for message in ("first cancellation", "second cancellation", "third cancellation"):
            task.cancel(message)
            await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.to_thread(processes[0].wait, 3), timeout=4)
        assert not task.done()
        assert observed == [1]
        if role == "server":
            assert instance._run_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await asyncio.wait_for(task, timeout=5)
        assert observed == [1]
        assert processes[0].stdin.closed and processes[0].stdout.closed
        if role == "server":
            assert not instance._run_lock.locked()
        await asyncio.sleep(0)
        assert loop_error_reports == []
    finally:
        release.set()
        await finish_child_test(task, processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_cause", ["cancel", "timeout"])
async def test_client_cleanup_error_after_cancellation_is_not_reported_to_loop(
    monkeypatch, first_cause, loop_error_reports
):
    """Expected cleanup failures reach the caller once with their original cause."""
    from iperf3_lib.exceptions import IperfCleanupError

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    cleanup_errors = []

    def fail_cleanup(*args, **kwargs):
        control = kwargs["_control"]
        if first_cause == "timeout":
            control.abort(TimeoutError("initial worker deadline"))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        try:
            control.check()
        except BaseException as cause:
            failure = IperfCleanupError("worker cleanup could not finish", control=control)
            cleanup_errors.append(failure)
            raise failure from cause
        pytest.fail("cancellation did not reach worker control")

    monkeypatch.setattr(_execution, "run_worker", fail_cleanup)
    _, call = async_call("client")
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        for message in ("original cancellation", "repeated cancellation"):
            task.cancel(message)
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(IperfCleanupError, match="worker cleanup") as captured:
            await asyncio.wait_for(task, timeout=5)
        assert captured.value is cleanup_errors[0]
        cause = captured.value.__cause__
        if first_cause == "cancel":
            assert isinstance(cause, asyncio.CancelledError)
            assert str(cause) == "original cancellation"
        else:
            assert isinstance(cause, TimeoutError)
            assert str(cause) == "initial worker deadline"
        await asyncio.sleep(0)
        assert loop_error_reports == []
    finally:
        release.set()
        await finish_child_test(task, [])


def test_deadline_during_child_exit_wait_preserves_timeout(monkeypatch):
    """A deadline during terminal protocol handling keeps its public error class."""
    from iperf3_lib._cancellation import _ExecutionControl

    body = "send(result)\nsend({'type':'done'})\nos.close(sys.stdout.fileno())\ntime.sleep(30)\n"
    processes = child(monkeypatch, body)
    wait_for_exit = _ExecutionControl.wait_for_exit
    entered = []

    def expire_at_wait(control, timeout):
        entered.append(True)
        control.abort(TimeoutError("deterministic process deadline"))
        return wait_for_exit(control, timeout)

    monkeypatch.setattr(_ExecutionControl, "wait_for_exit", expire_at_wait)
    with pytest.raises(TimeoutError, match="process deadline"):
        _execution.run_worker("client", {})
    assert entered
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize("first", ["cancel", "timeout", "complete"])
def test_terminal_cause_arbitration_preserves_first_committed_outcome(first):
    """Completion, cancellation and deadline select one stable operation outcome."""
    from iperf3_lib._cancellation import _ExecutionControl

    control = _ExecutionControl()
    cancelled = asyncio.CancelledError("cancelled first")
    expired = TimeoutError("deadline first")
    if first == "complete":
        control.complete()
        control.request_cancel(cancelled)
        control.abort(expired)
        control.check()
        return
    expected = cancelled if first == "cancel" else expired
    if first == "cancel":
        control.request_cancel(cancelled)
        control.abort(expired)
    else:
        control.abort(expired)
        control.request_cancel(cancelled)
    with pytest.raises(type(expected)) as captured:
        control.complete()
    assert captured.value is expected


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
async def test_cancellation_before_validated_completion_discards_result(monkeypatch):
    """A received result remains provisional until the worker exit is confirmed."""
    from iperf3_lib._cancellation import _ExecutionControl

    processes = child(
        monkeypatch,
        "send(result)\nsend({'type':'done'})\nos.close(sys.stdout.fileno())\ntime.sleep(30)\n",
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    original = _ExecutionControl.wait_for_exit

    def waiting(control, timeout):
        loop.call_soon_threadsafe(entered.set)
        return original(control, timeout)

    monkeypatch.setattr(_ExecutionControl, "wait_for_exit", waiting)
    _, call = async_call("client")
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].poll() is not None
    finally:
        await finish_child_test(task, processes)


@pytest.mark.asyncio
async def test_completed_async_operation_is_not_retroactively_cancelled(monkeypatch):
    """An already delivered successful result is stable under late Task.cancel."""
    processes = child(monkeypatch, "send(result)\nsend({'type':'done'})\n")
    _, call = async_call("client")
    task = asyncio.create_task(call)
    try:
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok
        assert not task.cancel()
        assert task.result() is result
        assert processes[0].poll() == 0
    finally:
        await finish_child_test(task, processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "timeout"])
async def test_task_cancellation_wins_before_executor_delivers_terminal_outcome(
    monkeypatch, outcome
):
    """A reaped worker's outcome cannot swallow cancellation of its pending await."""
    processes = child(
        monkeypatch,
        "send(result)\nsend({'type':'done'})\n" if outcome == "completed" else "time.sleep(30)\n",
    )
    loop = asyncio.get_running_loop()
    terminal_ready = asyncio.Event()
    release = threading.Event()
    outcomes = []
    run_worker = _execution.run_worker

    def hold_terminal_delivery(*args, **kwargs):
        try:
            result = run_worker(*args, **kwargs)
        except TimeoutError as exc:
            outcomes.append(exc)
            raise
        else:
            outcomes.append(result)
            return result
        finally:
            # Hold executor delivery after real protocol/deadline handling and
            # pipe/process cleanup. The event makes cancellation ordering exact.
            loop.call_soon_threadsafe(terminal_ready.set)
            assert release.wait(5)

    monkeypatch.setattr(_execution, "run_worker", hold_terminal_delivery)
    _, call = async_call("client", timeout=0.1 if outcome == "timeout" else None)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(terminal_ready.wait(), timeout=5)
        assert len(outcomes) == 1
        if outcome == "completed":
            assert outcomes[0].ok and processes[0].poll() == 0
        else:
            assert isinstance(outcomes[0], TimeoutError)
            assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
        assert task.cancel("cancel before executor delivery")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel before executor delivery"):
            await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        await finish_child_test(task, processes)


@pytest.mark.parametrize("failure", [False, True])
def test_cleanup_escalation_retains_ownership_until_exit(monkeypatch, failure):
    """Failed kill/reap is explicit and cannot release ownership prematurely."""
    from iperf3_lib._cancellation import _ExecutionControl
    from iperf3_lib.exceptions import IperfCleanupError

    exited = threading.Event()
    reaped = threading.Event()
    calls = []

    class Process:
        """Deterministic process whose final exit remains under test control."""

        def poll(self):
            return 0 if exited.is_set() else None

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")
            if not failure:
                exited.set()

        def wait(self, timeout):
            if exited.wait(min(timeout, 0.01)):
                return 0
            raise subprocess.TimeoutExpired("controlled worker", timeout)

    control = _ExecutionControl()
    process = Process()
    control.register(process)
    control.when_reaped(reaped.set)
    cause = asyncio.CancelledError("cancel requested")
    control.request_cancel(cause)
    try:
        if failure:
            with pytest.raises(IperfCleanupError, match="ownership is retained") as captured:
                control.close()
            assert captured.value.__cause__ is cause
            assert captured.value._control is control
            assert not control.cleanup_confirmed
            assert not reaped.is_set()
        else:
            control.close()
            assert control.cleanup_confirmed and reaped.is_set()
        assert calls == ["terminate", "kill"]
    finally:
        exited.set()
        assert reaped.wait(2)
    assert control.cleanup_confirmed


@pytest.mark.parametrize("stage", ["construct", "start", "after_start"])
def test_supervisor_startup_failure_reaps_acquired_process(monkeypatch, stage):
    """Ownership startup failures retain a cleanup owner even after partial start."""
    from iperf3_lib import _cancellation

    processes = child(monkeypatch, "time.sleep(30)\n")
    original_thread = threading.Thread

    def fail():
        raise RuntimeError("supervisor startup unavailable")

    def create_thread(*args, **kwargs):
        is_supervisor = kwargs["target"].__name__ == "_supervise"
        if is_supervisor and stage == "construct":
            fail()
        thread = original_thread(*args, **kwargs)
        if is_supervisor:
            start = thread.start

            def fail_start():
                if stage == "after_start":
                    start()
                fail()

            monkeypatch.setattr(thread, "start", fail_start)
        return thread

    monkeypatch.setattr(_cancellation.threading, "Thread", create_thread)
    try:
        with pytest.raises(RuntimeError, match="supervisor startup unavailable"):
            _execution.run_worker("client", {})
        assert len(processes) == 1 and processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.asyncio
async def test_cancellation_interrupts_full_startup_input_pipe(monkeypatch):
    """A worker that never reads its request cannot block cancellation on a full pipe."""
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client

    processes = child(monkeypatch, "time.sleep(30)\n", read_request=False)
    loop = asyncio.get_running_loop()
    blocked = asyncio.Event()
    write = os.write

    def observe_write(descriptor, data):
        try:
            return write(descriptor, data)
        except BlockingIOError:
            loop.call_soon_threadsafe(blocked.set)
            raise

    monkeypatch.setattr(_execution.os, "write", observe_write)
    task = asyncio.create_task(Client(ClientConfig("127.0.0.1", extra_data="x" * 1_000_000)).arun())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        await finish_child_test(task, processes)


def test_terminal_worker_with_retained_stdout_writer_fails_closed_and_cleans_up(monkeypatch):
    """A terminal receipt without clean EOF cannot qualify a provisional result."""
    processes = child(monkeypatch, "send(result)\nsend({'type':'done'})\n")
    original_popen = _execution.subprocess.Popen
    read_descriptor, retained_writer = os.pipe()
    reader = os.fdopen(read_descriptor, "rb", buffering=0)
    relays = []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        original_output = process.stdout
        process.stdout = reader

        # Keeping this OS pipe's writer open models a descriptor inherited by
        # another process. EOF cannot unblock a buffered reader during cleanup.
        def relay():
            with original_output:
                data = memoryview(original_output.buffer.read())
            while data:
                data = data[os.write(retained_writer, data) :]

        thread = threading.Thread(target=relay)
        relays.append(thread)
        thread.start()
        return process

    monkeypatch.setattr(_execution.subprocess, "Popen", popen)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execution.run_worker, "client", {})
        try:
            with pytest.raises(IperfLibraryError, match="EOF|terminal|output"):
                future.result(timeout=8)
            assert processes[0].poll() == 0
            assert processes[0].stdin.closed and reader.closed
            os.fstat(retained_writer)
        finally:
            os.close(retained_writer)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            reader.close()
            for thread in relays:
                thread.join(timeout=2)
                assert not thread.is_alive()
