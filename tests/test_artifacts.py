"""Contract tests for strict, portable v1 result artifacts and explicit migration."""

import copy
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import iperf3_lib.artifacts as artifacts
from iperf3_lib.artifacts import (
    ArtifactProducer,
    ArtifactValidationError,
    ResultArtifact,
    UnsupportedArtifactVersion,
    artifact_from_dict,
    artifact_from_legacy_dict,
    artifact_from_result,
    artifact_to_dict,
    dumps_artifact,
    loads_artifact,
)
from iperf3_lib.config import ClientConfig
from iperf3_lib.result import (
    ConfigurationSnapshot,
    Diagnostic,
    EndStats,
    ExecutionMetadata,
    FieldAvailability,
    FlowStats,
    IntervalStats,
    Result,
    RunTiming,
    StreamStats,
    SumStats,
    VerifiedSetting,
    result_from_iperf_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"
NATIVE_FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _artifact():
    requested = asdict(ClientConfig("127.0.0.1", duration=1))
    requested["protocol"] = "tcp"
    sender = SumStats(
        bits_per_second=0,
        bytes=0,
        duration_seconds=1,
        start_seconds=0,
        end_seconds=1,
        omitted=False,
        direction="client_to_server",
        observation="sender",
    )
    receiver = SumStats(
        bits_per_second=None,
        bytes=0,
        duration_seconds=1,
        direction="client_to_server",
        observation="receiver",
    )
    result = Result(
        ok=True,
        raw={
            "start": {"test_start": {"duration": 1, "num_streams": 2, "blksize": 131072}},
            "end": {"sum_received": {"bits_per_second": "malformed"}},
            "extra": [None, False, 0],
        },
        protocol="tcp",
        reporting_role="client",
        flows=[FlowStats("client_to_server", sender, receiver)],
        end=EndStats(sender, receiver),
        streams=[StreamStats("client_to_server", 5, sender, receiver)],
        intervals=[
            IntervalStats(
                -1,
                0,
                0,
                "client_to_server",
                "sender",
                scope="aggregate",
                bytes=0,
                duration_seconds=1,
                omitted=True,
            ),
            IntervalStats(
                0,
                1,
                0,
                "client_to_server",
                "sender",
                5,
                "stream",
                bytes=0,
                duration_seconds=1,
                omitted=False,
            ),
        ],
        diagnostics=[
            Diagnostic(
                "Recorded malformed input as absent normalized throughput.",
                "warning",
                "org.example.future_advisory",
                "/flows/0/receiver/bits_per_second",
                ["/raw/end/sum_received/bits_per_second"],
            )
        ],
        started_at_seconds=1000,
        duration_seconds=1,
        completed_at_seconds=1002,
        execution=ExecutionMetadata(
            "completed",
            "forward",
            RunTiming(999, 1002, 3, 1000, 1, 1001),
            ConfigurationSnapshot(
                requested,
                {"duration": VerifiedSetting(1, "verified", ["/raw/start/test_start/duration"])},
            ),
            "iperf 3.21",
            "recorded native system",
            "3.14.7",
            "recorded wrapper platform",
        ),
        availability={
            "/flows/0/receiver/bits_per_second": FieldAvailability(
                "malformed", ["/raw/end/sum_received/bits_per_second"]
            )
        },
        extensions={"org.example.run": {"id": "original", "tags": ["x", None, 0]}},
    )
    return ResultArtifact(
        result,
        ArtifactProducer("original-writer", "0.3.0"),
        extensions={"org.example.envelope": {"future": True}},
    )


def _set(data, path, value):
    tokens = path.split("/")[1:]
    cursor = data
    for token in tokens[:-1]:
        cursor = cursor[int(token)] if isinstance(cursor, list) else cursor[token]
    token = tokens[-1]
    cursor[int(token) if isinstance(cursor, list) else token] = value


def test_full_model_round_trip_preserves_units_zero_missing_and_producer():
    """Nested models, explicit zeros, raw data, units and writer identity survive."""
    artifact = _artifact()
    mapping = artifact_to_dict(artifact)
    restored = artifact_from_dict(mapping)
    assert restored == artifact
    assert loads_artifact(dumps_artifact(artifact)) == artifact
    assert loads_artifact(dumps_artifact(artifact).encode()) == artifact
    assert restored.result.flows[0].sender.bits_per_second == 0
    assert restored.result.flows[0].receiver.bits_per_second is None
    assert restored.result.intervals[0].omitted is True
    assert restored.result.intervals[0].start_seconds == -1
    assert restored.result.execution.timing.elapsed_seconds == 3
    assert restored.result.duration_seconds == 1
    assert restored.result.completed_at_seconds == 1002
    assert restored.result.execution.timing.estimated_completed_at_seconds == 1001
    assert restored.result.diagnostics[0].code == "org.example.future_advisory"
    assert restored.producer == ArtifactProducer("original-writer", "0.3.0")
    assert mapping["result"].keys() == {item.name for item in fields(Result)}
    assert restored.result.summary_mbps == 0


def test_conversion_and_import_detach_nested_mutable_data():
    """Later caller mutation cannot change an admitted artifact or imported model."""
    original = _artifact().result
    artifact = artifact_from_result(original)
    mapping = artifact_to_dict(artifact)
    restored = artifact_from_dict(mapping)
    original.raw["extra"].append(42)
    original.execution.configuration.requested["server"] = "changed"
    mapping["result"]["extensions"]["org.example.run"]["tags"].append("changed")
    assert artifact.result.raw["extra"] == [None, False, 0]
    assert artifact.result.execution.configuration.requested["server"] == "127.0.0.1"
    assert restored.result.extensions["org.example.run"]["tags"] == ["x", None, 0]


def test_result_snapshot_stays_unversioned():
    """Existing to_dict callers retain the unversioned dataclass representation."""
    data = _artifact().result.to_dict()
    assert "ok" in data and "schema_version" not in data
    with pytest.raises(ArtifactValidationError, match="artifact_from_legacy_dict"):
        loads_artifact(json.dumps(data))


@pytest.mark.parametrize("version", [-1, 0, 2, 99])
def test_unknown_versions_fail_before_attempting_future_shape(version):
    """Future or unsupported versions require an upgraded reader and lose no data."""
    with pytest.raises(UnsupportedArtifactVersion, match="upgrade") as error:
        artifact_from_dict({"schema_version": version, "new_shape": {}})
    assert error.value.path == "/schema_version"


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_schema_version_is_strict_integer(value):
    """Python's bool/int relationship does not weaken schema discrimination."""
    with pytest.raises(ArtifactValidationError) as error:
        artifact_from_dict({"schema_version": value})
    assert error.value.path == "/schema_version"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/producer",
        "/result",
        "/result/end",
        "/result/end/sum_sent",
        "/result/flows/0",
        "/result/streams/0",
        "/result/intervals/0",
        "/result/diagnostics/0",
        "/result/execution",
        "/result/execution/timing",
        "/result/execution/configuration",
        "/result/execution/configuration/effective/duration",
    ],
)
def test_unknown_canonical_fields_are_rejected_at_every_model_level(path):
    """The reader never silently drops new fields whose semantics are unknown."""
    data = artifact_to_dict(_artifact())
    _set(data, f"{path}/new_field", 1)
    with pytest.raises(ArtifactValidationError, match="unknown canonical") as error:
        artifact_from_dict(data)
    assert error.value.path == f"{path}/new_field"


