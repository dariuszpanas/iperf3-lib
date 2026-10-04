"""Native callback copy bounds, parser ownership and loss-aware reconstruction."""

import json
import threading
from types import SimpleNamespace

import pytest

from iperf3_lib import _event_capture, _ipc
from iperf3_lib._event_capture import BoundedDocumentCapture, EventCapture
from iperf3_lib.exceptions import IperfLibraryError

# The server start shape captured from native libiperf 3.19.1, including its
# identical target_bitrate fields before sock_bufsize and after tcp_mss_default.
_SERVER_START = (
    b'{"connected":[{"socket":6,"local_host":"127.0.0.1","local_port":44195,'
    b'"remote_host":"127.0.0.1","remote_port":60162},{"socket":9,'
    b'"local_host":"127.0.0.1","local_port":44195,"remote_host":"127.0.0.1",'
    b'"remote_port":60172}],"version":"iperf 3.19.1",'
    b'"system_info":"Linux ad871534c54f 7.0.12-linuxkit #1 SMP PREEMPT_DYNAMIC '
    b'Thu Aug 27 10:55:53 UTC 2026 x86_64","target_bitrate":500000,"sock_bufsize":0,'
    b'"sndbuf_actual":16384,"rcvbuf_actual":131072,"timestamp":'
    b'{"time":"Sun, 04 Oct 2026 07:55:31 GMT","timesecs":1791100531},'
    b'"accepted_connection":{"host":"127.0.0.1","port":60146},'
    b'"cookie":"elt26qjoboqrprucpsj4uh4czi5hrdi267d5","tcp_mss_default":0,'
    b'"target_bitrate":500000,"fq_rate":0,"test_start":{"protocol":"TCP",'
    b'"num_streams":2,"blksize":1200,"omit":0,"duration":1,"bytes":0,"blocks":0,'
    b'"reverse":0,"tos":0,"target_bitrate":500000,"bidir":0,"fqrate":0,"interval":0.25}}'
)


def encoded(kind, data):
    """Serialize one synthetic native envelope before entering the callback."""
    return json.dumps({"event": kind, "data": data}).encode()


def collector(*, emit=None, **kwargs):
    """Create a bounded byte-pointer stand-in and capture emitted worker messages."""
    messages = []
    ffi = SimpleNamespace(NULL=None, string=lambda pointer, maximum: pointer[:maximum])

    def deliver(message):
        messages.append(message)
        return True

    return EventCapture(ffi, emit or deliver, **kwargs), messages


def run_capture(payloads, **kwargs):
    """Deterministically fill admission before starting its parser consumer."""
    capture, messages = collector(**kwargs)
    for payload in payloads:
        capture.capture(None, payload)
    capture.start()
    capture.close()
    return capture, messages


@pytest.mark.parametrize("representation", ["stream", "document"])
def test_native_server_start_accepts_its_identical_duplicate_rate(representation):
    """Observed native start duplication is unambiguous in both native representations."""
    payload = (
        b'{"event":"start","data":' + _SERVER_START + b"}"
        if representation == "stream"
        else b'{"start":' + _SERVER_START + b',"intervals":[],"end":{}}'
    )
    capture, messages = run_capture([payload], live_events=True)
    assert capture.malformed == 0
    assert capture.raw["start"]["target_bitrate"] == 500000
    assert capture.raw["start"]["test_start"]["target_bitrate"] == 500000
    assert messages[0]["kind"] == (
        "native_start" if representation == "stream" else "native_document"
    )
    direct = BoundedDocumentCapture(capture.ffi)
    direct.capture(None, payload)
    direct.finalize()
    assert direct.error is None
    assert direct.parse_document(payload) == json.loads(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"event":"start","data":'
        + _SERVER_START.replace(b'"target_bitrate":500000', b'"target_bitrate":400000', 1)
        + b"}",
        b'{"start":{"target_bitrate":1,"target_bitrate":1,"target_bitrate":1}}',
        b'{"start":{"target_bitrate":1.0,"target_bitrate":1.0}}',
        b'{"start":{"target_bitrate":true,"target_bitrate":true}}',
        b'{"start":{"target_bitrate":-1,"target_bitrate":-1}}',
        b'{"target_bitrate":1,"target_bitrate":1}',
        b'{"start":{"test_start":{"target_bitrate":1,"target_bitrate":1}}}',
        b'{"event":"interval","data":{"target_bitrate":1,"target_bitrate":1}}',
        b'{"start":{"sock_bufsize":0,"sock_bufsize":0}}',
    ],
    ids=[
        "conflicting",
        "third",
        "float",
        "bool",
        "negative",
        "root",
        "nested",
        "interval",
        "other",
    ],
)
def test_native_server_duplicate_exception_remains_path_and_value_specific(payload):
    """Conflicting values, other fields, other paths and repeated duplicates stay invalid."""
    capture, messages = run_capture([payload], live_events=True)
    assert capture.malformed == 1 and capture.raw == {}
    assert messages[0]["kind"] == "malformed"
    direct = BoundedDocumentCapture(capture.ffi)
    with pytest.raises(ValueError, match="duplicate native JSON field"):
        direct.parse_document(payload)


