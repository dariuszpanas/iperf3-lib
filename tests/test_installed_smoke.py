"""Installed receipt qualification uses retained native fixtures and deliberate synthetic variants."""

from __future__ import annotations

import importlib.metadata
import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import RateIntent, resolve_rate
from iperf3_lib.result import result_from_iperf_json
from scripts import smoke_release as smoke

ROOT = Path(__file__).resolve().parents[1]


def native_result(version="3.21", profile="tcp-forward", config=None, intent=None):
    """Keep genuine measurement receipts while explicitly synthesizing wrapper/config evidence."""
    raw = json.loads(
        (ROOT / "tests/fixtures/native" / version / f"{profile}-client.json").read_text()
    )
    settings = raw["start"]["test_start"]
    if config is None:
        config = ClientConfig(
            "127.0.0.1",
            protocol=settings["protocol"].lower(),
            duration=settings["duration"],
            parallel=settings["num_streams"],
            rate=settings["target_bitrate"],
            reverse=bool(settings["reverse"]),
            bidirectional=bool(settings["bidir"]),
        )
    resolution = resolve_rate(config, intent)
    settings.update(
        omit=config.omit,
        duration=config.duration,
        num_streams=config.parallel,
        target_bitrate=resolution.native_per_stream_bps,
        reverse=int(config.reverse),
        bidir=int(config.bidirectional),
    )
    for field in ("blksize", "tos"):
        value = getattr(config, field)
        if value is not None:
            settings[field] = value
    raw["start"]["connecting_to"]["host"] = str(config.server)
    raw["start"]["target_bitrate"] = resolution.native_per_stream_bps
    raw["start"]["connecting_to"]["port"] = config.port
    for item in raw["start"]["connected"]:
        item["remote_port"] = config.port
    result = result_from_iperf_json(raw, reporting_role="client")
    timing = result.execution.timing
    timing.started_at_seconds = raw["start"]["timestamp"]["timesecs"] - 0.25
    timing.completed_at_seconds = timing.started_at_seconds + config.duration + 0.5
    timing.elapsed_seconds = config.duration + 0.5
    result.completed_at_seconds = timing.completed_at_seconds
    result.execution.configuration.requested = smoke.config_dict(
        replace(config, rate=resolution.native_per_stream_bps)
    )
    result.execution.python_version = "synthetic wrapper fixture"
    result.execution.platform = "synthetic installed environment"
    result.extensions["iperf3_lib.rate_intent"] = {
        "schema_version": 1,
        "caller_config": smoke.config_dict(config),
        "intent": asdict(intent) if intent is not None else None,
        "resolution": resolution.to_dict(),
    }
    return result, config


def capability_report(*, probe_native, result=None):
    """Retain a deterministic probe receipt while never loading native libraries in units."""
    from iperf3_lib.capabilities import get_capabilities

    data = smoke.json_value(asdict(get_capabilities(probe_native=False, result=result)))
    if probe_native:
        data["library"] = {"state": "available", "version": "3.21", "diagnostics": []}
        for feature in data["features"]:
            for symbol in feature["symbols"]:
                if symbol["declared"]:
                    symbol["state"] = "present"
    return data