@pytest.mark.parametrize(
    "path,value",
    [
        ("/kind", "other.result"),
        ("/producer/name", ""),
        ("/producer/version", 3),
        ("/result/ok", 1),
        ("/result/bidirectional", 0),
        ("/result/raw", []),
        ("/result/flows", {}),
        ("/result/flows/0/sender", []),
        ("/result/protocol", "TCP"),
        ("/result/reporting_role", "observer"),
        ("/result/execution/status", "cancelled"),
        ("/result/execution/method", "duplex"),
        ("/result/flows/0/direction", "forward"),
        ("/result/flows/0/sender/observation", "local"),
        ("/result/intervals/0/scope", "combined"),
        ("/result/diagnostics/0/severity", "critical"),
        ("/result/diagnostics/0/code", ""),
        ("/result/diagnostics/0/path", "raw/end"),
        ("/result/diagnostics/0/evidence_paths/0", "/raw/~2wrong"),
        ("/result/execution/configuration/effective/duration/state", "assumed"),
        ("/result/execution/configuration/requested/parallel", True),
        ("/result/execution/configuration/requested/protocol", "invalid"),
        ("/result/execution/configuration/requested/port", 0),
        ("/result/execution/configuration/requested/reverse", 1),
    ],
)
def test_wrong_types_enums_pointers_and_config_values_fail_at_path(path, value):
    """Malformed canonical values report their exact JSON Pointer location."""
    data = artifact_to_dict(_artifact())
    _set(data, path, value)
    with pytest.raises(ArtifactValidationError) as error:
        artifact_from_dict(data)
    assert error.value.path == path