def test_callback_copies_only_and_parser_assembles_on_another_thread(monkeypatch):
    """Neither JSON parsing nor encoded-message delivery occurs in a C callback."""
    owner = threading.get_ident()
    original = json.loads
    parsing_threads = []
    delivery_threads = []
    payloads = [
        encoded("start", {"test_start": {"protocol": "TCP"}}),
        encoded("interval", {"sum": {"bytes": 10}}),
        encoded("end", {"sum_sent": {"bytes": 10}}),
    ]

    def loads(*args, **kwargs):
        parsing_threads.append(threading.get_ident())
        assert threading.get_ident() != owner
        return original(*args, **kwargs)

    def emit(message):
        delivery_threads.append(threading.get_ident())
        assert threading.get_ident() != owner
        _ipc.encode_frame(message)
        return True

    monkeypatch.setattr(_event_capture.json, "loads", loads)
    capture, _ = run_capture(payloads, emit=emit, legacy_events=True)
    assert len(parsing_threads) == len(delivery_threads) == 3
    assert capture.raw["intervals"][0]["sum"]["bytes"] == 10
    assert capture.metadata()["reconstruction_complete"]
    assert not capture._thread.is_alive()


def test_native_callback_sequence_and_legacy_envelopes_stay_separate():
    """A complete document is typed evidence but does not consume a legacy sequence."""
    payloads = [
        encoded("start", {}),
        encoded("interval", {"sum": {"bytes": 10}}),
        json.dumps({"start": {}, "end": {}}).encode(),
    ]
    capture, messages = run_capture(payloads, legacy_events=True, live_events=True)
    legacy = [value for value in messages if value["type"] == "event"]
    typed = [value for value in messages if value["type"] == "live_event"]
    assert [value["sequence"] for value in legacy] == [1, 2]
    assert [value["capture_sequence"] for value in typed] == [1, 2, 3]
    assert [value["kind"] for value in typed] == ["native_start", "interval", "native_document"]
    assert all(value["arrival_offset_seconds"] >= 0 and value["time"] > 0 for value in typed)
    assert capture.metadata()["complete_document_source"] == "callback"
    assert capture.native_events == []
    assert capture.events_emitted == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"bad json",
        b"\xff",
        b"[]",
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b'{"x":1,"x":2}',
        b'{"event":3,"data":{}}',
        b'{"x":"\\ud800"}',
        b'{"x":' + b"[" * 70 + b"0" + b"]" * 70 + b"}",
    ],
)
def test_malformed_native_bytes_are_bounded_diagnostics_without_legacy_events(payload):
    """Invalid parser input cannot masquerade as a native measurement or completed capture."""
    capture, messages = run_capture([payload], legacy_events=True, live_events=True)
    assert capture.raw == {} and capture.native_events == []
    assert capture.events_emitted == 0
    assert capture.metadata()["malformed"] == 1
    assert not capture.metadata()["reconstruction_complete"]
    assert len(messages) == 1 and messages[0]["kind"] == "malformed"
    assert len(messages[0]["data"]["sample"]) <= 256


def test_malformed_known_payload_keeps_legacy_data_but_not_a_typed_measurement():
    """A recognized kind with an invalid shape becomes an explicit typed diagnostic."""
    capture, messages = run_capture([encoded("interval", 7)], legacy_events=True, live_events=True)
    assert messages[0]["kind"] == "interval" and messages[0]["data"] == 7
    assert messages[1]["kind"] == "malformed"
    assert capture.malformed == 1 and capture.raw == {}


