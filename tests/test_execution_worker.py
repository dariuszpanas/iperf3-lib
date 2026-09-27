"""Process lifecycle, bounded event delivery and native ownership regression tests."""

import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from iperf3_lib import _execution, _worker
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


def child(monkeypatch, body):
    """Replace only the child program, retaining actual pipe/process behavior."""
    original = subprocess.Popen
    processes = []
    program = (
        "import json,sys,time\nrequest=json.loads(sys.stdin.readline())\n"
        f"result={RESULT!r}\n"
        "def send(message):\n print(json.dumps(message),flush=True)\n" + body
    )

    def popen(args, **kwargs):
        assert args == [sys.executable, "-m", "iperf3_lib._worker"]
        process = original([sys.executable, "-u", "-c", program], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(_execution.subprocess, "Popen", popen)
    return processes


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
    with pytest.raises(IperfLibraryError, match="ended without"):
        _execution.run_worker("client", {})
    assert processes[0].poll() is not None


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
    child(
        monkeypatch,
        f"send({{'type':'error','class':{name!r},'message':'unavailable'}})\nsend({{'type':'done'}})\n",
    )
    with pytest.raises(expected, match="unavailable"):
        _execution.run_worker("client", {})


def test_callback_exception_waits_for_active_native_completion(monkeypatch):
    """Callback failures are raised after the child completes and is reaped."""
    processes = child(
        monkeypatch,
        "send({'type':'event','kind':'start','data':{},'sequence':1,'time':10})\ntime.sleep(.05)\nsend(result)\nsend({'type':'done'})\n",
    )

    def callback(_):
        raise LookupError("consumer failure")

    with pytest.raises(LookupError, match="consumer failure"):
        _execution.run_worker("client", {}, on_event=callback)
    assert processes[0].poll() == 0


def test_slow_callback_has_bounded_delivery_and_explicit_drops(monkeypatch):
    """Event drops preserve the final result and retain monotonic sequence numbers."""
    child(
        monkeypatch,
        "for i in range(3000):\n send({'type':'event','kind':'interval','data':{},'sequence':i+1,'time':10})\nresult['events_emitted']=3000\nsend(result)\nsend({'type':'done'})\n",
    )
    seen = []

    def callback(event):
        seen.append(event.sequence)
        if len(seen) == 1:
            time.sleep(0.2)

    result = _execution.run_worker("client", {}, on_event=callback)
    assert result.ok and seen == sorted(set(seen))
    delivery = result.extensions["iperf3_lib.event_delivery"]
    assert delivery["dropped"] > 0
    assert len(seen) + delivery["dropped"] == 3000


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
            NULL=None, string=lambda value: value, callback=lambda signature, callback: callback
        ),
    )
    monkeypatch.setattr(
        native_options, "configure_native", lambda *a, **k: SimpleNamespace(evidence={})
    )
    monkeypatch.setattr(native_options, "observe_native_options", lambda *a: {})
    output = io.StringIO()
    _worker.execute(
        {"role": role, "options": {}, "events": True},
        output,
        io.StringIO('{"continue":true}\n{"continue":false}\n'),
    )
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    results = [item for item in messages if item["type"] == "result"]
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
        _worker.execute({"role": "client", "options": {}}, io.StringIO(), io.StringIO())
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
            NULL=None, string=lambda value: value, callback=lambda signature, callback: callback
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
        _worker.execute({"role": "client", "options": {}}, io.StringIO(), io.StringIO())
    assert native.allocations == native.freed == []
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("stage", ["construct", "start"])
def test_parent_reader_startup_failure_reaps_child_and_closes_pipes(monkeypatch, stage):
    """Host thread resource exhaustion still closes and reaps the started process."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    original_thread = _execution.threading.Thread

    def fail(*args, **kwargs):
        raise RuntimeError("reader startup unavailable")

    if stage == "construct":
        monkeypatch.setattr(_execution.threading, "Thread", fail)
    else:

        def fail_start(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            monkeypatch.setattr(thread, "start", fail)
            return thread

        monkeypatch.setattr(_execution.threading, "Thread", fail_start)
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


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_parent_broken_startup_input_still_reaps_child(monkeypatch, stage):
    """Initial pipe failure and a failing close cannot bypass process cleanup."""
    processes = child(monkeypatch, "time.sleep(30)\n")
    original_popen = _execution.subprocess.Popen

    class BrokenInput:
        """Wrap the actual subprocess pipe and fail one admitted write operation."""

        def __init__(self, stream):
            """Retain the real pipe so closing and cleanup assertions remain meaningful."""
            self.stream = stream

        @property
        def closed(self):
            """Report the real pipe's final resource state."""
            return self.stream.closed

        def write(self, value):
            """Fail writes before data reaches the child's input."""
            if stage == "write":
                raise BrokenPipeError("closed child input")
            return self.stream.write(value)

        def flush(self):
            """Raise a flush failure independently from writing the request."""
            raise BrokenPipeError("closed child input")

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

    monkeypatch.setattr(_execution.subprocess, "Popen", popen)
    with pytest.raises(IperfLibraryError, match="input during startup"):
        _execution.run_worker("client", {})
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_worker_broken_output_frees_native_test_and_joins_writer(worker_native, stage):
    """Broken output propagates explicitly after exactly-once native cleanup."""
    native, threads = worker_native

    class BrokenOutput(io.StringIO):
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
        _worker.execute({"role": "client", "options": {}}, BrokenOutput(), io.StringIO())
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
        _worker.execute({"role": "client", "options": {}}, io.StringIO(), io.StringIO())
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
    output = io.StringIO()
    with pytest.raises(RuntimeError, match="second native setup failed"):
        _worker.execute(
            {"role": "server", "options": {}},
            output,
            io.StringIO('{"continue":true}\n'),
        )
    assert native.freed == native.allocations
    assert len({id(test) for test in native.freed}) == 2
    assert all(not thread.is_alive() for thread in threads)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 1 and messages[0]["type"] == "result"


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
    output = io.StringIO()
    _worker.execute({"role": "client", "options": {}, "events": False}, output, io.StringIO())
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 1
    message = messages[0]
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
    output = io.StringIO()
    _worker.execute({"role": role, "options": {}}, output, io.StringIO())
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 1
    message = messages[0]
    assert message["error"] == message["raw"]["error"] == original_error
    result = _execution._decode_result(message, role)
    assert not result.ok and result.execution.status == "failed"
    assert result.error == result.raw["error"] == original_error
    assert native.freed == native.allocations and len(native.freed) == 1
    assert all(not thread.is_alive() for thread in threads)