@pytest.mark.parametrize("field", ["bytes", "packets", "lost_packets", "retransmits", "stream_id"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "0"])
def test_counts_are_nonnegative_integers(field, value):
    """Counts and local socket identifiers reject coercion and negative values."""
    data = artifact_to_dict(_artifact())
    path = f"/result/intervals/0/{field}"
    _set(data, path, value)
    with pytest.raises(ArtifactValidationError) as error:
        artifact_from_dict(data)
    assert error.value.path == path


@pytest.mark.parametrize(
    "field", ["bits_per_second", "duration_seconds", "lost_percent", "jitter_ms"]
)
@pytest.mark.parametrize("value", [True, -0.1, float("inf"), float("-inf"), float("nan"), "0"])
def test_measurements_are_finite_nonnegative_numbers(field, value):
    """Missing measurements use null; invalid values never become plausible zeros."""
    data = artifact_to_dict(_artifact())
    path = f"/result/intervals/0/{field}"
    _set(data, path, value)
    with pytest.raises(ArtifactValidationError) as error:
        artifact_from_dict(data)
    assert error.value.path == path


@pytest.mark.parametrize(
    "path,value",
    [
        ("/result/execution", None),
        ("/result/ok", False),
        ("/result/error", "failed"),
        ("/result/bidirectional", True),
        ("/result/completed_at_seconds", 1001),
        ("/result/end/sum_sent/bits_per_second", 1),
        ("/result/streams/0/sender/observation", "receiver"),
        ("/result/streams/0/sender/direction", "server_to_client"),
        ("/result/intervals/0/direction", "server_to_client"),
        ("/result/intervals/0/stream_id", 5),
        ("/result/intervals/0/end_seconds", -2),
        ("/result/intervals/0/lost_percent", 101),
        ("/result/execution/timing/elapsed_seconds", -1),
        ("/result/execution/configuration/effective/duration/value", None),
        ("/result/execution/configuration/effective/duration/evidence_paths", []),
    ],
)
def test_invariants_reject_contradictory_normalized_data(path, value):
    """Envelope validation detects conflicting status, projection, scope and provenance."""
    data = artifact_to_dict(_artifact())
    _set(data, path, value)
    with pytest.raises(ArtifactValidationError):
        artifact_from_dict(data)


def test_duplicate_flow_and_stream_identities_are_rejected():
    """A saved model cannot double-count one named flow or local stream."""
    for key in ("flows", "streams"):
        data = artifact_to_dict(_artifact())
        data["result"][key].append(copy.deepcopy(data["result"][key][0]))
        with pytest.raises(ArtifactValidationError, match="duplicate"):
            artifact_from_dict(data)


def test_direction_flags_and_local_stream_intervals_cannot_disagree():
    """Direction consistency also holds when an artifact's method is unknown."""
    artifact = _artifact()
    artifact.result.execution.method = "unknown"
    artifact.result.intervals[1].direction = "server_to_client"
    with pytest.raises(ArtifactValidationError, match="both directions"):
        artifact_to_dict(artifact)
    artifact.result.bidirectional = True
    with pytest.raises(ArtifactValidationError, match="local stream"):
        artifact_to_dict(artifact)


def test_clock_adjustment_does_not_invalidate_monotonic_elapsed():
    """Observed wall-clock order may change while monotonic elapsed remains valid."""
    artifact = _artifact()
    artifact.result.execution.timing.started_at_seconds = 1003
    assert loads_artifact(dumps_artifact(artifact)).result.execution.timing.elapsed_seconds == 3


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1, 2), {1: "key"}, object()])
def test_raw_and_extensions_allow_only_strict_json(value):
    """Escape hatches preserve unknown JSON, never arbitrary Python objects."""
    for destination in ("raw", "extensions"):
        artifact = _artifact()
        getattr(artifact.result, destination)["org.example.extra"] = value
        with pytest.raises(ArtifactValidationError):
            artifact_to_dict(artifact)


def test_cyclic_raw_is_rejected_without_recursion_failure():
    """Mutable caller data cannot introduce an unrepresentable JSON cycle."""
    artifact = _artifact()
    artifact.result.raw["cycle"] = artifact.result.raw
    with pytest.raises(ArtifactValidationError, match="cyclic"):
        artifact_to_dict(artifact)