@pytest.mark.parametrize("setting", [{"pending_items": 1}, {"pending_bytes": 40}])
def test_capture_queue_saturation_has_counted_gaps_and_preserves_retained_prefix(setting):
    """Count and byte saturation reject admission without waiting for the parser."""
    payloads = [encoded("start", {})] * 3
    capture, messages = run_capture(payloads, live_events=True, **setting)
    metadata = capture.metadata()
    assert metadata["callbacks"] == 3
    assert metadata["copied"] == 1
    assert metadata["capture_dropped"] == 2
    assert not metadata["reconstruction_complete"]
    assert messages[-1]["kind"] == "delivery_gap"
    assert messages[-1]["data"] == {"stage": "capture", "dropped": 2}
    assert messages[-1]["capture_sequence"] is None
    assert capture._pending_bytes == 0


def test_payload_copy_scans_only_the_bound_plus_one_byte():
    """An oversized callback is rejected before parsing or retaining its contents."""
    limits = []
    capture, messages = collector(capture_bytes=16, live_events=True)

    def copy(pointer, maximum):
        limits.append(maximum)
        return pointer[:maximum]

    capture.ffi.string = copy
    capture.capture(None, b"x" * 10000)
    capture.start()
    capture.close()
    assert limits == [17]
    assert capture.copied == 0 and capture.capture_dropped == 1
    assert len(messages) == 1 and messages[0]["kind"] == "delivery_gap"


@pytest.mark.parametrize("failure", [MemoryError, RuntimeError, KeyboardInterrupt, SystemExit])
def test_copy_failures_never_escape_c_callback(failure):
    """Even process-control exceptions are recorded instead of crossing the C boundary."""
    capture, _ = collector()

    def copy(*args):
        raise failure("copy failed")

    capture.ffi.string = copy
    capture.capture(None, b"payload")
    capture.start()
    capture.close()
    assert capture.callbacks == capture.capture_dropped == 1
    assert capture.copied == 0
    assert failure.__name__ in capture.diagnostics[0]


def test_null_copy_and_repeated_malformed_input_keep_diagnostics_bounded():
    """Fixed diagnostic samples cannot grow with native callback failure count."""
    capture, _ = collector()
    for _ in range(100):
        capture.capture(None, None)
    capture.start()
    capture.close()
    assert capture.capture_dropped == capture.callbacks == 100
    assert len(capture.diagnostics) == _event_capture.MAX_DIAGNOSTICS
    assert all(len(value) <= 256 for value in capture.diagnostics)


def test_retention_budget_counts_projection_and_original_native_envelopes():
    """A retained end after a rejected large interval does not repair missing evidence."""
    payloads = [
        encoded("start", {}),
        encoded("interval", {"unicode": '漢\\"' * 1000}),
        encoded("end", {"sum_received": {"bytes": 10}}),
    ]
    capture, messages = run_capture(payloads, retained_bytes=800, live_events=True)
    assert capture.raw == {"start": {}, "end": {"sum_received": {"bytes": 10}}}
    metadata = capture.metadata()
    assert metadata["retention_dropped"] == 1
    assert not metadata["reconstruction_complete"]
    retained = {"raw": capture.raw, "native_events": capture.native_events}
    assert len(json.dumps(retained, ensure_ascii=False).encode()) <= 800
    assert messages[-1]["data"] == {"stage": "retention", "dropped": 1}


def test_error_serialization_is_included_in_combined_retention_limit():
    """Escaping a structured native error into raw text cannot exceed the retained bound."""
    capture, _ = run_capture([encoded("error", {"details": ['\\"漢'] * 1000})], retained_bytes=1000)
    assert capture.retention_dropped == 1
    assert capture.raw == {} and capture.native_events == []


def test_independent_getter_document_recovers_after_capture_and_retention_loss():
    """An independently copied complete document repairs minimum-event reconstruction gaps."""
    capture, _ = run_capture(
        [encoded("start", {}), encoded("interval", {"large": "x" * 1000}), encoded("end", {})],
        pending_items=2,
        retained_bytes=600,
    )
    assert capture.capture_dropped == capture.retention_dropped == 1
    complete = {"start": {"test_start": {}}, "end": {"sum_sent": {"bytes": 10}}}
    capture.recover_document(json.dumps(complete).encode())
    metadata = capture.metadata()
    assert capture.raw == complete and capture.native_events == []
    assert metadata["complete_document"]
    assert metadata["complete_document_source"] == "getter"
    assert not metadata["reconstruction_complete"]
    assert metadata["callbacks"] == metadata["copied"] + metadata["capture_dropped"]


