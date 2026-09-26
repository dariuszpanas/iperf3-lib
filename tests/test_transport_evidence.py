"""Qualify TCP/CPU observations against retained native evidence and strict artifacts."""

import copy
import json
from pathlib import Path

import pytest

from iperf3_lib.artifacts import (
    artifact_from_result,
    artifact_to_dict,
    dumps_artifact,
    loads_artifact,
)
from iperf3_lib.result import result_from_iperf_json

NATIVE = Path(__file__).parent / "fixtures" / "native"
INTERVAL_FIELDS = {
    "smoothed_rtt_seconds": "rtt",
    "rtt_variation_seconds": "rttvar",
    "send_congestion_window_bytes": "snd_cwnd",
    "advertised_send_window_bytes": "snd_wnd",
    "path_mtu_bytes": "pmtu",
}
SUMMARY_FIELDS = {
    "minimum_sampled_rtt_seconds": "min_rtt",
    "maximum_sampled_rtt_seconds": "max_rtt",
    "native_mean_sampled_rtt_seconds": "mean_rtt",
    "maximum_send_congestion_window_bytes": "max_snd_cwnd",
    "maximum_advertised_send_window_bytes": "max_snd_wnd",
}


def _raw(version="3.21", scenario="tcp-forward", role="client"):
    return json.loads((NATIVE / version / f"{scenario}-{role}.json").read_text(encoding="utf-8"))


def _assert_fields(model, native, mapping, prefix):
    for canonical, key in mapping.items():
        expected = native.get(key)
        if expected is not None and expected >= 0:
            assert getattr(model, canonical) == (
                expected / 1_000_000 if canonical.endswith("seconds") else expected
            )
            assert model.evidence_paths[canonical] == f"{prefix}/{key}"
        else:
            assert getattr(model, canonical) is None
            assert canonical not in model.evidence_paths


@pytest.mark.parametrize(
    "fixture", sorted(NATIVE.glob("*/*.json")), ids=lambda item: f"{item.parent.name}-{item.stem}"
)
def test_native_tcp_and_cpu_evidence_matches_local_endpoint_and_units(fixture):
    """Both native versions and both endpoint roles preserve exact qualified values."""
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    original = copy.deepcopy(raw)
    result = result_from_iperf_json(raw)
    role = "client" if fixture.stem.endswith("client") else "server"
    cpu = {item.locality: item for item in result.cpu}
    assert cpu["local"].endpoint == role
    assert cpu["remote"].endpoint == ("server" if role == "client" else "client")
    for locality, prefix in (("local", "host"), ("remote", "remote")):
        for part in ("total", "user", "system"):
            field = f"{part}_percent"
            source = f"/raw/end/cpu_utilization_percent/{prefix}_{part}"
            if locality == "remote" and role == "server":
                assert getattr(cpu[locality], field) is None
                assert result.availability[f"/cpu/1/{field}"].state == "unknown"
            else:
                assert (
                    getattr(cpu[locality], field)
                    == raw["end"]["cpu_utilization_percent"][f"{prefix}_{part}"]
                )
                assert cpu[locality].evidence_paths[field] == source
    canonical_streams = [interval for interval in result.intervals if interval.scope == "stream"]
    native_streams = [
        (i, j, stream)
        for i, interval in enumerate(raw["intervals"])
        for j, stream in enumerate(interval["streams"])
    ]
    for interval, (i, j, native) in zip(canonical_streams, native_streams, strict=True):
        if result.protocol == "tcp" and native["sender"]:
            assert interval.tcp is not None
            _assert_fields(interval.tcp, native, INTERVAL_FIELDS, f"/raw/intervals/{i}/streams/{j}")
        else:
            assert interval.tcp is None
    for index, (stream, native) in enumerate(
        zip(result.streams, raw["end"]["streams"], strict=True)
    ):
        sender = native.get("sender")
        if result.protocol == "tcp" and sender and sender["sender"]:
            assert stream.sender.tcp is not None
            _assert_fields(
                stream.sender.tcp, sender, SUMMARY_FIELDS, f"/raw/end/streams/{index}/sender"
            )
        elif stream.sender is not None:
            assert stream.sender.tcp is None
        if stream.receiver is not None:
            assert stream.receiver.tcp is None
    assert all(
        interval.tcp is None for interval in result.intervals if interval.scope == "aggregate"
    )
    assert raw == original
    assert loads_artifact(dumps_artifact(artifact_from_result(result))).result == result


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
def test_bidirectional_remote_sender_tcp_numbers_are_not_promoted(version):
    """Local receiver TCP_INFO can be positive under native remote-sender end objects."""
    raw = _raw(version, "tcp-bidirectional")
    result = result_from_iperf_json(raw)
    remote = [
        (i, native["sender"])
        for i, native in enumerate(raw["end"]["streams"])
        if not native["sender"]["sender"]
    ]
    assert remote
    assert any(native["max_snd_cwnd"] > 0 for _, native in remote)
    for index, _ in remote:
        assert result.streams[index].sender.tcp is None
        assert result.availability[f"/streams/{index}/sender/tcp"].state == "unknown"