@pytest.mark.parametrize("key", ["unnamespaced", "org.", ".org", "org..example", "org/example"])
def test_extension_keys_require_dotted_namespaces(key):
    """Future extension data is isolated from canonical fields and other writers."""
    artifact = _artifact()
    artifact.extensions[key] = "value"
    with pytest.raises(ArtifactValidationError, match="namespace"):
        artifact_to_dict(artifact)


def test_duplicate_json_keys_include_full_pointer_even_inside_raw():
    """Duplicates at any depth cannot silently overwrite stored evidence."""
    text = dumps_artifact(_artifact())
    duplicate = text.replace('"extra": [null, false, 0]', '"extra": 1, "extra": 2')
    with pytest.raises(ArtifactValidationError, match="duplicate") as error:
        loads_artifact(duplicate)
    assert error.value.path == "/result/raw/extra"


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "{", b"\xff", "[]", "null"])
def test_malformed_or_nonfinite_json_is_rejected(text):
    """Parsing errors become a documented artifact error rather than silent coercion."""
    with pytest.raises(ArtifactValidationError):
        loads_artifact(text)


def test_required_canonical_fields_are_not_inferred_during_import():
    """V1 requires the complete schema emitted by its writer, including nulls."""
    data = artifact_to_dict(_artifact())
    del data["result"]["execution"]["platform"]
    with pytest.raises(ArtifactValidationError, match="required canonical") as error:
        artifact_from_dict(data)
    assert error.value.path == "/result/execution/platform"


def test_mutated_models_are_validated_before_encoding():
    """Dataclass constructors are convenient, but the serialization boundary is strict."""
    artifact = _artifact()
    artifact.result.execution.status = "unrecognized"
    with pytest.raises(ArtifactValidationError):
        dumps_artifact(artifact)
    artifact = _artifact()
    artifact.result.streams[0].sender = {"bits_per_second": 0}
    with pytest.raises(ArtifactValidationError, match="SumStats dataclass"):
        artifact_to_dict(artifact)


def test_availability_must_target_null_canonical_field_with_evidence():
    """Availability cannot overwrite observed zero or smuggle raw-field semantics."""
    for pointer in ("/raw/unknown", "/flows/9/sender", "/flows/0/sender/bytes", "", "/new_field"):
        artifact = _artifact()
        artifact.result.availability[pointer] = FieldAvailability("absent")
        with pytest.raises(ArtifactValidationError):
            artifact_to_dict(artifact)
    artifact = _artifact()
    artifact.result.availability["/protocol"] = FieldAvailability("unknown")
    artifact.result.protocol = None
    with pytest.raises(ArtifactValidationError, match="evidence"):
        artifact_to_dict(artifact)


@pytest.mark.parametrize("state", ["absent", "unsupported", "malformed", "unknown"])
def test_all_availability_states_round_trip(state):
    """Absent, unsupported, malformed and unknown remain distinct from zero."""
    artifact = _artifact()
    artifact.result.availability["/flows/0/receiver/bits_per_second"].state = state
    assert loads_artifact(dumps_artifact(artifact)) == artifact


def test_effective_settings_preserve_verified_differences_and_unavailability():
    """Only returned evidence establishes an effective value; differences are explicit."""
    artifact = _artifact()
    config = artifact.result.execution.configuration
    config.effective["parallel"] = VerifiedSetting(
        2, "verified", ["/raw/start/test_start/num_streams"]
    )
    with pytest.raises(ArtifactValidationError, match="disagreement"):
        artifact_to_dict(artifact)
    artifact.result.diagnostics.append(
        Diagnostic(
            "Native parallelism differs.",
            "warning",
            "configuration.difference",
            "/execution/configuration/effective/parallel",
        )
    )
    config.effective["blksize"] = VerifiedSetting(
        131072, "verified", ["/raw/start/test_start/blksize"]
    )
    config.effective["tos"] = VerifiedSetting()
    config.effective["mptcp"] = VerifiedSetting(
        None, "unsupported", ["/extensions/org.example.capabilities/mptcp"]
    )
    assert loads_artifact(dumps_artifact(artifact)) == artifact
    config.effective["tos"].value = 0
    with pytest.raises(ArtifactValidationError, match="null"):
        artifact_to_dict(artifact)