def test_rejected_later_document_invalidates_preceding_complete_evidence():
    """Retaining an older document cannot conceal loss of its native replacement."""
    original = {"start": {}, "end": {}}
    later = {**original, "padding": "x" * 600, "error": "replacement failure"}
    capture, _ = run_capture(
        [json.dumps(original).encode(), json.dumps(later).encode()], retained_bytes=500
    )
    assert capture.raw == original
    assert capture.retention_dropped == 1
    assert not capture.complete_document
    assert capture.complete_document_source is None
    assert capture.native_error == "replacement failure"
    capture.recover_document(json.dumps(original).encode())
    assert capture.complete_document_source == "getter"
    assert capture.native_error == "replacement failure"


@pytest.mark.parametrize("replacement", ["callback", "getter"])
def test_full_document_error_survives_a_later_clean_document(replacement):
    """Native failure is latched outside callbacks before documents are replaced."""
    failed = b'{"start":{},"end":{},"error":"observed native failure"}'
    clean = b'{"start":{},"end":{}}'
    capture, _ = run_capture([failed, clean] if replacement == "callback" else [failed])
    if replacement == "getter":
        capture.recover_document(clean)
    assert capture.raw == {"start": {}, "end": {}}
    assert capture.complete_document_source == replacement
    assert capture.native_error == "observed native failure"


@pytest.mark.parametrize(
    "payload", [b"bad", encoded("end", {}), json.dumps({"large": "x" * 1000}).encode()]
)
def test_invalid_getter_does_not_replace_retained_callback_evidence(payload):
    """Malformed, partial or over-retention getter output stays a separate diagnostic."""
    capture, _ = run_capture([encoded("start", {}), encoded("end", {})], retained_bytes=600)
    original = capture.raw.copy()
    capture.recover_document(payload)
    assert capture.raw == original
    assert capture.getter_error
    assert not capture.complete_document


def test_unknown_and_server_output_events_preserve_native_names_without_inference():
    """Future native events remain bounded advisory evidence rather than typed intervals."""
    capture, messages = run_capture(
        [encoded("future_kind", {"new": 1}), encoded("server_output_text", "hello")],
        live_events=True,
    )
    assert messages[0]["kind"] == "unknown"
    assert messages[0]["data"] == {"native_kind": "future_kind", "payload": {"new": 1}}
    assert messages[1]["kind"] == "server_output"
    assert capture.raw == {"server_output_text": "hello"}
    assert [value["event"] for value in capture.native_events] == [
        "future_kind",
        "server_output_text",
    ]


def test_native_error_followed_by_end_preserves_original_error():
    """An end fragment is not a success signal and never erases native failure."""
    capture, messages = run_capture(
        [encoded("error", "native refusal"), encoded("end", {})], live_events=True
    )
    assert capture.raw["error"] == "native refusal"
    assert [value["kind"] for value in messages] == ["native_error", "native_end"]


@pytest.mark.parametrize(
    "late", [b"malformed", None, encoded("interval", 3), encoded("error", "late failure")]
)
def test_late_loss_or_error_requires_a_new_independent_complete_document(late):
    """A preceding full document cannot silently erase later native failure evidence."""
    capture, _ = run_capture([b'{"start":{},"end":{}}', late])
    assert not capture.complete_document
    assert not capture.metadata()["reconstruction_complete"]
    if late == encoded("error", "late failure"):
        assert capture.raw["error"] == "late failure"
    capture.recover_document(b'{"start":{},"end":{},"error":"final native failure"}')
    assert capture.complete_document_source == "getter"
    assert capture.raw["error"] == "final native failure"


def test_advisory_unknown_after_complete_document_does_not_rewrite_native_json():
    """A future advisory envelope remains a live observation without inventing failure."""
    capture, messages = run_capture(
        [b'{"start":{},"end":{}}', encoded("future_kind", {"extra": 1})], live_events=True
    )
    assert capture.complete_document and capture.malformed == 0
    assert capture.raw == {"start": {}, "end": {}}
    assert capture.native_events == []
    assert messages[-1]["kind"] == "unknown"


def test_live_and_legacy_delivery_losses_do_not_remove_captured_complete_json():
    """A saturated delivery sink cannot corrupt independently retained final evidence."""
    payloads = [encoded("start", {}), encoded("end", {}), b'{"start":{},"end":{}}']
    capture, _ = run_capture(
        payloads, emit=lambda value: False, live_events=True, legacy_events=True
    )
    assert capture.events_dropped == capture.events_emitted == 2
    assert capture.live_dropped == capture.live_emitted == 3
    assert capture.complete_document
    assert capture.raw == {"start": {}, "end": {}}


