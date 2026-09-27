"""Advanced compatibility uses native receipts and preserves historical reports."""

from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest
from test_analysis import _policy, _result
from trial_helpers import measured, report

from iperf3_lib.analysis import AnalysisTrial, check_compatibility
from iperf3_lib.config import LEGACY_CONFIG_FIELDS, ClientConfig, config_to_dict
from iperf3_lib.reports import (
    ReportValidationError,
    dumps_report,
    loads_report,
    report_from_dict,
    report_to_dict,
)
from iperf3_lib.result import VerifiedSetting

ADVANCED = {
    "mptcp": True,
    "json_stream": True,
    "bind_address": "127.0.0.2",
    "bind_device": "lo",
    "client_port": 12345,
    "address_family": "ipv6",
    "socket_buffer_bytes": 32768,
    "congestion_control": "cubic",
    "no_delay": True,
    "mss": 1400,
    "connect_timeout_ms": 1000,
    "bytes_to_send": 4096,
    "blocks_to_send": 2,
    "interval_seconds": 0.5,
    "pacing_timer_us": 100,
    "fq_rate_bps": 500000,
    "burst_packets": 3,
    "zerocopy": True,
    "skip_rx_copy": True,
    "udp_counters_64bit": True,
    "dont_fragment": True,
    "flow_label": 12,
    "sctp_streams": 2,
    "sctp_bind_addresses": ["127.0.0.1"],
    "payload_file": "/tmp/payload",
    "repeating_payload": True,
    "affinity": 0,
    "server_affinity": 1,
    "get_server_output": True,
    "title": "trial",
    "extra_data": "metadata",
    "receive_timeout_ms": 1000,
    "send_timeout_ms": 1000,
    "control_keepalive": [10, 2, 3],
    "gsro": True,
    "username": "qualification",
    "rsa_public_key_path": "/tmp/public.pem",
    "use_pkcs1_padding": True,
}


def _observation(result, name, value):
    """Attach an explicitly synthetic getter receipt to a unit-test result."""
    result.extensions.setdefault("example.native_receipts", {})[name] = copy.deepcopy(value)
    result.execution.configuration.effective[name] = VerifiedSetting(
        copy.deepcopy(value), "verified", [f"/extensions/example.native_receipts/{name}"]
    )


def _advanced(name, value):
    result = _result()
    result.execution.configuration.requested = {name: copy.deepcopy(value)}
    _observation(result, name, value)
    return result


def _compare(*results, policy=None):
    return check_compatibility(
        [AnalysisTrial(str(index), result) for index, result in enumerate(results)],
        policy=policy or _policy(),
    )


def test_every_admitted_advanced_field_has_explicit_comparison_coverage():
    """Prevent future ClientConfig additions from silently escaping comparison review."""
    assert set(ADVANCED) == (
        {item.name for item in fields(ClientConfig)} - LEGACY_CONFIG_FIELDS
    ) | {"mptcp", "json_stream"}


@pytest.mark.parametrize("name,value", ADVANCED.items())
def test_nondefault_advanced_requests_require_and_retain_verified_observations(name, value):
    """A nondefault request selects the dimension but its native receipt supplies the value."""
    result = _advanced(name, value)
    compared = _compare(result, copy.deepcopy(result))
    assert compared.compatible and compared.quality == "complete"
    assert compared.fingerprints["0"][name] == value
    assert compared.fingerprints["1"][name] == value
    result.execution.configuration.effective.pop(name)
    missing = _compare(result, copy.deepcopy(result))
    assert not missing.compatible
    assert missing.fingerprints["0"][name] is None


def test_any_trial_selects_field_for_all_trials_without_assumed_default():
    """An unrequested setting in the peer still needs an actual observation."""
    requested = _advanced("congestion_control", "cubic")
    peer = _result()
    compared = _compare(requested, peer)
    assert not compared.compatible
    assert compared.fingerprints["1"]["congestion_control"] is None
    _observation(peer, "congestion_control", "cubic")
    assert _compare(requested, peer).compatible