@pytest.mark.parametrize(
    "key,value",
    [("protocol", 123), ("rate", "fast"), ("reverse", []), ("parallel", True), ("server", 12)],
)
def test_verified_settings_still_require_field_specific_types(key, value):
    """Verified state cannot bypass the canonical effective configuration types."""
    artifact = _artifact()
    artifact.result.execution.configuration.effective[key] = VerifiedSetting(
        value, "verified", ["/raw/start/test_start/duration"]
    )
    with pytest.raises(ArtifactValidationError) as error:
        artifact_to_dict(artifact)
    assert error.value.path == f"/result/execution/configuration/effective/{key}/value"


@pytest.mark.parametrize(
    "pointer",
    [
        "/execution/configuration/requested/duration",
        "/raw/missing",
        "/extensions/org.example.missing",
        "/raw/extra/03",
        "/raw/extra/0",
    ],
)
def test_verification_needs_existing_observed_evidence(pointer):
    """Requests, absent paths and null values cannot support a verified native setting."""
    artifact = _artifact()
    artifact.result.execution.configuration.effective["duration"].evidence_paths = [pointer]
    with pytest.raises(ArtifactValidationError, match="existing native raw"):
        artifact_to_dict(artifact)


def test_namespaced_getter_receipt_can_support_verified_setting():
    """Persisted getter evidence remains usable without running the getter on import."""
    artifact = _artifact()
    artifact.result.extensions["org.example.getters"] = {
        "duration": {"value": 1, "getter": "iperf_get_test_duration"}
    }
    artifact.result.execution.configuration.effective["duration"].evidence_paths = [
        "/extensions/org.example.getters/duration"
    ]
    assert loads_artifact(dumps_artifact(artifact)) == artifact


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
def test_client_execution_metadata_survives_artifact_boundary(monkeypatch, status):
    """Actual Client.run metadata, differences and failure data survive serialization."""
    import iperf3_lib.iperf_client as client_module

    raw = json.loads(
        (NATIVE_FIXTURES / "3.21" / "tcp-forward-client.json").read_text(encoding="utf-8")
    )
    freed = []
    native = SimpleNamespace(
        i_errno=7,
        iperf_new_test=lambda: 1,
        iperf_defaults=lambda test: 0,
        set_protocol=lambda test, protocol: 0,
        iperf_get_test_protocol_id=lambda test: 1,
        iperf_run_client=lambda test: -1 if status == "failed" else 0,
        iperf_get_test_json_output_string=lambda test: (
            0 if status == "incomplete" else json.dumps(raw).encode()
        ),
        iperf_strerror=lambda error: b"native connection failure",
        iperf_free_test=freed.append,
    )
    for name in ("role", "server_hostname", "server_port", "duration", "rate", "json_output"):
        setattr(native, f"iperf_set_test_{name}", lambda *args: None)
    monkeypatch.setattr(client_module, "lib", native)
    monkeypatch.setattr(
        client_module,
        "ffi",
        SimpleNamespace(NULL=0, new=lambda spec, value: value, string=lambda value: value),
    )
    wall = iter([100, 103])
    monotonic = iter([200, 202.5])
    monkeypatch.setattr(
        client_module,
        "time",
        SimpleNamespace(time=lambda: next(wall), monotonic=lambda: next(monotonic)),
    )
    config = ClientConfig("127.0.0.1", duration=1, rate=2_000_000)
    result = client_module.Client(config).run()
    before = copy.deepcopy(result.to_dict())
    artifact = artifact_from_result(result)
    restored = loads_artifact(dumps_artifact(artifact))
    assert result.to_dict() == before
    config.rate = 10
    assert restored.result.execution.configuration.requested["rate"] == 2_000_000
    assert restored.result.execution.status == status
    assert restored.result.execution.timing.started_at_seconds == 100
    assert restored.result.execution.timing.completed_at_seconds == 103
    assert restored.result.execution.timing.elapsed_seconds == 2.5
    assert restored.result.completed_at_seconds == 103
    assert freed == [1]
    if status != "incomplete":
        assert restored.result.raw == raw
        assert any(item.code == "configuration.difference" for item in restored.result.diagnostics)
    if status == "failed":
        assert restored.result.error == "native connection failure"
        assert not restored.result.ok


