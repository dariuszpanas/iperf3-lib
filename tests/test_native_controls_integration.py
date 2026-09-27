"""Bounded real-libiperf runs for expanded typed worker configuration."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from typing import Any

import pytest

from iperf3_lib import _execution
from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact
from iperf3_lib.config import ClientConfig
from iperf3_lib.exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from iperf3_lib.ffi.api import ffi, lib
from iperf3_lib.iperf_client import Client
from iperf3_lib.result import Result

pytestmark = pytest.mark.integration


def _run(iperf3_server: tuple[str, int], **options: Any) -> Result:
    host, port = iperf3_server
    duration = options.pop("duration", 1)
    result = Client(ClientConfig(server=host, port=port, duration=duration, **options)).run(
        timeout=15
    )
    assert result.ok, result.error
    assert result.raw["start"]["connected"]
    assert result.raw["end"]["sum_received"]["bytes"] > 0
    return result


def _receipts(result: Result, expected: dict[str, Any]) -> None:
    receipts = result.extensions["iperf3_lib.native_configuration"]
    assert isinstance(receipts, dict)
    for name, value in expected.items():
        receipt = receipts[name]
        assert isinstance(receipt, dict)
        assert receipt["value"] == value
        assert str(receipt["getter"]).startswith("iperf_get_")


def test_worker_tcp_network_controls_match_native_run(iperf3_server):
    """Bind controls and TCP settings survive a real native worker execution."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        local_port = reservation.getsockname()[1]
    expected = {
        "bind_address": "127.0.0.1",
        "bind_device": "lo",
        "client_port": local_port,
        "socket_buffer_bytes": 8192,
        "congestion_control": "reno",
        "no_delay": True,
        "mss": 1200,
        "connect_timeout_ms": 500,
        "pacing_timer_us": 1000,
        "burst_packets": 3,
        "rate": 1_000_000,
        "interval_seconds": 0.2,
        "get_server_output": True,
        "extra_data": "native-control-proof",
    }
    result = _run(iperf3_server, address_family="ipv4", fq_rate_bps=2_000_000, **expected)
    _receipts(result, expected)
    start = result.raw["start"]
    connected = start["connected"][0]
    assert connected["local_host"] == "127.0.0.1"
    assert connected["local_port"] == local_port
    assert ipaddress.ip_address(connected["remote_host"]).version == 4
    assert start["sock_bufsize"] == 8192
    assert start["sndbuf_actual"] >= 8192 and start["rcvbuf_actual"] >= 8192
    assert start["fq_rate"] == 2_000_000
    assert start["test_start"]["target_bitrate"] == 1_000_000
    assert start["test_start"]["interval"] == pytest.approx(0.2)
    assert len(result.raw["intervals"]) >= 3
    assert result.raw.get("server_output_text") or result.raw.get("server_output_json")
    assert loads_artifact(dumps_artifact(artifact_from_result(result))).result == result


@pytest.mark.parametrize(
    ("name", "native_name", "count"),
    [("bytes_to_send", "bytes", 32768), ("blocks_to_send", "blocks", 16)],
)
def test_worker_count_termination_is_native_and_bounded(iperf3_server, name, native_name, count):
    """Count mode disables the duration timer and returns measured traffic."""
    result = _run(iperf3_server, duration=None, blksize=1024, rate=1_000_000, **{name: count})
    _receipts(result, {name: count, "duration": 0})
    start = result.raw["start"]["test_start"]
    assert start[native_name] == count and start["duration"] == 0
    requested_bytes = count if name == "bytes_to_send" else count * 1024
    assert result.raw["end"]["sum_sent"]["bytes"] >= requested_bytes


@pytest.mark.parametrize("gsro", [False, True])
def test_worker_udp_controls_have_native_evidence(iperf3_server, gsro):
    """UDP counter/DF settings and version-gated offload configuration run locally."""
    version = ffi.string(lib.iperf_get_iperf_version()).decode()
    options: dict[str, Any] = {
        "protocol": "udp",
        "address_family": "ipv4",
        "blksize": 1200,
        "rate": 1_000_000,
        "udp_counters_64bit": True,
        "dont_fragment": True,
        "gsro": gsro,
    }
    if gsro and "3.19.1" in version:
        host, port = iperf3_server
        with pytest.raises(UnsupportedFeatureError, match="3.21"):
            Client(ClientConfig(server=host, port=port, duration=1, **options)).run(timeout=15)
        return
    result = _run(iperf3_server, **options)
    _receipts(result, {"udp_counters_64bit": True, "dont_fragment": True, "blksize": 1200})
    start = result.raw["start"]["test_start"]
    assert start["protocol"] == "UDP"
    assert start["target_bitrate"] == 1_000_000
    if gsro:
        assert start["gso"] == 1 and start["gro"] == 1
    assert result.raw["end"]["sum_received"]["packets"] > 0


