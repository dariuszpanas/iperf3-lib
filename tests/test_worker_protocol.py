"""Worker wire failures preserve native ownership and trustworthy terminal receipts."""

import io
import json
import os
from types import SimpleNamespace

import pytest

from iperf3_lib import _execution, _ipc, _worker, native_options
from iperf3_lib.exceptions import IperfLibraryError


@pytest.fixture
def native(monkeypatch):
    """Provide native-owned tokens and copied JSON without loading libiperf."""
    state = SimpleNamespace(allocated=[], freed=[], callbacks={}, runs=0, payloads=[])

    def allocate():
        test = object()
        state.allocated.append(test)
        return test

    def run(test):
        state.runs += 1
        for payload in state.payloads:
            state.callbacks[test](test, json.dumps(payload).encode())
        return 0

    library = SimpleNamespace(
        iperf_new_test=allocate,
        iperf_defaults=lambda _: 0,
        iperf_free_test=state.freed.append,
        iperf_get_iperf_version=lambda: b"iperf 3.21",
        iperf_set_test_json_callback=lambda test, callback: state.callbacks.__setitem__(
            test, callback
        ),
        iperf_run_client=run,
        iperf_run_server=run,
        i_errno=0,
    )
    state.library = library
    monkeypatch.setattr(_worker, "lib", library)
    monkeypatch.setattr(
        _worker,
        "ffi",
        SimpleNamespace(
            NULL=None,
            string=lambda value, maximum=None: value[:maximum],
            callback=lambda signature, fn: fn,
        ),
    )
    monkeypatch.setattr(native_options, "configure_native", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_options, "observe_native_options", lambda *args: {})
    return state


def _request(*, role="server", events=False, live_events=False):
    session = _ipc.Session.new()
    request = _ipc.FrameDecoder().feed(
        session.emit(
            {
                "type": "request",
                "role": role,
                "options": {},
                "max_runs": 2 if role == "server" else 1,
                "events": events,
                "live_events": live_events,
            }
        )
    )[0]
    return session, request


def _messages(output):
    decoder = _ipc.FrameDecoder()
    messages = decoder.feed(output.getvalue())
    decoder.eof()
    return messages


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "0" * 32},
        {"worker_id": "0" * 32},
        {"transport_sequence": 1},
        {"transport_sequence": 3},
        {"run_index": 2},
        {"continue": 1},
        {"continue": "true"},
        {"type": "request"},
    ],
    ids=["request-id", "worker-id", "replay", "gap", "run", "integer", "string", "type"],
)
def test_invalid_continuation_never_allocates_next_native_run(native, change):
    """Malformed commands produce a failed receipt after the completed test is freed."""
    session, request = _request()
    command = _ipc.FrameDecoder().feed(session.emit({"type": "continue", "continue": True}))[0]
    output = io.BytesIO()
    with pytest.raises(_ipc.IPCError):
        _worker.execute(request, output, io.BytesIO(_ipc.encode_frame({**command, **change})))
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["ready", "result", "error", "terminal"]
    assert [item["transport_sequence"] for item in messages] == [1, 2, 3, 4]
    assert messages[-1]["runs"] == 1
    assert messages[-1]["run_index"] == 1
    assert messages[-1]["status"] == "failed"
    assert native.runs == 1
    assert len(native.allocated) == 1
    assert native.freed == native.allocated


def test_initial_setup_failure_has_no_readiness_and_frees_before_error(native, monkeypatch):
    """Readiness proves configuration and callback setup succeeded, not just allocation."""
    session, request = _request(role="client")

    def configure(*args, **kwargs):
        raise ValueError("configuration failed")

    monkeypatch.setattr(native_options, "configure_native", configure)
    output = io.BytesIO()
    with pytest.raises(ValueError, match="configuration failed"):
        _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["error", "terminal"]
    assert native.runs == 0
    assert len(native.allocated) == 1
    assert native.freed == native.allocated
    responses = _execution._Responses(session, "client", os.getpid())
    for message in messages:
        responses.accept(message)
    assert responses.phase == "terminal"
    assert messages[-1]["runs"] == 0