def test_mixed_udp_stream_requires_unattributed_observation_and_diagnostic():
    """Mixed native UDP values remain available without invented endpoint attribution."""
    artifact = _artifact()
    stream = artifact.result.streams[0]
    stream.unattributed = SumStats(bits_per_second=0, jitter_ms=0, packets=0, lost_packets=0)
    with pytest.raises(ArtifactValidationError, match="diagnostic"):
        artifact_to_dict(artifact)
    artifact.result.diagnostics.append(
        Diagnostic(
            "Mixed UDP summary.",
            "warning",
            "provenance.mixed_udp_summary",
            "/streams/0/unattributed",
            ["/raw/end/streams/0/udp"],
        )
    )
    assert loads_artifact(dumps_artifact(artifact)) == artifact
    stream.unattributed.observation = "sender"
    with pytest.raises(ArtifactValidationError, match="observation"):
        artifact_to_dict(artifact)


@pytest.mark.parametrize(
    "ok,error,status",
    [(True, None, "completed"), (False, "failure", "failed"), (False, None, "incomplete")],
)
def test_manual_result_conversion_adds_only_explicit_compatibility_metadata(ok, error, status):
    """Old constructors remain usable without fabricating native or operation metadata."""
    result = Result(
        ok=ok, error=error, started_at_seconds=100, duration_seconds=1, completed_at_seconds=101
    )
    artifact = artifact_from_result(result)
    timing = artifact.result.execution.timing
    assert artifact.result.execution.status == status
    assert artifact.result.execution.method == "unknown"
    assert artifact.result.execution.configuration.requested is None
    assert artifact.result.completed_at_seconds is None
    assert timing.started_at_seconds is None
    assert timing.completed_at_seconds is None
    assert timing.elapsed_seconds is None
    assert timing.native_started_at_seconds == 100
    assert timing.requested_duration_seconds == 1
    assert timing.estimated_completed_at_seconds == 101
    assert result.completed_at_seconds == 101
    assert {item.code for item in artifact.result.diagnostics} == {
        "compatibility.inferred_status",
        "compatibility.unverified_completion",
    }


def test_legacy_end_only_preserves_zero_without_inventing_direction():
    """Migration constructs nested models and an explicitly unknown primary flow."""
    legacy = {
        "ok": True,
        "raw": {"future_native": 7},
        "end": {"sum_sent": {"bits_per_second": 0}, "sum_received": None},
        "intervals": [{"start_seconds": 0, "end_seconds": 1, "bits_per_second": 0}],
        "completed_at_seconds": 101,
    }
    artifact = artifact_from_legacy_dict(legacy)
    assert isinstance(artifact.result.end, EndStats)
    assert isinstance(artifact.result.end.sum_sent, SumStats)
    assert artifact.result.flows[0].direction == "unknown"
    assert artifact.result.intervals[0].scope == "unknown"
    assert artifact.result.intervals[0].stream_id is None
    assert artifact.result.raw == {"future_native": 7}
    assert artifact.result.execution.timing.estimated_completed_at_seconds == 101
    assert loads_artifact(dumps_artifact(artifact)) == artifact


@pytest.mark.parametrize(
    "legacy",
    [
        {"ok": True, "unknown": 1},
        {"ok": True, "execution": None},
        {"ok": True, "end": {"new": 1}},
        {"ok": True, "intervals": [{"start_seconds": 0, "end_seconds": 1, "scope": "aggregate"}]},
        {"ok": True, "diagnostics": [{"message": "x", "code": "new"}]},
        {},
        {"ok": True, "intervals": [{}]},
    ],
)
def test_legacy_import_rejects_unknown_or_missing_required_fields(legacy):
    """The explicit adapter supports one known snapshot shape, never lossy guessing."""
    with pytest.raises(ArtifactValidationError):
        artifact_from_legacy_dict(legacy)


def test_import_does_not_probe_environment_native_library_or_reparse_raw(monkeypatch):
    """Normalized values and writer environment survive even with unusable native raw."""
    from iperf3_lib.ffi import api

    artifact = _artifact()
    text = dumps_artifact(artifact)

    def forbidden(*args, **kwargs):
        raise AssertionError("Artifact import must not call native/environment/parser helpers")

    monkeypatch.delenv("IPERF3_LIB", raising=False)
    monkeypatch.setattr(api, "_dlopen", forbidden)
    monkeypatch.setattr(api.ffi, "dlopen", forbidden)
    monkeypatch.setattr("iperf3_lib.result.result_from_iperf_json", forbidden)
    monkeypatch.setattr(artifacts, "version", forbidden)
    monkeypatch.setattr("platform.platform", forbidden)
    monkeypatch.setattr("platform.python_version", forbidden)
    assert loads_artifact(text) == artifact
    assert artifact_from_dict(artifact_to_dict(artifact)) == artifact
    for fixture in FIXTURES.glob("v1-*.json"):
        document = fixture.read_text(encoding="utf-8")
        imported = loads_artifact(document)
        assert artifact_to_dict(imported) == json.loads(document)