def test_worker_sctp_stream_option_does_not_corrupt_rate(iperf3_server):
    """SCTP parser fallthrough is corrected without inventing stream-count evidence."""
    result = _run(iperf3_server, protocol="sctp", sctp_streams=3, rate=1_000_000, blksize=4096)
    assert result.raw["start"]["test_start"]["protocol"] == "SCTP"
    assert result.raw["start"]["test_start"]["target_bitrate"] == 1_000_000
    _receipts(result, {"rate": 1_000_000})
    assert result.execution is not None
    assert result.execution.configuration.effective["sctp_streams"].state == "unavailable"


def test_native_parser_process_exit_is_contained(iperf3_server, monkeypatch):
    """Fault injection reaches native exit(1), while the parent remains usable."""
    real_popen = subprocess.Popen
    children = []
    script = """
from iperf3_lib import native_options
original = native_options._arguments
def bad_arguments(role, options):
    return original(role, options) + ['--iperf3-lib-deliberately-unknown']
native_options._arguments = bad_arguments
from iperf3_lib._worker import main
main()
"""

    def altered_worker(args, **kwargs):
        child = real_popen([args[0], "-c", script], **kwargs)
        children.append(child)
        return child

    host, port = iperf3_server
    with monkeypatch.context() as context:
        context.setattr(_execution.subprocess, "Popen", altered_worker)
        with pytest.raises(IperfLibraryError, match="exit 1|code 1"):
            Client(ClientConfig(server=host, port=port, duration=1)).run(timeout=10)
    assert len(children) == 1 and children[0].poll() == 1
    _run(iperf3_server, rate=1_000_000)


def test_missing_payload_is_an_explicit_failure(iperf3_server, tmp_path):
    """A missing native payload file cannot silently become a successful result."""
    host, port = iperf3_server
    client = Client(
        ClientConfig(server=host, port=port, duration=1, payload_file=str(tmp_path / "missing.bin"))
    )
    try:
        result = client.run(timeout=10)
    except IperfError as exc:
        assert "file" in str(exc).lower()
    else:
        assert not result.ok and result.error
        assert "file" in result.error.lower()


@pytest.mark.parametrize("present", [False, True])
def test_missing_or_invalid_auth_key_rejects_without_prompt(tmp_path, present):
    """Key errors occur before connecting and cannot prompt or expose the password."""
    path = tmp_path / "public.pem"
    if present:
        path.write_text("not a valid PEM key", encoding="utf-8")
    client = Client(
        ClientConfig(
            server="127.0.0.1", duration=1, username="tester", rsa_public_key_path=str(path)
        ),
        password="not-in-errors",
    )
    with pytest.raises(ValueError, match="key") as captured:
        client.run(timeout=10)
    assert "not-in-errors" not in str(captured.value)


def test_client_streaming_retains_events_and_versioned_raw_provenance(iperf3_server):
    """Live callbacks keep a usable result and disclose old-native reconstruction."""
    host, port = iperf3_server
    events = []
    result = Client(
        ClientConfig(
            host,
            port=port,
            duration=1,
            rate=500_000,
            interval_seconds=0.2,
            title="stream-title",
            extra_data="stream-extra",
        )
    ).run(on_event=events.append, timeout=10)
    assert result.ok, result.error
    assert {event.kind for event in events} >= {"start", "interval", "end"}
    assert [event.sequence for event in events] == sorted({event.sequence for event in events})
    _receipts(result, {"json_stream": True, "extra_data": "stream-extra"})
    assert result.execution.configuration.effective["json_stream"].value is True
    version = ffi.string(lib.iperf_get_iperf_version()).decode()
    if "3.19.1" in version:
        retained = result.extensions["iperf3_lib.native_json"]
        assert retained["representation"] == "reconstructed_events"
        assert {item["event"] for item in retained["events"]} >= {"start", "interval", "end"}
    else:
        assert result.raw["title"] == "stream-title"
        assert result.raw["extra_data"] == "stream-extra"
        assert "iperf3_lib.native_json" not in result.extensions
    assert loads_artifact(dumps_artifact(artifact_from_result(result))).result == result


def test_copy_and_repeating_payload_controls_run_with_getter_evidence(iperf3_server):
    """TCP copy controls and native payload selection preserve actual traffic."""
    result = _run(
        iperf3_server,
        zerocopy=True,
        bidirectional=True,
        skip_rx_copy=True,
        repeating_payload=True,
        receive_timeout_ms=1000,
        send_timeout_ms=1000,
        control_keepalive=(9, 2, 3),
        rate=500_000,
    )
    _receipts(result, {"zerocopy": True, "repeating_payload": True})
    assert result.execution.configuration.effective["skip_rx_copy"].state == "unavailable"


def test_payload_file_is_used_in_native_count_mode(iperf3_server, tmp_path):
    """A readable payload file can drive a count-limited native transfer."""
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload-proof" * 4096)
    result = _run(
        iperf3_server,
        payload_file=str(payload),
        duration=None,
        bytes_to_send=32768,
        blksize=1024,
        rate=500_000,
    )
    assert result.raw["end"]["sum_sent"]["bytes"] >= 32768