@pytest.mark.parametrize("stage", ["allocate", "defaults"])
def test_second_run_setup_failure_identifies_authorized_run(native, monkeypatch, stage):
    """A failed next allocation has run 2 identity but only one completed result."""
    session, request = _request()
    original = native.library.iperf_new_test
    if stage == "allocate":
        monkeypatch.setattr(
            native.library, "iperf_new_test", lambda: original() if not native.allocated else None
        )
    else:

        def defaults(test):
            if len(native.allocated) == 2:
                raise IperfLibraryError("second defaults failed")
            return 0

        monkeypatch.setattr(native.library, "iperf_defaults", defaults)
    commands = io.BytesIO(session.emit({"type": "continue", "continue": True}))
    output = io.BytesIO()
    with pytest.raises(IperfLibraryError):
        _worker.execute(request, output, commands)
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["ready", "result", "error", "terminal"]
    assert [item["run_index"] for item in messages] == [1, 1, 2, 2]
    assert messages[-1]["runs"] == 1
    assert native.freed == native.allocated
    assert len(native.freed) == (1 if stage == "allocate" else 2)
    responses = _execution._Responses(session, "server", os.getpid())
    for message in messages:
        responses.accept(message)
        if message["type"] == "result":
            assert responses.continue_run(True) == 1
    assert responses.phase == "terminal"


def test_oversized_final_result_frees_test_and_retains_capture_failure(native):
    """A too-large native document becomes bounded incomplete evidence after cleanup."""
    session, request = _request(role="client")
    native.payloads = [{"oversized": "x" * _ipc.MAX_FRAME_BYTES}]
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["ready", "result", "terminal"]
    assert messages[1]["raw"] == {}
    assert messages[1]["capture"]["capture_dropped"] == 1
    assert not messages[1]["capture"]["reconstruction_complete"]
    assert messages[-1]["runs"] == 1
    assert messages[-1]["status"] == "completed"
    assert native.freed == native.allocated
    assert len(native.freed) == 1
    responses = _execution._Responses(session, "client", os.getpid())
    for message in messages:
        responses.accept(message)
    assert responses.phase == "terminal"


def test_oversized_event_drops_delivery_without_corrupting_result_or_wire_sequence(native):
    """Native sequence records loss while wire sequence remains contiguous and valid."""
    session, request = _request(role="client", events=True)
    native.payloads = [
        {"event": "interval", "data": {"oversized": "x" * _ipc.MAX_EVENT_BYTES}},
        {"event": "end", "data": {"sum_sent": {"bytes": 10}}},
    ]
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["ready", "event", "result", "terminal"]
    assert [item["transport_sequence"] for item in messages] == [1, 2, 3, 4]
    assert messages[1]["sequence"] == 2
    result = messages[2]
    assert result["events_emitted"] == 2
    assert result["events_dropped"] == 1
    assert result["error"] is None
    assert len(result["raw"]["intervals"][0]["oversized"]) == _ipc.MAX_EVENT_BYTES
    assert messages[-1]["status"] == "completed"
    assert native.freed == native.allocated
    responses = _execution._Responses(session, "client", os.getpid())
    for message in messages:
        responses.accept(message)
    assert responses.phase == "terminal"


def test_worker_typed_events_have_capture_identity_without_changing_legacy(native):
    """Wrapped callbacks and complete documents retain separate native/legacy sequences."""
    _, request = _request(role="client", events=True, live_events=True)
    native.payloads = [
        {"event": "start", "data": {}},
        {"event": "future", "data": {"new": 1}},
        {"start": {}, "end": {}},
    ]
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    legacy = [value for value in messages if value["type"] == "event"]
    typed = [value for value in messages if value["type"] == "live_event"]
    result = next(value for value in messages if value["type"] == "result")
    assert [value["sequence"] for value in legacy] == [1, 2]
    assert [value["capture_sequence"] for value in typed] == [1, 2, 3]
    assert [value["kind"] for value in typed] == ["native_start", "unknown", "native_document"]
    assert result["events_emitted"] == 2
    assert result["capture"]["live_emitted"] == 3
    assert all(value["run_index"] == 1 for value in typed)
    assert all(value["request_id"] == request["request_id"] for value in typed)
    assert messages[-2]["type"] == "result" and messages[-1]["type"] == "terminal"