@pytest.fixture
def installed_receipt(monkeypatch):
    """Run real smoke orchestration over explicit synthetic native executions."""
    import iperf3_lib
    import iperf3_lib.ffi.api as api

    calls = []
    ports = []

    class FixtureClient:
        """Return native capture variants for the exact admitted configuration."""

        def __init__(self, config, *, rate_intent=None):
            self.config = config
            self.intent = rate_intent

        def run(self):
            cfg = self.config
            calls.append((smoke.config_dict(cfg), self.intent))
            profile = (
                "sctp-forward"
                if cfg.protocol.value == "sctp"
                else "udp"
                if cfg.protocol.value == "udp"
                else "tcp-bidirectional"
                if cfg.bidirectional
                else "tcp-reverse"
                if cfg.reverse
                else "tcp-forward"
            )
            return native_result(profile=profile, config=cfg, intent=self.intent)[0]

    @contextmanager
    def server(executable, *, port=None):
        selected = port if port is not None else 16000 + len(ports)
        ports.append(selected)
        yield "127.0.0.1", selected

    monkeypatch.setattr(iperf3_lib, "Client", FixtureClient)
    monkeypatch.setattr(smoke, "native_server", server)
    monkeypatch.setattr(
        smoke,
        "require_installed_package",
        lambda version: "/isolated/lib/site-packages/iperf3_lib/__init__.py",
    )
    monkeypatch.setattr(smoke, "qualify_capabilities", capability_report)
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "iperf3")
    pointer = api.ffi.new("char[]", b"3.21")
    monkeypatch.setattr(api, "lib", SimpleNamespace(iperf_get_iperf_version=lambda: pointer))
    version = importlib.metadata.version("iperf3-lib")
    receipt = smoke.qualify(version, "3.21")
    receipt.update(
        source_revision="a" * 40,
        artifact={"filename": f"iperf3_lib-{version}-py3-none-any.whl", "sha256": "b" * 64},
    )
    return receipt, calls, ports


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
@pytest.mark.parametrize(
    "profile", ["tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp", "sctp-forward"]
)
def test_native_fixture_analysis_transport_and_rate_receipts(version, profile):
    """Qualify actual recorded measurement units and native setting receipts on both versions."""
    result, config = native_result(version, profile)
    analysis = smoke.qualify_analysis(result)
    assert len(analysis["summary"]) == 2 * len(result.flows)
    for item in analysis["summary"]:
        assert item["throughput_bps"] == item["bytes"] * 8 / item["measured_seconds"]
    for item in analysis["intervals"]:
        if item["coverage"]["included_count"] < 2:
            assert item["quality"] == "insufficient_data"
    transport = smoke.qualify_transport_evidence(result)
    assert len(transport["cpu"]) == 2
    if profile in ("udp", "sctp-forward", "tcp-reverse"):
        assert transport["tcp_interval_count"] == transport["tcp_summary_count"] == 0
    else:
        assert transport["tcp_interval_count"] > 0 and transport["tcp_summary_count"] > 0
    assert smoke.qualify_rate_intent(result, config)["caller_config"] == smoke.config_dict(config)


def test_full_receipt_v2_preserves_profiles_and_same_port_trial_population(installed_receipt):
    """Exercise orchestration and offline verification while retaining all new reports."""
    receipt, calls, ports = installed_receipt
    smoke.validate_smoke_receipt(receipt)
    assert len(calls) == 10 and len(ports) == 10
    assert len(set(ports[5:8])) == 1
    assert len(set(ports[8:])) == 1
    assert receipt["schema_version"] == 2
    assert receipt["native_version"] == "3.21"
    assert receipt["cases"][0]["artifact"]["result"]["execution"]["native_version"] == "iperf 3.21"
    assert receipt["cases"][0]["rate_intent"]["resolution"]["native_per_stream_bps"] == 500000
    assert receipt["cases"][0]["rate_intent"]["resolution"]["unused_bps_per_direction"] == 1
    assert receipt["repeated_assessment"]["report"]["ci_exit_code"] == 0
    assert len(receipt["repeated_assessment"]["report"]["execution"]["trials"]) == 3
    sweep = receipt["sweep"]["report"]["result"]
    assert [cell["parameters"] for cell in sweep["prepared"]["cells"]] == [
        {"parallel": 1},
        {"parallel": 2},
    ]
    assert [
        record["artifact"]["result"]["raw"]["start"]["test_start"]["target_bitrate"]
        for record in sweep["execution"]["trials"]
    ] == [1_000_001, 500_000]
    assert sweep["comparisons"][0]["compatibility"]["compatible"] is True
    assert [case["profile"] for case in receipt["cases"]] == [
        "tcp-forward",
        "tcp-reverse",
        "tcp-bidirectional",
        "udp",
        "sctp",
    ]