def test_fresh_process_can_import_and_deserialize_with_dlopen_forbidden():
    """Lazy loading also holds before package import in a fresh Python process."""
    code = """
import cffi
def forbidden(*args, **kwargs):
    raise AssertionError('libiperf must not be loaded')
cffi.FFI.dlopen = forbidden
from iperf3_lib.artifacts import loads_artifact
import sys
artifact = loads_artifact(sys.stdin.read())
assert artifact.result.flows[0].sender.bits_per_second == 0
assert artifact.producer.name == 'original-writer'
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=dumps_artifact(_artifact()),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "fixture",
    sorted(NATIVE_FIXTURES.glob("*/*.json")),
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_every_saved_native_endpoint_round_trips_without_losing_raw(fixture):
    """Minimum/latest TCP, UDP, SCTP, methods and endpoint roles retain every field."""
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    result = result_from_iperf_json(raw)
    artifact = artifact_from_result(result)
    restored = loads_artifact(dumps_artifact(artifact))
    assert restored == artifact
    assert restored.result.raw == raw
    assert restored.result.execution.timing.completed_at_seconds is None
    assert restored.result.completed_at_seconds is None


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("v1-*.json")), ids=lambda path: path.name)
def test_committed_v1_goldens_round_trip_semantically(fixture):
    """Retained writer artifacts remain readable without regeneration or information loss."""
    data = json.loads(fixture.read_text(encoding="utf-8"))
    artifact = artifact_from_dict(data)
    assert artifact_to_dict(artifact) == data
    assert loads_artifact(dumps_artifact(artifact, indent=2)) == artifact


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("legacy-*.json")), ids=lambda path: path.name
)
def test_committed_legacy_snapshots_require_explicit_migration(fixture):
    """Historical snapshots remain supported through a clearly selected compatibility API."""
    data = json.loads(fixture.read_text(encoding="utf-8"))
    with pytest.raises(ArtifactValidationError, match="migration|legacy"):
        artifact_from_dict(data)
    artifact = artifact_from_legacy_dict(data)
    assert loads_artifact(dumps_artifact(artifact)) == artifact
    assert artifact.result.raw == data.get("raw", {})


def test_retained_fixture_inventory_and_native_provenance():
    """Deleting a golden cannot silently turn its contract regression into a skip."""
    assert {path.name for path in FIXTURES.glob("*.json")} == {
        "legacy-end-only.json",
        "v1-failed.json",
        "v1-incomplete.json",
        "v1-partial-zero-extensions.json",
        "v1-native-3.19.1-tcp-bidirectional-client.json",
        "v1-native-3.19.1-tcp-reverse-server.json",
        "v1-native-3.21-tcp-warmup-server.json",
        "v1-native-3.21-udp-bidirectional-client.json",
        "v1-native-3.21-sctp-reverse-client.json",
    }
    for fixture in FIXTURES.glob("v1-native-*.json"):
        artifact = loads_artifact(fixture.read_text(encoding="utf-8"))
        source = artifact.extensions["org.iperf3-lib.fixture"]["native_fixture"]
        native = json.loads((NATIVE_FIXTURES / source).read_text(encoding="utf-8"))
        assert artifact.result.raw == native
        assert artifact.result.execution.native_version == native["start"]["version"]
        assert artifact.result.flows[0].sender.bytes == native["end"]["sum_sent"]["bytes"]
        assert artifact.result.flows[0].receiver.bytes == native["end"]["sum_received"]["bytes"]
        assert (
            artifact.result.flows[0].sender.duration_seconds == native["end"]["sum_sent"]["seconds"]
        )
        if artifact.result.protocol == "sctp":
            assert all(interval.retransmits is None for interval in artifact.result.intervals)
            assert any(
                state.state == "unsupported" for state in artifact.result.availability.values()
            )
            assert any(
                item.code == "measurement.unsupported" for item in artifact.result.diagnostics
            )