def test_transport_frame_oversize_counts_drop_without_parser_failure():
    """IPC encoding rejection is delivery loss rather than loss of native capture."""

    def reject(message):
        raise _ipc.IPCError("oversized frame")

    capture, _ = run_capture(
        [encoded("start", {})], emit=reject, live_events=True, legacy_events=True
    )
    assert capture.events_dropped == capture.live_dropped == 1
    assert capture.raw == {"start": {}}


def test_parser_sink_failure_stops_and_joins_before_owner_can_publish_result():
    """An internal transport failure is raised after the parser relinquishes ownership."""

    def fail(message):
        raise OSError("broken output")

    capture, _ = collector(emit=fail, legacy_events=True)
    capture.capture(None, encoded("start", {}))
    capture.capture(None, encoded("end", {}))
    capture.start()
    with pytest.raises(IperfLibraryError, match="parser failed"):
        capture.close()
    assert not capture._thread.is_alive()
    assert capture._pending_bytes == 0


def test_parser_close_is_idempotent_and_closed_capture_is_explicit_loss():
    """Releasing capture twice cannot restart parsing or emit duplicate late events."""
    capture, messages = run_capture([encoded("start", {})], live_events=True)
    original = list(messages)
    capture.close()
    capture.capture(None, encoded("end", {}))
    assert messages == original
    assert capture.capture_dropped == 1


def test_direct_document_helper_retains_latest_bounded_bytes_without_parsing():
    """Direct execution uses bounded copies for both its callback and fallback getter."""
    ffi = SimpleNamespace(NULL=None, string=lambda pointer, maximum: pointer[:maximum])
    capture = BoundedDocumentCapture(ffi, limit=20)
    capture.capture(None, b"first")
    capture.capture(None, b"second")
    assert capture.payload == b"second"
    assert capture.read(None) is None
    assert capture.read(b"getter") == b"getter"
    capture.capture(None, b"x" * 21)
    assert capture.payload == b"second"
    assert capture.error == "Cannot capture native JSON: IperfLibraryError"
    with pytest.raises(IperfLibraryError, match="capture byte limit"):
        capture.read(b"x" * 21)


def test_direct_helper_latches_prior_error_only_when_finalized(monkeypatch):
    """Byte-only callbacks defer parsing and preserve failure across replacement."""
    ffi = SimpleNamespace(NULL=None, string=lambda pointer, maximum: pointer[:maximum])
    capture = BoundedDocumentCapture(ffi)
    failed = b'{"error":"observed native failure"}'
    clean = b'{"start":{},"end":{}}'
    original = _event_capture._native_object

    def forbidden(*args):
        raise AssertionError("parser entered from callback")

    monkeypatch.setattr(_event_capture, "_native_object", forbidden)
    capture.capture(None, failed)
    capture.capture(None, clean)
    assert capture.native_error is None and capture.error is None
    assert capture.payload == clean
    monkeypatch.setattr(_event_capture, "_native_object", original)
    capture.finalize()
    assert capture.native_error == "observed native failure"
    assert capture.payload == clean
    assert capture._pending_bytes == 0 and not capture._documents
    capture.finalize()
    assert capture.native_error == "observed native failure"


@pytest.mark.parametrize("budget", ["count", "bytes"])
def test_direct_helper_saturation_retains_known_failure_for_finalize(budget):
    """Bounded history loss never discards an already-admitted native error."""
    failed = b'{"error":"known failure"}'
    clean = b'{"start":{},"end":{}}'
    ffi = SimpleNamespace(NULL=None, string=lambda pointer, maximum: pointer[:maximum])
    capture = BoundedDocumentCapture(
        ffi,
        pending_items=1 if budget == "count" else 10,
        pending_bytes=len(failed) if budget == "bytes" else 1000,
    )
    capture.capture(None, failed)
    capture.capture(None, clean)
    assert capture.error == "Native JSON callback history exceeds the capture bound"
    assert capture._pending_bytes == len(failed) and len(capture._documents) == 1
    capture.finalize()
    assert capture.native_error == "known failure"
    assert capture._pending_bytes == 0 and not capture._documents


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'])
def test_direct_helper_rejects_non_strict_json_outside_callback(payload):
    """Direct and worker documents use the same duplicate and finite-value rules."""
    ffi = SimpleNamespace(NULL=None, string=lambda pointer, maximum: pointer[:maximum])
    capture = BoundedDocumentCapture(ffi)
    capture.capture(None, payload)
    assert capture.error is None
    capture.finalize()
    assert capture.error.startswith("Cannot decode native JSON:")
    with pytest.raises((ValueError, _ipc.IPCError)):
        capture.parse_document(payload)