def test_receipt_verification_uses_retained_environment_not_current_machine(
    installed_receipt, monkeypatch
):
    """Independent verification needs no native loading or locally matching package metadata."""
    receipt, _, _ = installed_receipt

    def forbidden(*args, **kwargs):
        pytest.fail("receipt verifier queried current native/package environment")

    monkeypatch.setattr("cffi.FFI.dlopen", forbidden)
    monkeypatch.setattr(importlib.metadata, "version", forbidden)
    monkeypatch.setattr("iperf3_lib.artifacts.version", forbidden)
    monkeypatch.setattr("iperf3_lib.sweep_reports.version", forbidden)
    smoke.validate_smoke_receipt(receipt)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=1),
        lambda d: d.update(source_revision="bad"),
        lambda d: d["artifact"].update(sha256="bad"),
        lambda d: d["capabilities"]["offline"]["library"].update(state="available"),
        lambda d: d["capabilities"]["probed"]["features"][0].update(runtime="completed"),
        lambda d: d["cases"].pop(),
        lambda d: d["cases"][0]["rate_intent"]["resolution"].update(unused_bps_per_direction=0),
        lambda d: d["cases"][0]["analysis"]["summary"][0].update(throughput_bps=1),
        lambda d: d["cases"][0]["transport"].update(tcp_interval_count=0),
        lambda d: d["cases"][0]["capabilities"]["execution"].update(status="not_provided"),
        lambda d: d["repeated_assessment"]["report"]["assessment"].update(median_throughput_bps=0),
        lambda d: d["repeated_assessment"].update(text="not the retained report"),
        lambda d: d["saved_artifacts"]["failed_native"]["result"].update(ok=True),
    ],
)
def test_offline_receipt_validator_rejects_missing_or_changed_evidence(installed_receipt, mutate):
    """Reported success cannot hide absent profiles, altered analysis, or mismatched identity."""
    receipt, _, _ = installed_receipt
    mutate(receipt)
    with pytest.raises((ValueError, KeyError, TypeError)):
        smoke.validate_smoke_receipt(receipt)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d["producer"].update(version="another-package-version"),
        lambda d: d["result"]["prepared"]["axes"][0].update(values=[2, 1]),
        lambda d: d["result"]["prepared"]["cells"].reverse(),
        lambda d: d["result"]["execution"]["plan"]["budget"].update(max_active_seconds=3),
        lambda d: d["result"]["execution"]["trials"].pop(),
        lambda d: d["result"]["cells"][0]["directions"][0].update(median_throughput_bps=1),
        lambda d: d["result"]["cells"][0]["directions"][0]["samples"][0].update(
            eligible_for_cell=False
        ),
        lambda d: d["result"]["comparisons"].clear(),
        lambda d: d["result"]["comparison_policy"].update(allowed_differences={"rate": "changed"}),
    ],
)
def test_sweep_receipt_rejects_changed_plan_evidence_or_comparison(installed_receipt, mutate):
    """Two-cell qualification retains exact order, bounds, native samples and methodology."""
    receipt, _, _ = installed_receipt
    mutate(receipt["sweep"]["report"])
    with pytest.raises((ValueError, KeyError, TypeError)):
        smoke.validate_sweep(
            receipt["sweep"],
            package_version=receipt["package_version"],
            native_version=receipt["native_version"],
        )


def test_admitted_executor_rejects_fixture_endpoint_replacement(monkeypatch):
    """An occupied fixture port cannot be silently replaced to qualify a changed trial."""
    from iperf3_lib.trials import TrialSpec

    @contextmanager
    def changed_server(executable, *, port):
        yield "127.0.0.1", port + 1

    monkeypatch.setattr(smoke, "native_server", changed_server)
    spec = TrialSpec("one", "cell", "measured", 0, ClientConfig("127.0.0.1", duration=1))
    with pytest.raises(ValueError, match="changed the admitted endpoint"):
        smoke.execute_admitted_trial("fixture", spec)


@pytest.mark.parametrize("field", ["bytes", "duration_seconds"])
def test_summary_qualification_rejects_missing_evidence(field):
    """Reported bitrate is never a fallback for absent bytes or measured duration."""
    result, _ = native_result()
    setattr(result.flows[0].receiver, field, None)
    with pytest.raises(ValueError, match="byte/time"):
        smoke.qualify_analysis(result)


@pytest.mark.parametrize("value", [True, 1.0])
def test_rate_receipt_keeps_exact_integer_scalar_types(value):
    """JSON booleans and floats cannot impersonate the integer remainder receipt."""
    config = ClientConfig("127.0.0.1", duration=1, parallel=2)
    intent = RateIntent(aggregate_bps_per_direction=1_000_001)
    result, _ = native_result(config=config, intent=intent)
    result.extensions["iperf3_lib.rate_intent"]["resolution"]["unused_bps_per_direction"] = value
    with pytest.raises(ValueError, match="native allocation"):
        smoke.qualify_rate_intent(result, config, intent)


@pytest.mark.parametrize("value", [True, 1.0])
def test_duplicated_rate_receipt_keeps_exact_scalar_types(installed_receipt, value):
    """The separately retained view must exactly match its canonical extension."""
    receipt, _, _ = installed_receipt
    receipt["cases"][0]["rate_intent"]["resolution"]["unused_bps_per_direction"] = value
    with pytest.raises(ValueError, match="rate-intent report"):
        smoke.validate_smoke_receipt(receipt)