@pytest.mark.parametrize(
    "policy",
    [
        _policy(),
        _policy(varying_fields=("congestion_control",)),
        _policy(allowed_differences={"congestion_control": "evaluate a transport change"}),
    ],
)
def test_difference_permission_never_supplies_missing_advanced_evidence(policy):
    """Even identical requests and explicit policy allowances cannot invent native proof."""
    result = _advanced("congestion_control", "cubic")
    result.execution.configuration.effective["congestion_control"].state = "unavailable"
    compared = _compare(result, result, policy=policy)
    assert not compared.compatible
    assert any("congestion_control" in diagnostic.message for diagnostic in compared.diagnostics)


@pytest.mark.parametrize(
    "policy",
    [
        _policy(varying_fields=("no_delay",)),
        _policy(allowed_differences={"no_delay": "controlled TCP experiment"}),
    ],
)
def test_policy_mention_selects_default_only_advanced_field(policy):
    """Policy-selected dimensions are checked even when requests never enabled them."""
    result = _result()
    compared = _compare(result, policy=policy)
    assert compared.fingerprints["0"]["no_delay"] is None
    assert not compared.compatible
    _observation(result, "no_delay", False)
    assert _compare(result, policy=policy).compatible


def test_actual_difference_requires_recorded_policy_and_uses_effective_value():
    """An intentional native difference is neither hidden by equal requests nor auto-allowed."""
    first = _advanced("congestion_control", "cubic")
    second = copy.deepcopy(first)
    _observation(second, "congestion_control", "reno")
    denied = _compare(first, second)
    assert not denied.compatible
    assert denied.fingerprints["1"]["congestion_control"] == "reno"
    allowed = _compare(
        first,
        second,
        policy=_policy(allowed_differences={"congestion_control": "controlled comparison"}),
    )
    assert allowed.compatible and allowed.quality == "partial"
    assert any(item.code == "comparison.allowed_difference" for item in allowed.diagnostics)
    varying = _compare(first, second, policy=_policy(varying_fields=("congestion_control",)))
    assert varying.compatible and varying.quality == "complete"


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["/raw/missing"],
        ["/execution/configuration/requested/no_delay"],
        ["/extensions/unnamespaced/no_delay"],
    ],
)
def test_advanced_observation_must_have_an_existing_native_receipt(paths):
    """A setter request or fictitious JSON pointer cannot verify a boolean."""
    result = _advanced("no_delay", True)
    result.execution.configuration.effective["no_delay"].evidence_paths = paths
    assert not _compare(result).compatible


@pytest.mark.parametrize(
    "name,value",
    [
        ("no_delay", 1),
        ("socket_buffer_bytes", True),
        ("socket_buffer_bytes", 1.0),
        ("socket_buffer_bytes", -1),
        ("mss", 65536),
        ("pacing_timer_us", 2**31),
        ("congestion_control", ""),
        ("congestion_control", "a\0b"),
        ("address_family", "inet6"),
        ("interval_seconds", True),
        ("interval_seconds", float("nan")),
        ("interval_seconds", float("inf")),
        ("sctp_bind_addresses", "127.0.0.1"),
        ("sctp_bind_addresses", [""]),
        ("control_keepalive", [1, 2]),
        ("control_keepalive", [True, 2, 3]),
    ],
)
def test_advanced_native_values_have_strict_frozen_types(name, value):
    """Malformed effective data cannot compare successfully merely by equality."""
    result = _advanced(name, ADVANCED[name])
    _observation(result, name, value)
    with pytest.raises(ValueError, match="effective"):
        _compare(result)


def test_array_fingerprints_are_detached_from_mutable_result_receipts():
    """Returned evidence is stable if the original canonical object is later mutated."""
    result = _advanced("sctp_bind_addresses", ["127.0.0.1"])
    compared = _compare(result)
    result.execution.configuration.effective["sctp_bind_addresses"].value.append("127.0.0.2")
    assert compared.fingerprints["0"]["sctp_bind_addresses"] == ["127.0.0.1"]


def test_verified_literal_unknown_text_is_not_missing_provenance():
    """Arbitrary extra-data text can literally be unknown without losing its receipt."""
    result = _advanced("extra_data", "unknown")
    assert _compare(result).compatible


