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
        SimpleNamespace(NULL=None, string=lambda value: value, callback=lambda signature, fn: fn),
    )
    monkeypatch.setattr(native_options, "configure_native", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_options, "observe_native_options", lambda *args: {})
    return state


def _request(*, role="server", events=False):
    session = _ipc.Session.new()
    request = _ipc.FrameDecoder().feed(
        session.emit(
            {
                "type": "request",
                "role": role,
                "options": {},
                "max_runs": 2 if role == "server" else 1,
                "events": events,
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


def test_oversized_final_result_frees_test_and_sends_failed_terminal(native):
    """An untransportable final JSON is an explicit failure after native cleanup."""
    session, request = _request(role="client")
    native.payloads = [{"oversized": "x" * _ipc.MAX_FRAME_BYTES}]
    output = io.BytesIO()
    with pytest.raises(_ipc.IPCError, match="byte limit"):
        _worker.execute(request, output, io.BytesIO())
    messages = _messages(output)
    assert [item["type"] for item in messages] == ["ready", "error", "terminal"]
    assert messages[-1]["runs"] == 0
    assert messages[-1]["status"] == "failed"
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