@pytest.mark.parametrize("key,value", [("server", "localhost"), ("port", 9999), ("omit", 1)])
def test_native_defaults_must_match_admitted_smoke_configuration(key, value):
    """Native effective endpoint and omitted time must agree with the bounded request."""
    result, config = native_result()
    result.execution.configuration.effective[key].value = value
    with pytest.raises(ValueError, match=f"effective {key}"):
        smoke.verify_native_provenance(result, config)


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
def test_native_json_and_abi_version_formats_are_matched_without_rewriting(version):
    """Both qualified producers retain the iperf prefix absent from the ABI getter."""
    result, _ = native_result(version=version)
    assert smoke.native_version_matches(result.execution.native_version, version)
    assert result.execution.native_version == f"iperf {version}"


@pytest.mark.parametrize(
    "recorded,abi",
    [
        ("iperf 3.19.1", "3.21"),
        ("iperf 3.21", "3.19.1"),
        ("iperf 13.21", "3.21"),
        ("iperf 3.210", "3.21"),
        ("iperf 3.21 extra", "3.21"),
        ("extra iperf 3.21", "3.21"),
        ("3.21", "3.21"),
        ("iperf iperf 3.21", "iperf 3.21"),
        ("iperf 3.21 extra", "3.21 extra"),
        (None, "3.21"),
    ],
)
def test_native_version_matching_rejects_mismatch_and_lookalikes(recorded, abi):
    """A substring or altered version prefix cannot qualify retained native identity."""
    assert not smoke.native_version_matches(recorded, abi)


@pytest.mark.parametrize(
    "target", ["interval_units", "interval_receipts", "cpu_value", "cpu_locality", "summary_units"]
)
def test_transport_qualification_rejects_units_or_provenance_drift(target):
    """Inspect native receipts rather than accepting merely populated normalized fields."""
    result, _ = native_result()
    tcp = next(item.tcp for item in result.intervals if item.tcp)
    if target == "interval_units":
        tcp.smoothed_rtt_seconds = 123
    elif target == "interval_receipts":
        tcp.evidence_paths.pop("smoothed_rtt_seconds")
    elif target == "cpu_value":
        result.cpu[0].total_percent = 12345
    elif target == "cpu_locality":
        result.cpu[0].locality = "remote"
    else:
        result.streams[0].sender.tcp.native_mean_sampled_rtt_seconds = 123
    with pytest.raises(ValueError):
        smoke.qualify_transport_evidence(result)


def test_offline_qualification_starts_before_any_native_load(monkeypatch):
    """Even a package-import regression that attempts native loading fails the smoke."""
    import cffi

    def unexpectedly_loading(version):
        cffi.FFI().dlopen("unexpected-native-library")

    monkeypatch.setattr(smoke, "require_installed_package", unexpectedly_loading)
    with pytest.raises(ValueError, match="offline installed qualification"):
        smoke.qualify("0.2.0", "3.21")


@pytest.mark.parametrize("port", [True, 0, 65536, "5201"])
def test_explicit_native_fixture_port_is_strict(port):
    """Invalid declared ports fail before creating a fixture process."""
    with pytest.raises(ValueError, match="port"):
        with smoke.native_server("unreachable-executable", port=port):
            pytest.fail("entered an invalid fixture")


@pytest.mark.parametrize(
    "field,value", [("omit", 1), ("blksize", 1024), ("tos", 1), ("server", "localhost")]
)
def test_valid_but_different_profile_cannot_relabel_bounded_smoke(installed_receipt, field, value):
    """A different valid configuration still fails this fixed qualification profile."""
    receipt, _, _ = installed_receipt
    case = receipt["cases"][0]
    config = ClientConfig(**case["rate_intent"]["caller_config"])
    setattr(config, field, value)
    intent = RateIntent(aggregate_bps_per_direction=1_000_001)
    result, _ = native_result(config=config, intent=intent)
    case["artifact"] = smoke.round_trip_artifact(result)
    case["result"] = smoke.json_value(result.to_dict())
    with pytest.raises(ValueError, match="bounded configuration"):
        smoke.validate_smoke_receipt(receipt)


def test_receipt_requires_capability_feature_coverage_and_default_trial_policy(installed_receipt):
    """Empty capability output or extra trial pauses cannot pass a different smoke contract."""
    receipt, _, _ = installed_receipt
    data = smoke.json_value(receipt)
    data["capabilities"]["offline"]["features"] = []
    with pytest.raises(ValueError, match="feature identities"):
        smoke.validate_smoke_receipt(data)
    data = smoke.json_value(receipt)
    plan = data["repeated_assessment"]["report"]["execution"]["plan"]
    plan["policy"]["pause_seconds"] = 1
    plan["planned_pause_seconds"] = 2
    with pytest.raises(ValueError, match="population contract"):
        smoke.validate_smoke_receipt(data)