@pytest.mark.parametrize("getter", [False, True])
def test_worker_getter_recovers_queue_loss_only_when_independent_document_exists(
    native, monkeypatch, getter
):
    """The minimum-version lossy projection remains distinguishable from full native JSON."""
    original = _worker.EventCapture

    class DeferredCapture(original):
        def start(self):
            """Delay parsing so saturation does not depend on thread scheduling."""

        def close(self):
            """Drain after native free, before publishing any final receipt."""
            if self._thread is None:
                super().start()
            super().close()

    monkeypatch.setattr(
        _worker,
        "EventCapture",
        lambda *args, **kwargs: DeferredCapture(*args, pending_items=1, **kwargs),
    )
    _, request = _request(role="client", live_events=True)
    native.payloads = [{"event": "start", "data": {}}, {"event": "end", "data": {}}]
    complete = {"start": {}, "end": {"sum_sent": {"bytes": 10, "seconds": 1}}}

    def document(test):
        assert test not in native.freed
        return json.dumps(complete).encode()

    if getter:
        native.library.iperf_get_test_json_output_string = document
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    result = next(value for value in messages if value["type"] == "result")
    assert result["capture"]["capture_dropped"] == 1
    assert not result["capture"]["reconstruction_complete"]
    assert result["capture"]["complete_document"] is getter
    assert result["raw"] == (complete if getter else {"start": {}})
    assert result["raw_representation"] == ("native_complete" if getter else "reconstructed_events")
    assert native.freed == native.allocated


def test_worker_callback_remains_live_through_free_and_parser_settles_before_result(
    native, monkeypatch
):
    """The native free boundary cannot outlive callback ownership or race final frames."""
    _, request = _request(role="client", events=True)
    native.payloads = [{"event": "start", "data": {}}]

    def free(test):
        native.callbacks[test](test, json.dumps({"event": "end", "data": {}}).encode())
        native.freed.append(test)

    monkeypatch.setattr(native.library, "iperf_free_test", free)
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert [value["type"] for value in messages] == [
        "ready",
        "event",
        "event",
        "result",
        "terminal",
    ]
    assert messages[-2]["raw"] == {"start": {}, "end": {}}
    assert native.freed == native.allocated


def test_worker_native_failure_and_error_end_order_remain_failed(native, monkeypatch):
    """Late native end cannot replace the original failed-call status or error detail."""
    _, request = _request(role="client", live_events=True)
    native.payloads = [
        {"event": "error", "data": "actual refused connection"},
        {"event": "end", "data": {}},
    ]
    original = native.library.iperf_run_client

    def run(test):
        original(test)
        return -1

    monkeypatch.setattr(native.library, "iperf_run_client", run)
    native.library.iperf_strerror = lambda code: b"generic native error"
    native.library.i_errno = 103
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    result = next(value for value in _messages(output) if value["type"] == "result")
    assert result["error"] == "actual refused connection"
    assert result["native_returncode"] == -1 and result["native_error_code"] == 103
    assert native.freed == native.allocated


@pytest.mark.parametrize("source", ["envelope", "document"])
@pytest.mark.parametrize("replacement", ["getter", "callback"])
def test_independent_document_never_erases_an_observed_native_error(native, source, replacement):
    """Native error evidence survives even a zero return and an error-free final document."""
    _, request = _request(role="client")
    native.payloads = (
        [{"event": "error", "data": "observed native failure"}, {"event": "end", "data": {}}]
        if source == "envelope"
        else [{"start": {}, "end": {}, "error": "observed native failure"}]
    )
    if replacement == "getter":
        native.library.iperf_get_test_json_output_string = lambda test: b'{"start":{},"end":{}}'
    else:
        native.payloads.append({"start": {}, "end": {}})
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    result = next(value for value in _messages(output) if value["type"] == "result")
    assert result["capture"]["complete_document_source"] == replacement
    assert result["error"] == "observed native failure"
    assert result["native_returncode"] == 0
    assert "error" not in result["raw"]
    normalized = _execution._decode_result(result, "client")
    assert not normalized.ok and normalized.execution.status == "failed"
    assert normalized.error == "observed native failure"