def _report_result(value="cubic", *, observed=True):
    result = measured()
    result.execution.configuration.requested = config_to_dict(
        ClientConfig("127.0.0.1", duration=1, parallel=2, rate=4000000, congestion_control=value)
    )
    if observed:
        _observation(result, "congestion_control", value)
    return result


@pytest.mark.parametrize("observed", [True, False])
def test_advanced_reports_roundtrip_without_current_public_analysis(monkeypatch, observed):
    """Frozen v1 rules preserve both proved advanced settings and conservative unknown outcomes."""
    original = report([_report_result(observed=observed) for _ in range(3)])
    data = report_to_dict(original)
    assert original.assessment.compatibility.compatible is observed

    def forbidden(*args, **kwargs):
        raise AssertionError("reader must use frozen rules")

    import iperf3_lib.analysis as current

    monkeypatch.setattr(current, "check_compatibility", forbidden)
    monkeypatch.setattr(current, "_fingerprint", forbidden)
    restored = report_from_dict(data)
    assert restored == original
    assert json.loads(dumps_report(restored)) == data


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("congestion_control"),
        lambda item: item.update(congestion_control="reno"),
        lambda item: item.update(congestion_control=None),
    ],
)
def test_report_reader_rejects_dropped_or_rewritten_advanced_fingerprint(mutation):
    """A stored comparison cannot omit the nondefault native setting it checked."""
    data = report_to_dict(report([_report_result() for _ in range(3)]))
    mutation(data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"])
    with pytest.raises(ReportValidationError, match="fingerprints"):
        report_from_dict(data)


def test_report_reader_rejects_forged_compatible_decision_with_unknown_advanced_setting():
    """Unknown native configuration stays incompatible in the archived verifier."""
    data = report_to_dict(report([_report_result(observed=False) for _ in range(3)]))
    data["assessment"]["compatibility"].update(compatible=True, quality="complete")
    with pytest.raises(ReportValidationError):
        report_from_dict(data)


def test_report_preserves_reasoned_advanced_difference_and_rejects_removed_reason():
    """A stored allowance is required for a fixed advanced field that actually differs."""
    original = report(
        [_report_result("cubic"), _report_result("reno"), _report_result("cubic")],
        comparison=_policy(allowed_differences={"congestion_control": "deliberate change"}),
    )
    data = report_to_dict(original)
    assert original.assessment.compatibility.compatible
    assert report_from_dict(data) == original
    data["comparison"]["allowed_differences"] = {}
    data["assessment"]["compatibility"]["policy"]["allowed_differences"] = {}
    with pytest.raises(ReportValidationError, match="compatibility"):
        report_from_dict(data)


@pytest.mark.parametrize("value", [True, 1.0])
def test_report_advanced_fingerprint_preserves_exact_numeric_type(value):
    """Integer one cannot be replaced by boolean true or a floating-point lookalike."""
    result = measured()
    result.execution.configuration.requested = config_to_dict(
        ClientConfig("127.0.0.1", duration=1, parallel=2, rate=4000000, pacing_timer_us=1)
    )
    _observation(result, "pacing_timer_us", 1)
    data = report_to_dict(report([copy.deepcopy(result) for _ in range(3)]))
    data["assessment"]["compatibility"]["fingerprints"]["trial:default:measured:0"][
        "pacing_timer_us"
    ] = value
    with pytest.raises(ReportValidationError, match="fingerprints"):
        report_from_dict(data)


def test_default_only_results_keep_original_fingerprint_keys_and_golden_serialization():
    """Historical archives never gain dimensions from newly declared default settings."""
    legacy = _compare(_result())
    result = _result()
    result.execution.configuration.requested = config_to_dict(ClientConfig("127.0.0.1"))
    assert _compare(result).fingerprints == legacy.fingerprints
    for fixture in (Path(__file__).parent / "fixtures/reports/v1").glob("*.json"):
        serialized = fixture.read_text()
        assert json.loads(dumps_report(loads_report(serialized))) == json.loads(serialized)