@pytest.mark.parametrize("version", [None, "iperf 99.0"])
def test_unqualified_producer_keeps_measurements_raw_with_unknown_availability(version):
    """Numeric presence alone cannot establish qualified TCP/CPU semantics."""
    raw = _raw()
    raw["start"]["version"] = version
    result = result_from_iperf_json(raw)
    assert all(interval.tcp is None for interval in result.intervals)
    assert all(stream.sender.tcp is None for stream in result.streams)
    assert all(item.total_percent is None for item in result.cpu)
    assert any(
        path.endswith("/tcp") and state.state == "unknown"
        for path, state in result.availability.items()
    )
    assert result.raw == raw
    artifact_to_dict(artifact_from_result(result))


def test_unknown_role_keeps_local_cpu_locality_and_remote_cpu_unknown():
    """Unknown endpoint identity does not turn remote placeholder data into observations."""
    raw = _raw()
    del raw["start"]["connecting_to"]
    result = result_from_iperf_json(raw)
    assert result.reporting_role is None
    assert result.cpu[0].endpoint == "unknown"
    assert result.cpu[0].total_percent == raw["end"]["cpu_utilization_percent"]["host_total"]
    assert result.cpu[1].total_percent is None
    artifact_to_dict(artifact_from_result(result))


@pytest.mark.parametrize("field", list(INTERVAL_FIELDS.values()))
@pytest.mark.parametrize("value", [True, -2, 1.5, float("inf")])
def test_invalid_native_tcp_numbers_reject(field, value):
    """TCP getters return integers; malformed values cannot acquire normalized units."""
    raw = _raw()
    raw["intervals"][0]["streams"][0][field] = value
    with pytest.raises(ValueError):
        result_from_iperf_json(raw)


@pytest.mark.parametrize("field,canonical", [(key, name) for name, key in INTERVAL_FIELDS.items()])
def test_native_tcp_unavailable_sentinel_is_never_zero(field, canonical):
    """Source-defined -1 getters retain unsupported evidence on qualified producers."""
    raw = _raw()
    raw["intervals"][0]["streams"][0][field] = -1
    result = result_from_iperf_json(raw)
    index, interval = next(
        (i, item) for i, item in enumerate(result.intervals) if item.scope == "stream"
    )
    assert getattr(interval.tcp, canonical) is None
    assert result.availability[f"/intervals/{index}/tcp/{canonical}"].state == "unsupported"
    artifact_to_dict(artifact_from_result(result))


def test_native_cpu_above_one_hundred_and_observed_zero_are_preserved():
    """Process CPU time divided by elapsed time is not capped at one logical CPU."""
    raw = _raw()
    raw["end"]["cpu_utilization_percent"].update(
        host_total=250.5,
        host_user=249.5,
        host_system=1,
        remote_total=0,
        remote_user=0,
        remote_system=0,
    )
    result = result_from_iperf_json(raw)
    assert result.cpu[0].total_percent == 250.5
    assert result.cpu[1].total_percent == 0
    artifact_to_dict(artifact_from_result(result))


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_invalid_native_cpu_values_reject(value):
    """CPU percentages remain finite and nonnegative without a 100-percent ceiling."""
    raw = _raw()
    raw["end"]["cpu_utilization_percent"]["host_total"] = value
    with pytest.raises(ValueError):
        result_from_iperf_json(raw)


def test_socket_conflict_does_not_promote_tcp_sender_evidence():
    """A conflicting local-role record cannot turn uncertain TCP_INFO into sender facts."""
    raw = _raw()
    raw["end"]["streams"][0]["receiver"]["sender"] = False
    result = result_from_iperf_json(raw)
    interval = next(item for item in result.intervals if item.scope == "stream")
    assert interval.observation is None
    assert interval.tcp is None
    assert result.streams[0].sender.tcp is None
    artifact_to_dict(artifact_from_result(result))


def test_missing_tcp_cpu_fields_stay_absent_without_fabricated_zeros():
    """Missing getter fields remain null and do not acquire observation receipts."""
    raw = _raw()
    del raw["intervals"][0]["streams"][0]["rttvar"]
    del raw["end"]["cpu_utilization_percent"]["host_user"]
    result = result_from_iperf_json(raw)
    interval = next(item for item in result.intervals if item.scope == "stream")
    assert interval.tcp.rtt_variation_seconds is None
    assert "rtt_variation_seconds" not in interval.tcp.evidence_paths
    assert result.cpu[0].user_percent is None
    artifact_to_dict(artifact_from_result(result))


@pytest.mark.parametrize("platform", [None, "Darwin unknown", "Windows unknown"])
def test_unqualified_platform_retains_raw_tcp_and_cpu(platform):
    """Native version alone cannot establish the tested Linux field interpretation."""
    raw = _raw()
    raw["start"]["system_info"] = platform
    result = result_from_iperf_json(raw)
    assert all(interval.tcp is None for interval in result.intervals)
    assert all(item.total_percent is None for item in result.cpu)
    assert result.raw == raw
