"""Exercise an installed distribution with bounded loopback native benchmarks.

Invoke using the isolated installed interpreter: python -I smoke_release.py.
This script must not obtain iperf3_lib from a source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iperf3_lib import ClientConfig, Result


def round_trip_artifact(result: Result) -> dict:
    """Qualify the installed v1 JSON codec and retain its complete decoded envelope."""
    from iperf3_lib.artifacts import (
        artifact_from_result,
        artifact_to_dict,
        dumps_artifact,
        loads_artifact,
    )

    original = artifact_from_result(result)
    expected = artifact_to_dict(original)
    encoded = dumps_artifact(original)
    restored = loads_artifact(encoded)
    actual = artifact_to_dict(restored)
    if actual != expected or dumps_artifact(restored) != encoded:
        raise ValueError("installed artifact codec changed recorded evidence during round trip")
    if actual["schema_version"] != 1 or actual["kind"] != "iperf3-lib.result":
        raise ValueError("installed artifact codec did not produce the v1 result envelope")
    if actual["producer"] != {
        "name": "iperf3-lib",
        "version": importlib.metadata.version("iperf3-lib"),
    }:
        raise ValueError("installed artifact producer does not match the installed distribution")
    return actual


def verify_native_artifact(result: Result, config: ClientConfig) -> dict:
    """Check observed timing and requested/effective provenance before serializing a run."""
    execution = result.execution
    method = "bidirectional" if config.bidirectional else "reverse" if config.reverse else "forward"
    if execution is None or execution.status != "completed" or execution.method != method:
        raise ValueError("native artifact execution status or method differs from the run")
    timing = execution.timing
    if (
        timing.started_at_seconds is None
        or timing.completed_at_seconds is None
        or timing.elapsed_seconds is None
        or timing.elapsed_seconds <= 0
        or timing.completed_at_seconds != result.completed_at_seconds
        or timing.requested_duration_seconds != config.duration
    ):
        raise ValueError("native artifact is missing consistent observed operation timing")
    native_start = result.raw["start"]
    native_timestamp = native_start["timestamp"]["timesecs"]
    if (
        timing.native_started_at_seconds != native_timestamp
        or timing.estimated_completed_at_seconds
        != native_timestamp + native_start["test_start"]["duration"]
    ):
        raise ValueError("native artifact does not preserve native timing separately")
    requested = asdict(config)
    requested["server"] = str(config.server)
    requested["protocol"] = config.protocol.value
    recorded = execution.configuration
    if recorded.requested != requested or set(recorded.effective) != set(requested):
        raise ValueError("native artifact lost the complete admitted configuration snapshot")
    for key in ("protocol", "duration", "parallel", "rate"):
        setting = recorded.effective[key]
        if (
            setting.state != "verified"
            or setting.value != requested[key]
            or not setting.evidence_paths
        ):
            raise ValueError(f"native artifact effective {key} lacks matching native evidence")
    for key in ("mptcp", "json_stream"):
        setting = recorded.effective[key]
        if setting.state != "unavailable" or setting.value is not None or setting.evidence_paths:
            raise ValueError(f"native artifact claims unobserved {key} is verified")
    if (
        execution.native_version != native_start["version"]
        or not execution.python_version
        or not execution.platform
    ):
        raise ValueError("native artifact is missing native or wrapper environment metadata")
    return round_trip_artifact(result)


def require_installed_package(expected_version: str) -> str:
    """Verify package identity and reject source-tree or editable imports."""
    import iperf3_lib

    location = Path(iperf3_lib.__file__).resolve()
    if not sys.flags.isolated or not location.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError(
            "smoke must use an isolated interpreter and a package inside its environment"
        )
    distribution = importlib.metadata.distribution("iperf3-lib")
    if distribution.version != expected_version:
        raise ValueError("installed package version does not match the retained artifact")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
        raise ValueError("editable package installation cannot qualify a distribution")
    requirements = distribution.requires or []
    if [re.split(r"[\s(<>=!~;\[]", item, maxsplit=1)[0].lower() for item in requirements] != [
        "cffi"
    ]:
        raise ValueError("installed package runtime dependencies must contain only CFFI")
    return str(location)


def native_cases(executable: str) -> list[dict]:
    """Verify native configuration and measurements for representative methods."""
    from iperf3_lib import Client, ClientConfig, Protocol

    cases = (
        ("tcp-forward", Protocol.TCP, 2, False, False),
        ("tcp-reverse", Protocol.TCP, 1, True, False),
        ("tcp-bidirectional", Protocol.TCP, 1, False, True),
        ("udp", Protocol.UDP, 1, False, False),
        ("sctp", Protocol.SCTP, 1, False, False),
    )
    receipts: list[dict] = []
    for name, protocol, parallel, reverse, bidirectional in cases:
        with native_server(executable) as (host, port):
            config = ClientConfig(
                server=host,
                port=port,
                duration=1,
                rate=1_000_000,
                protocol=protocol,
                parallel=parallel,
                reverse=reverse,
                bidirectional=bidirectional,
            )
            result = Client(config).run()
        if not result.ok:
            raise ValueError(f"{name} native benchmark failed: {result.error}")
        if result.reporting_role != "client":
            raise ValueError(
                f"{name} reporting role must identify the client producing native JSON"
            )
        native = result.raw["start"]["test_start"]
        expected = {
            "protocol": protocol.value.upper(),
            "duration": 1,
            "target_bitrate": 1_000_000,
            "num_streams": parallel,
        }
        if reverse:
            expected["reverse"] = 1
        if bidirectional:
            expected["bidir"] = 1
        for key, value in expected.items():
            if native.get(key) != value:
                raise ValueError(f"{name} native {key} differs from requested {value}")
        expected_directions = (
            {"client_to_server", "server_to_client"}
            if bidirectional
            else {"server_to_client"}
            if reverse
            else {"client_to_server"}
        )
        if {flow.direction for flow in result.flows} != expected_directions:
            raise ValueError(f"{name} normalized flow directions differ from native configuration")
        for flow in result.flows:
            suffix = (
                "_bidir_reverse" if bidirectional and flow.direction == "server_to_client" else ""
            )
            for observer, native_key in (("sender", "sum_sent"), ("receiver", "sum_received")):
                stats = getattr(flow, observer)
                measured = result.raw["end"][native_key + suffix]["bits_per_second"]
                if stats is None or stats.bits_per_second != measured or measured <= 0:
                    raise ValueError(
                        f"{name} {flow.direction}/{observer} does not match native output"
                    )
                if stats.direction != flow.direction or stats.observation != observer:
                    raise ValueError(f"{name} nested observation has inconsistent provenance")
        for native_interval in result.raw.get("intervals", []):
            for stream in native_interval.get("streams", []):
                if "sender" not in stream or "socket" not in stream:
                    continue
                matches = [
                    item
                    for item in result.intervals
                    if item.stream_id == stream["socket"]
                    and item.start_seconds == stream.get("start")
                ]
                direction = (
                    ("client_to_server" if stream["sender"] else "server_to_client")
                    if bidirectional
                    else next(iter(expected_directions))
                )
                observer = "sender" if stream["sender"] else "receiver"
                if not matches or any(
                    item.direction != direction or item.observation != observer for item in matches
                ):
                    raise ValueError(f"{name} per-stream interval disagrees with native provenance")
        receipts.append(
            {
                "profile": name,
                "result": result.to_dict(),
                "artifact": verify_native_artifact(result, config),
            }
        )
    return receipts


def verify_saved_result_semantics() -> dict:
    """Round-trip saved native evidence without inventing completion or freshness."""
    from iperf3_lib.artifacts import artifact_from_dict
    from iperf3_lib.exporters.prometheus import render_text
    from iperf3_lib.result import result_from_iperf_json

    result = result_from_iperf_json(
        {
            "start": {
                "timestamp": {"timesecs": 100},
                "test_start": {"protocol": "TCP", "reverse": 0, "bidir": 0, "duration": 5},
            },
            "end": {"sum_sent": {"retransmits": 2}, "sum_received": {"bits_per_second": 0}},
        },
        reporting_role="client",
    )
    saved_artifact = round_trip_artifact(result)
    result = artifact_from_dict(saved_artifact).result
    execution = result.execution
    if (
        execution is None
        or execution.timing.estimated_completed_at_seconds != 105
        or execution.timing.native_started_at_seconds != 100
        or execution.timing.started_at_seconds is not None
        or execution.timing.completed_at_seconds is not None
        or execution.timing.elapsed_seconds is not None
        or result.completed_at_seconds is not None
        or execution.configuration.requested is not None
        or "timestamp_seconds" in render_text(result)
    ):
        raise ValueError(
            "saved native artifact invented observed timing, requested intent, or freshness"
        )
    flow = result.flows[0]
    if flow.sender is None or flow.sender.bits_per_second is not None:
        raise ValueError("installed parser manufactured an absent throughput measurement")
    if flow.receiver is None or flow.receiver.bits_per_second != 0:
        raise ValueError("installed parser lost a measured zero")
    samples = [
        line
        for line in render_text(result).splitlines()
        if line.startswith("iperf3_last_run_throughput_bytes_per_second{")
    ]
    if (
        len(samples) != 1
        or 'observer="receiver"' not in samples[0]
        or not samples[0].endswith(" 0")
    ):
        raise ValueError("installed exporter conflates absent and zero measurements")
    failure = result_from_iperf_json({"error": "saved native failure"}, reporting_role="client")
    failed_artifact = round_trip_artifact(failure)
    failure = artifact_from_dict(failed_artifact).result
    failure_text = render_text(failure, last_success_timestamp_seconds=90)
    if (
        failure.ok
        or failure.error != "saved native failure"
        or failure.execution is None
        or failure.execution.status != "failed"
        or failure.completed_at_seconds is not None
        or "throughput" in failure_text
        or "iperf3_last_success_timestamp_seconds 90" not in failure_text
    ):
        raise ValueError(
            "installed parser/exporter treats a native error as a successful measurement"
        )
    return {"saved_native": saved_artifact, "failed_native": failed_artifact}


@contextmanager
def native_server(executable: str) -> Iterator[tuple[str, int]]:
    """Own one fresh server per profile and wait without opening a probe connection."""
    host = "127.0.0.1"
    with socket.socket() as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory() as temporary:
        log = Path(temporary) / "server.log"
        with log.open("w", encoding="utf-8") as output:
            server = subprocess.Popen(
                [executable, "-s", "--one-off", "-B", host, "-p", str(port), "--forceflush"],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if server.poll() is not None:
                        raise ValueError("native smoke server exited before readiness")
                    if f"Server listening on {port}" in log.read_text(encoding="utf-8"):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("native smoke server did not become ready")
                    time.sleep(0.05)
                yield host, port
                if server.wait(timeout=5) != 0:
                    raise ValueError("native smoke server exited unsuccessfully")
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)


def qualify(expected_version: str, expected_native_version: str) -> dict:
    """Run native clients against temporary, bounded loopback servers."""
    location = require_installed_package(expected_version)
    saved_artifacts = verify_saved_result_semantics()
    from iperf3_lib.ffi.api import ffi, lib

    native_version = ffi.string(lib.iperf_get_iperf_version()).decode()
    if not re.search(rf"(?<![\d.]){re.escape(expected_native_version)}(?![\d.])", native_version):
        raise ValueError(
            f"loaded native version is {native_version!r}, expected {expected_native_version}"
        )
    executable = shutil.which("iperf3")
    if executable is None:
        raise ValueError("native smoke requires the matching iperf3 server executable")
    receipts = native_cases(executable)
    return {
        "schema_version": 1,
        "ok": True,
        "package_version": expected_version,
        "native_version": native_version,
        "python": sys.version,
        "installed_location": location,
        "cases": receipts,
        "saved_artifacts": saved_artifacts,
    }


def main() -> int:
    """Write a machine-readable installed-distribution qualification receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-native-version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--distribution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    status = 0
    receipt: dict[str, object]
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_revision):
            raise ValueError("qualification requires a full source commit SHA")
        receipt = qualify(args.expected_version, args.expected_native_version)
    except Exception as error:
        receipt = {"schema_version": 1, "ok": False, "error": f"{type(error).__name__}: {error}"}
        status = 1
    with args.distribution.open("rb") as source:
        artifact_hash = hashlib.file_digest(source, "sha256").hexdigest()
    receipt.update(
        {
            "source_revision": args.source_revision,
            "artifact": {"filename": args.distribution.name, "sha256": artifact_hash},
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"installed native smoke: {'passed' if status == 0 else 'failed'} ({args.output})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