def test_worker_capture_is_fresh_for_each_sequential_server_run(native):
    """Native and legacy counters reset while validated run identity advances."""
    session, request = _request(events=True, live_events=True)
    native.payloads = [{"event": "start", "data": {}}, {"event": "end", "data": {}}]
    commands = session.emit({"type": "continue", "continue": True}) + session.emit(
        {"type": "continue", "continue": False}, run_index=2
    )
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO(commands))
    messages = _messages(output)
    results = [value for value in messages if value["type"] == "result"]
    assert len(results) == 2
    assert [value["capture"]["callbacks"] for value in results] == [2, 2]
    for run in (1, 2):
        typed = [
            value
            for value in messages
            if value["type"] == "live_event" and value["run_index"] == run
        ]
        assert [value["capture_sequence"] for value in typed] == [1, 2]
    assert native.freed == native.allocated and len(native.freed) == 2


@pytest.mark.parametrize("stage", ["construct", "start", "consume"])
def test_worker_parser_failures_free_and_settle_before_error_terminal(native, monkeypatch, stage):
    """Parser startup/runtime failure cannot leak native state or trail terminal frames."""
    _, request = _request(role="client")
    native.payloads = [{"event": "end", "data": {}}]
    original = _worker.EventCapture
    if stage == "construct":

        def factory(*args, **kwargs):
            raise RuntimeError("parser creation failed")

        monkeypatch.setattr(_worker, "EventCapture", factory)
    else:

        def fail(*args):
            raise RuntimeError("parser failed")

        monkeypatch.setattr(original, "start" if stage == "start" else "_consume", fail)
    output = io.BytesIO()
    with pytest.raises((RuntimeError, IperfLibraryError), match="parser"):
        _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert messages[-2]["type"] == "error" and messages[-1]["type"] == "terminal"
    assert not any(value["type"] == "result" for value in messages)
    assert native.freed == native.allocated and len(native.freed) == 1


def test_unsettled_parser_cannot_race_error_or_terminal_publication(native, monkeypatch):
    """An unjoined parser forces incomplete transport rather than an untrustworthy terminal."""
    import threading

    entered, release = threading.Event(), threading.Event()
    owned = []
    original_start = _worker.EventCapture.start

    def start(capture):
        original_start(capture)
        owned.append((capture, capture._thread.join))
        monkeypatch.setattr(capture._thread, "join", lambda timeout=None: None)

    def blocked(*args):
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(_worker.EventCapture, "start", start)
    monkeypatch.setattr(_worker.EventCapture, "_consume", blocked)
    native.payloads = [{"event": "end", "data": {}}]
    original_run = native.library.iperf_run_client

    def run(test):
        original_run(test)
        assert entered.wait(timeout=2)
        return 0

    monkeypatch.setattr(native.library, "iperf_run_client", run)
    _, request = _request(role="client")
    output = io.BytesIO()
    try:
        with pytest.raises(IperfLibraryError, match="parser did not stop"):
            _worker.execute(request, output, io.BytesIO())
        assert [value["type"] for value in _messages(output)] == ["ready"]
        assert native.freed == native.allocated
    finally:
        release.set()
        for capture, join in owned:
            join(timeout=2)
            assert capture.settled


@pytest.mark.parametrize("failure", ["exception", "oversize"])
def test_failed_independent_getter_does_not_discard_valid_callback_document(native, failure):
    """Separate getter diagnostics cannot erase bounded native-complete callback evidence."""
    _, request = _request(role="client")
    native.payloads = [{"start": {}, "end": {}}]

    def getter(test):
        if failure == "exception":
            raise RuntimeError("getter unavailable")
        return b"x" * (_worker.BoundedDocumentCapture(None).limit + 1)

    native.library.iperf_get_test_json_output_string = getter
    output = io.BytesIO()
    _worker.execute(request, output, io.BytesIO())
    result = next(value for value in _messages(output) if value["type"] == "result")
    assert result["raw"] == {"start": {}, "end": {}}
    assert result["capture"]["complete_document_source"] == "callback"
    assert result["capture"]["getter_error"]
