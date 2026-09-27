"""Offline archive and admission regressions for expanded native configuration."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from iperf3_lib.artifacts import (
    ArtifactValidationError,
    artifact_from_dict,
    artifact_from_result,
    artifact_to_dict,
    dumps_artifact,
    loads_artifact,
)
from iperf3_lib.config import (
    LEGACY_CONFIG_FIELDS,
    ClientConfig,
    Protocol,
    config_from_dict,
    config_to_dict,
)
from iperf3_lib.intent import estimate_plan
from iperf3_lib.reports import plan_result_from_dict, plan_result_to_dict
from iperf3_lib.result import ConfigurationSnapshot, VerifiedSetting, result_from_iperf_json
from iperf3_lib.trials import (
    PlanBudget,
    TrialPolicy,
    TrialSpec,
    prepare_plan,
    prepare_trials,
    run_plan,
)

OBSERVED = {
    "bind_address": "127.0.0.1",
    "bind_device": "lo",
    "client_port": 0,
    "address_family": "ipv4",
    "socket_buffer_bytes": 0,
    "congestion_control": "cubic",
    "no_delay": False,
    "mss": 0,
    "connect_timeout_ms": 0,
    "bytes_to_send": 0,
    "blocks_to_send": 0,
    "interval_seconds": 0.0,
    "pacing_timer_us": 0,
    "fq_rate_bps": 0,
    "burst_packets": 0,
    "zerocopy": False,
    "skip_rx_copy": False,
    "udp_counters_64bit": False,
    "dont_fragment": False,
    "flow_label": 0,
    "sctp_streams": 0,
    "sctp_bind_addresses": [],
    "payload_file": "payload.bin",
    "repeating_payload": False,
    "affinity": 0,
    "server_affinity": 0,
    "get_server_output": False,
    "title": "offline",
    "extra_data": "preserved",
    "receive_timeout_ms": 0,
    "send_timeout_ms": 0,
    "control_keepalive": [0, 0, 0],
    "gsro": False,
    "username": "fixture-user",
    "rsa_public_key_path": "public.pem",
    "use_pkcs1_padding": False,
}


def setting_artifact(name, value):
    """Use retained failed-run evidence without admitting or executing a request."""
    result = result_from_iperf_json({"error": "offline fixture"}, reporting_role="client")
    result.raw["receipt"] = {name: value}
    result.execution.configuration = ConfigurationSnapshot(
        effective={name: VerifiedSetting(value, "verified", [f"/raw/receipt/{name}"])}
    )
    return artifact_from_result(result)


def test_observed_examples_cover_every_extended_configuration_field():
    """An added field must obtain an explicit observed-value archive regression."""
    assert set(OBSERVED) == {item.name for item in fields(ClientConfig)} - LEGACY_CONFIG_FIELDS


@pytest.mark.parametrize("name,value", OBSERVED.items())
def test_extended_effective_values_roundtrip_without_request_only_bounds(name, value):
    """Preserve valid observed zero/false/default values independently of request bounds."""
    artifact = setting_artifact(name, value)
    restored = loads_artifact(dumps_artifact(artifact))
    assert restored == artifact
    effective = restored.result.execution.configuration.effective[name]
    assert effective.value == value
    assert type(effective.value) is type(value)
    assert effective.evidence_paths == [f"/raw/receipt/{name}"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("no_delay", 0),
        ("zerocopy", 1),
        ("skip_rx_copy", "false"),
        ("udp_counters_64bit", 1.0),
        ("dont_fragment", []),
        ("repeating_payload", {}),
        ("get_server_output", "true"),
        ("gsro", 0),
        ("use_pkcs1_padding", 1),
        ("bind_address", False),
        ("bind_device", 1),
        ("congestion_control", []),
        ("payload_file", {}),
        ("title", 1.0),
        ("extra_data", False),
        ("username", 1),
        ("rsa_public_key_path", []),
        ("client_port", True),
        ("socket_buffer_bytes", 1.0),
        ("mss", "1200"),
        ("connect_timeout_ms", {}),
        ("bytes_to_send", False),
        ("blocks_to_send", 1.0),
        ("pacing_timer_us", "1000"),
        ("fq_rate_bps", []),
        ("burst_packets", True),
        ("flow_label", "1"),
        ("sctp_streams", 1.0),
        ("affinity", False),
        ("server_affinity", []),
        ("receive_timeout_ms", "100"),
        ("send_timeout_ms", True),
        ("address_family", "inet"),
        ("address_family", 4),
        ("interval_seconds", True),
        ("interval_seconds", "0.5"),
        ("interval_seconds", float("inf")),
        ("sctp_bind_addresses", "127.0.0.1"),
        ("sctp_bind_addresses", [True]),
        ("control_keepalive", [1, 2]),
        ("control_keepalive", [1, 2, 3, 4]),
        ("control_keepalive", [True, 0, 0]),
        ("control_keepalive", [0, 0.0, 0]),
    ],
)
def test_extended_effective_values_reject_type_coercion_on_encode_and_decode(name, value):
    """Both archive directions reject invalid effective values before trusting receipts."""
    artifact = setting_artifact(name, OBSERVED[name])
    mapping = artifact_to_dict(artifact)
    mapping["result"]["execution"]["configuration"]["effective"][name]["value"] = value
    path = f"/result/execution/configuration/effective/{name}/value"
    with pytest.raises(ArtifactValidationError) as error:
        artifact_from_dict(mapping)
    assert error.value.path.startswith(path)
    artifact.result.execution.configuration.effective[name].value = value
    with pytest.raises(ArtifactValidationError) as error:
        artifact_to_dict(artifact)
    assert error.value.path.startswith(path)


@pytest.mark.parametrize("target", [{"bytes_to_send": 1001}, {"blocks_to_send": 3}])
def test_count_configuration_and_requested_artifact_roundtrip_preserve_tuples(target):
    """Expanded typed requests survive JSON without inventing a duration or password."""
    config = ClientConfig(
        "127.0.0.1",
        protocol=Protocol.SCTP,
        duration=None,
        blksize=4096,
        sctp_streams=2,
        sctp_bind_addresses=("127.0.0.1",),
        control_keepalive=(10, 1, 3),
        **target,
    )
    requested = config_to_dict(config)
    assert requested["duration"] is None
    assert requested["sctp_bind_addresses"] == ["127.0.0.1"]
    assert requested["control_keepalive"] == [10, 1, 3]
    assert "password" not in requested
    assert config_from_dict(json.loads(json.dumps(requested))) == config
    result = result_from_iperf_json({"error": "saved failed attempt"}, reporting_role="client")
    result.execution.configuration.requested = requested
    artifact = artifact_from_result(result)
    restored = loads_artifact(dumps_artifact(artifact))
    assert restored == artifact
    assert restored.result.execution.configuration.requested == requested
    assert restored.result.execution.status == "failed"
    requested["sctp_bind_addresses"].append("127.0.0.2")
    assert restored.result.execution.configuration.requested["sctp_bind_addresses"] == ["127.0.0.1"]


def test_original_thirteen_field_request_still_decodes_without_rewriting_history():
    """The extended reader accepts the historical complete legacy field set."""
    result = result_from_iperf_json({"error": "historical failure"}, reporting_role="client")
    requested = config_to_dict(ClientConfig("127.0.0.1"), compact=True)
    assert set(requested) == LEGACY_CONFIG_FIELDS
    result.execution.configuration.requested = requested
    original = artifact_from_result(result)
    restored = loads_artifact(dumps_artifact(original))
    assert restored == original
    assert set(restored.result.execution.configuration.requested) == LEGACY_CONFIG_FIELDS


@pytest.mark.parametrize(
    "termination",
    [
        {"duration": None, "bytes_to_send": 1001},
        {"duration": None, "blocks_to_send": 3},
        {"duration": 0},
    ],
)
def test_count_and_unlimited_estimates_stay_unknown_even_with_finite_rate(termination):
    """Never multiply a count or unlimited duration into a fabricated time/byte estimate."""
    config = ClientConfig("127.0.0.1", rate=8000, omit=2, parallel=2, **termination)
    estimate = estimate_plan([config])
    assert estimate.active_seconds is None
    assert estimate.estimated_payload_bits is None
    assert estimate.estimated_payload_bytes is None
    assert estimate.runs[0].active_seconds is None
    assert estimate.runs[0].estimated_payload_bits is None
    assert estimate.runs[0].rate.aggregate_bps_per_direction == 16000
    with pytest.raises(ValueError, match="active-time estimate is unknown"):
        estimate_plan([config], max_active_seconds=100)
    with pytest.raises(ValueError, match="payload estimate is unbounded"):
        estimate_plan([config], max_payload_bytes=10**9)
    mixed = estimate_plan([ClientConfig("127.0.0.1", duration=1, rate=8000), config])
    assert mixed.runs[0].active_seconds == 1
    assert mixed.runs[0].estimated_payload_bits == 8000
    assert mixed.active_seconds is mixed.estimated_payload_bytes is None


@pytest.mark.parametrize("target", [{"bytes_to_send": 1001}, {"blocks_to_send": 3}])
def test_count_plan_archives_roundtrip_unknown_estimates_and_expanded_requests(target):
    """Finite count trials retain admission unknowns and all detached request metadata."""
    config = ClientConfig(
        "127.0.0.1", duration=None, bind_address="127.0.0.1", control_keepalive=(0, 0, 0), **target
    )
    policy = TrialPolicy(repetitions=1)
    for budget in (PlanBudget(100, None), PlanBudget(None, 10**9)):
        with pytest.raises(ValueError, match="estimate"):
            prepare_trials(config, policy=policy, budget=budget)
    plan = prepare_trials(config, policy=policy, budget=PlanBudget(None, None))
    called = []

    def execute(spec):
        called.append(spec.config)
        result = result_from_iperf_json({"error": "offline refusal"}, reporting_role="client")
        result.execution.configuration.requested = config_to_dict(spec.config)
        return result

    execution = run_plan(plan, executor=execute)
    assert len(called) == 1
    assert execution.trials[0].status == "failed"
    mapping = plan_result_to_dict(execution)
    restored = plan_result_from_dict(json.loads(json.dumps(mapping)))
    assert restored == execution
    assert restored.plan.estimate.active_seconds is None
    assert restored.plan.estimate.estimated_payload_bytes is None
    assert restored.plan.trials[0].config == config
    assert restored.plan.trials[0].config.control_keepalive == (0, 0, 0)


def test_finite_trials_reject_unlimited_duration_before_executor_admission():
    """Neither convenience expansion nor explicit plans admit an infinite native run."""
    config = ClientConfig("127.0.0.1", duration=0, rate=8000)
    policy = TrialPolicy(repetitions=1)
    budget = PlanBudget(None, None)
    with pytest.raises(ValueError, match="unlimited duration"):
        prepare_trials(config, policy=policy, budget=budget)
    with pytest.raises(ValueError, match="unlimited duration"):
        prepare_plan(
            [TrialSpec("infinite", "one", "measured", 0, config)], policy=policy, budget=budget
        )
    finite = prepare_trials(ClientConfig("127.0.0.1"), policy=policy, budget=budget)
    finite.trials[0].config.duration = 0
    with pytest.raises(ValueError, match="unlimited duration"):
        run_plan(finite, executor=lambda _: pytest.fail("infinite native run was admitted"))
