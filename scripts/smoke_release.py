"""Exercise an installed distribution with bounded loopback native benchmarks.

Invoke using the isolated installed interpreter: python -I smoke_release.py.
This script must not obtain iperf3_lib from a source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from iperf3_lib import ClientConfig, Result
    from iperf3_lib.intent import RateIntent
    from iperf3_lib.trials import TrialSpec


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


def verify_native_provenance(
    result: Result,
    config: ClientConfig,
    intent: RateIntent | None = None,
) -> None:
    """Verify recorded native provenance without relying on this machine's environment."""
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
    from iperf3_lib.intent import resolve_rate

    resolved = replace(config, rate=resolve_rate(config, intent).native_per_stream_bps)
    requested = config_dict(resolved)
    recorded = execution.configuration
    if recorded.requested != requested or set(recorded.effective) != set(requested):
        raise ValueError("native artifact lost the complete admitted configuration snapshot")
    for key in ("server", "port", "protocol", "duration", "omit", "parallel", "rate"):
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


def verify_native_artifact(
    result: Result, config: ClientConfig, intent: RateIntent | None = None
) -> dict:
    """Check native operation provenance and roundtrip the installed artifact codec."""
    verify_native_provenance(result, config, intent)
    return round_trip_artifact(result)


def config_dict(config: ClientConfig) -> dict:
    """Serialize the exact caller/native configuration without enum coercion drift."""
    data = asdict(config)
    data["server"] = str(config.server)
    data["protocol"] = config.protocol.value
    return data


def json_value(value):
    """Detach a strict JSON receipt, normalizing tuples and JSON object keys."""
    return json.loads(json.dumps(value, allow_nan=False))


def same_json(left, right) -> bool:
    """Compare receipts without treating true, 1, and 1.0 as identical evidence."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def native_version_matches(recorded: str | None, abi_version: str) -> bool:
    """Match native JSON's exact producer string to the ABI's numeric version."""
    return bool(re.fullmatch(r"\d+(?:\.\d+)+", abi_version)) and recorded == f"iperf {abi_version}"


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


def qualify_analysis(result):
    """Check analysis on the very same observed native result retained in the receipt."""
    from iperf3_lib.analysis import Selection, interval_stability, summary_throughput

    summaries, intervals = [], []
    for flow in result.flows:
        for observation in ("sender", "receiver"):
            stats = getattr(flow, observation)
            measured = summary_throughput(result, direction=flow.direction, observation=observation)
            if (
                stats is None
                or stats.bytes is None
                or stats.duration_seconds is None
                or stats.duration_seconds <= 0
                or measured.quality != "complete"
                or measured.bytes != stats.bytes
                or measured.measured_seconds != stats.duration_seconds
                or measured.throughput_bps != stats.bytes * 8 / stats.duration_seconds
            ):
                raise ValueError(
                    "installed summary analysis disagrees with native byte/time evidence"
                )
            summaries.append(asdict(measured))
            stability = interval_stability(result, selection=Selection(flow.direction, observation))
            # One-second profiles can contain only one interval, or no local
            # interval for the remote observer. Preserve this honest limitation.
            if stability.coverage.included_count < 2 and stability.quality != "insufficient_data":
                raise ValueError("installed interval analysis overstates sparse native evidence")
            intervals.append(asdict(stability))
    return json_value({"summary": summaries, "intervals": intervals})


def qualify_rate_intent(result, config, intent=None):
    """Match original intent, exact derived request, and actual native target."""
    from iperf3_lib.intent import resolve_rate

    resolution = resolve_rate(config, intent)
    extension = result.extensions.get("iperf3_lib.rate_intent")
    if (
        not isinstance(extension, dict)
        or type(extension.get("schema_version")) is not int
        or extension.get("schema_version") != 1
        or not same_json(extension.get("caller_config"), config_dict(config))
        or not same_json(extension.get("resolution"), resolution.to_dict())
        or not same_json(extension.get("intent"), asdict(intent) if intent is not None else None)
        or result.raw["start"]["test_start"]["target_bitrate"] != resolution.native_per_stream_bps
    ):
        raise ValueError("installed rate intent differs from native allocation")
    if result.execution is None or result.execution.configuration.requested != config_dict(
        replace(config, rate=resolution.native_per_stream_bps)
    ):
        raise ValueError("rate intent was not applied to the exact recorded native configuration")
    effective = result.execution.configuration.effective.get("rate")
    if (
        effective is None
        or effective.state != "verified"
        or effective.value != resolution.native_per_stream_bps
    ):
        raise ValueError("rate intent lacks a matching verified native target")
    return json_value(extension)


def qualify_transport_evidence(result: Result) -> dict:
    """Verify qualified local TCP units, endpoint CPU, and unsupported measurements."""
    interval_fields = {
        "smoothed_rtt_seconds": "rtt",
        "rtt_variation_seconds": "rttvar",
        "send_congestion_window_bytes": "snd_cwnd",
        "advertised_send_window_bytes": "snd_wnd",
        "path_mtu_bytes": "pmtu",
    }
    summary_fields = {
        "minimum_sampled_rtt_seconds": "min_rtt",
        "maximum_sampled_rtt_seconds": "max_rtt",
        "native_mean_sampled_rtt_seconds": "mean_rtt",
        "maximum_send_congestion_window_bytes": "max_snd_cwnd",
        "maximum_advertised_send_window_bytes": "max_snd_wnd",
    }

    def check(tcp, names):
        if not tcp.evidence_paths:
            raise ValueError("installed TCP fields have no native evidence")
        parent = next(iter(tcp.evidence_paths.values())).rsplit("/", 1)[0]
        native: Any = {"raw": result.raw}
        for part in parent.lstrip("/").split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            native = native[int(key)] if isinstance(native, list) else native[key]
        expected = {field: f"{parent}/{key}" for field, key in names.items() if key in native}
        if tcp.evidence_paths != expected or native.get("sender") is not True:
            raise ValueError("installed TCP evidence lost exact local-sender field provenance")
        for field, key in names.items():
            value = native.get(key)
            if value is not None and field.endswith("_seconds"):
                value /= 1_000_000
            if getattr(tcp, field) != value:
                raise ValueError("installed TCP field differs from native receipt or units")

    interval_count = 0
    for item in result.intervals:
        if item.tcp is not None:
            interval_count += 1
            if result.protocol != "tcp" or item.observation != "sender" or item.scope != "stream":
                raise ValueError(
                    "installed parser attributed TCP state to an unsupported observation"
                )
            check(item.tcp, interval_fields)
    expected_intervals = sum(
        1
        for interval in result.raw.get("intervals", [])
        for stream in interval.get("streams", [])
        if result.protocol == "tcp"
        and stream.get("sender") is True
        and any(name in stream for name in interval_fields.values())
    )
    summary_count = 0
    for stream in result.streams:
        if stream.receiver is not None and stream.receiver.tcp is not None:
            raise ValueError("installed parser attributed TCP state to remote sender or receiver")
        if stream.sender is not None and stream.sender.tcp is not None:
            summary_count += 1
            if result.protocol != "tcp":
                raise ValueError("installed parser attributed TCP state to another protocol")
            check(stream.sender.tcp, summary_fields)
    expected_summaries = sum(
        1
        for stream in result.raw.get("end", {}).get("streams", [])
        if result.protocol == "tcp"
        and stream.get("sender", {}).get("sender") is True
        and any(name in stream.get("sender", {}) for name in summary_fields.values())
    )
    if (interval_count, summary_count) != (expected_intervals, expected_summaries):
        raise ValueError("installed parser lost local TCP evidence")
    if [(item.endpoint, item.locality) for item in result.cpu] != [
        ("client", "local"),
        ("server", "remote"),
    ]:
        raise ValueError("installed CPU evidence lost client/server locality")
    for item, prefix in zip(result.cpu, ("host", "remote"), strict=True):
        for field in ("total", "user", "system"):
            key = f"{prefix}_{field}"
            if (
                getattr(item, f"{field}_percent")
                != result.raw["end"]["cpu_utilization_percent"][key]
                or item.evidence_paths.get(f"{field}_percent")
                != f"/raw/end/cpu_utilization_percent/{key}"
            ):
                raise ValueError("installed CPU evidence differs from returned native measurement")
    if result.protocol == "sctp":
        senders = [flow.sender for flow in result.flows if flow.sender is not None]
        senders += [stream.sender for stream in result.streams if stream.sender is not None]
        if any(stats.retransmits is not None for stats in senders):
            raise ValueError("installed SCTP output promoted unsupported retransmission data")
        if not any(
            path.endswith("/retransmits") and value.state == "unsupported"
            for path, value in result.availability.items()
        ):
            raise ValueError("installed SCTP result lost unsupported retransmission evidence")
    return json_value(
        {
            "tcp_interval_count": interval_count,
            "tcp_summary_count": summary_count,
            "cpu": [asdict(item) for item in result.cpu],
            "availability": {key: asdict(value) for key, value in result.availability.items()},
        }
    )


def validate_capabilities(data: dict, *, probe_native: bool, result: Result | None = None) -> None:
    """Check retained capability layers without probing this verifier's environment."""
    library = data["library"]
    if library["state"] != ("available" if probe_native else "unprobed"):
        raise ValueError("installed capabilities lost explicit probing semantics")
    if probe_native and (not isinstance(library["version"], str) or not library["version"].strip()):
        raise ValueError("native capability probe lost its version")
    if not probe_native and (library["version"] is not None or library["diagnostics"]):
        raise ValueError("offline capability report claims native library observation")
    expected_features = {
        "protocol_selection",
        "bidirectional",
        "rate",
        "json_output",
        "json_callback",
        "bind_address",
        "mptcp",
        "json_stream",
        "pacing_timer",
        "socket_buffer",
        "congestion_control",
        "server_output",
        "socket_pacing",
        "concurrent_execution",
        "hard_cancellation",
    }
    names = [feature["name"] for feature in data["features"]]
    if len(names) != len(set(names)) or not expected_features.issubset(names):
        raise ValueError("capability report lost expected feature identities")
    for feature in data["features"]:
        if feature["runtime"] != "not_run":
            raise ValueError("symbol lookup was promoted to execution success")
        for symbol in feature["symbols"]:
            if (not probe_native or not symbol["declared"]) and symbol["state"] != "unknown":
                raise ValueError("offline/undeclared symbol claims native observation")
    evidence = data["execution"]
    if result is None:
        if evidence != {
            "status": "not_provided",
            "native_version": None,
            "protocol": None,
            "verified_settings": [],
            "error": None,
        }:
            raise ValueError("unexecuted capability report fabricated run evidence")
    else:
        if result.execution is None:
            raise ValueError("supplied capability result lacks execution metadata")
        expected = {
            "status": result.execution.status,
            "native_version": result.execution.native_version,
            "protocol": result.protocol,
            "verified_settings": sorted(
                name
                for name, setting in result.execution.configuration.effective.items()
                if setting.state == "verified"
            ),
            "error": result.error,
        }
        if evidence != expected:
            raise ValueError("supplied execution evidence disappeared from capability report")


def qualify_capabilities(*, probe_native: bool, result: Result | None = None) -> dict:
    """Retain a capability report with library and execution evidence kept separate."""
    from iperf3_lib.capabilities import get_capabilities

    data = json_value(asdict(get_capabilities(probe_native=probe_native, result=result)))
    validate_capabilities(data, probe_native=probe_native, result=result)
    return data


def execute_admitted_trial(executable: str, spec: TrialSpec) -> Result:
    """Own a fresh server at the admitted endpoint and qualify the actual result."""
    from iperf3_lib import Client

    with native_server(executable, port=spec.config.port) as endpoint:
        if endpoint != (str(spec.config.server), spec.config.port):
            raise ValueError("native fixture changed the admitted endpoint")
        result = Client(spec.config, rate_intent=spec.rate_intent).run()
    qualify_rate_intent(result, spec.config, spec.rate_intent)
    verify_native_artifact(result, spec.config, spec.rate_intent)
    verify_native_case_result(result, spec.config, spec.rate_intent)
    qualify_analysis(result)
    qualify_transport_evidence(result)
    return result


def qualify_repeated_assessment(executable):
    """Exercise a warm-up and two real measurements from one unchanged admitted plan.

    Requires native_server(executable, *, port=None) to restart a one-off fixture
    at the same declared port for each invocation; this is a test fixture only.
    """
    from iperf3_lib import ClientConfig
    from iperf3_lib.analysis import ComparisonPolicy
    from iperf3_lib.assessments import AssessmentPolicy, assess_plan, ci_exit_code
    from iperf3_lib.intent import RateIntent
    from iperf3_lib.reports import (
        dumps_report,
        loads_report,
        render_junit,
        render_text,
        report_to_dict,
    )
    from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials, run_plan

    host = "127.0.0.1"
    with socket.socket() as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]
    config = ClientConfig(server=host, port=port, duration=1, parallel=2)
    intent = RateIntent(aggregate_bps_per_direction=1_000_001)
    plan = prepare_trials(
        config,
        rate_intent=intent,
        policy=TrialPolicy(warmup_runs=1, repetitions=2),
        budget=PlanBudget(max_active_seconds=3, max_payload_bytes=375_000),
    )

    execution = run_plan(plan, executor=lambda spec: execute_admitted_trial(executable, spec))
    if not execution.execution_success or len(execution.trials) != 3:
        raise ValueError("installed repeated native plan did not fully complete")
    baselines = tuple(
        record.artifact
        for record in execution.trials
        if record.spec.phase == "measured" and record.artifact is not None
    )
    report = assess_plan(
        execution,
        policy=AssessmentPolicy(minimum_valid_trials=2, minimum_valid_baselines=2),
        comparison=ComparisonPolicy(
            group_id="installed-native-repetition",
            endpoint_pair=("loopback-client", "loopback-server"),
        ),
        baselines=baselines,
    )
    assessment = report.assessment
    if (
        assessment.outcome != "pass"
        or not assessment.compatibility.compatible
        or len(assessment.measurements) != 2
        or len(assessment.baseline_measurements) != 2
        or len(assessment.exclusions) != 1
        or assessment.exclusions[0].reason != "warmup_run"
        or assessment.median_throughput_bps != assessment.baseline_median_bps
        or assessment.relative_change != 0
        or ci_exit_code(report) != 0
    ):
        raise ValueError("installed native assessment lost its recorded median/population contract")
    encoded = dumps_report(report)
    restored = loads_report(encoded)
    if report_to_dict(restored) != report_to_dict(report) or dumps_report(restored) != encoded:
        raise ValueError("installed assessment report changed retained evidence")
    text, xml = render_text(restored), render_junit(restored)
    if text != render_text(report) or xml != render_junit(report):
        raise ValueError("stored assessment changed human or CI rendering")
    root = ET.fromstring(xml)
    if int(root.get("failures", "-1")) != 0 or int(root.get("errors", "-1")) != 0:
        raise ValueError("installed successful native assessment emitted a failing JUnit suite")
    rates = [
        item.throughput.throughput_bps
        for item in assessment.measurements
        if item.throughput.throughput_bps is not None
    ]
    if (
        len(rates) != 2
        or assessment.median_throughput_bps is None
        or not math.isclose(
            assessment.median_throughput_bps, statistics.median(rates), rel_tol=1e-12
        )
    ):
        raise ValueError("installed assessment median disagrees with independent arithmetic")
    expected_ids = {
        f"trial:{record.spec.trial_id}"
        for record in execution.trials
        if record.spec.phase == "measured"
    }
    if {item.trial_id for item in assessment.measurements} != expected_ids:
        raise ValueError("assessment lost namespaced source references")
    if assessment.effective_minimum_bps != assessment.baseline_median_bps:
        raise ValueError("native baseline assessment lost its inclusive threshold")
    return {"report": json.loads(encoded), "text": text, "junit": xml}


def qualify_sweep(executable: str) -> dict:
    """Run two declared stream counts under one finite aggregate-rate budget."""
    from iperf3_lib import ClientConfig
    from iperf3_lib.analysis import ComparisonPolicy
    from iperf3_lib.intent import RateIntent
    from iperf3_lib.sweep_reports import (
        dumps_sweep_report,
        loads_sweep_report,
        report_from_sweep,
        sweep_report_to_dict,
    )
    from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep
    from iperf3_lib.trials import PlanBudget, TrialPolicy

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    prepared = prepare_sweep(
        ClientConfig("127.0.0.1", port=port, duration=1),
        (SweepAxis("parallel", (1, 2)),),
        rate_intent=RateIntent(aggregate_bps_per_direction=1_000_001),
        policy=TrialPolicy(repetitions=1),
        budget=PlanBudget(max_active_seconds=2, max_payload_bytes=250_001),
    )
    sweep = run_sweep(
        prepared,
        executor=lambda spec: execute_admitted_trial(executable, spec),
        comparison_policy=ComparisonPolicy(
            "installed-native-stream-sweep",
            ("loopback-client", "loopback-server"),
            varying_fields=("parallel",),
        ),
    )
    report = report_from_sweep(sweep)
    encoded = dumps_sweep_report(report)
    restored = loads_sweep_report(encoded)
    if not same_json(sweep_report_to_dict(restored), sweep_report_to_dict(report)) or (
        dumps_sweep_report(restored) != encoded
    ):
        raise ValueError("installed sweep report changed retained evidence")
    return {"report": json.loads(encoded)}


def verify_native_case_result(
    result: Result,
    config: ClientConfig,
    intent: RateIntent | None = None,
    *,
    name: str = "installed",
) -> None:
    """Verify actual native configuration and flow observations for one profile."""
    from iperf3_lib.intent import resolve_rate

    protocol, parallel = config.protocol, config.parallel
    reverse, bidirectional = config.reverse, config.bidirectional
    if not result.ok:
        raise ValueError(f"{name} native benchmark failed: {result.error}")
    if result.reporting_role != "client":
        raise ValueError(f"{name} reporting role must identify the client producing native JSON")
    native = result.raw["start"]["test_start"]
    expected = {
        "protocol": protocol.value.upper(),
        "duration": 1,
        "target_bitrate": resolve_rate(config, intent).native_per_stream_bps,
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
        suffix = "_bidir_reverse" if bidirectional and flow.direction == "server_to_client" else ""
        for observer, native_key in (("sender", "sum_sent"), ("receiver", "sum_received")):
            stats = getattr(flow, observer)
            measured = result.raw["end"][native_key + suffix]["bits_per_second"]
            if (
                stats is None
                or stats.bits_per_second != measured
                or measured <= 0
                or stats.bytes != result.raw["end"][native_key + suffix]["bytes"]
                or stats.duration_seconds != result.raw["end"][native_key + suffix]["seconds"]
            ):
                raise ValueError(f"{name} {flow.direction}/{observer} does not match native output")
            if stats.direction != flow.direction or stats.observation != observer:
                raise ValueError(f"{name} nested observation has inconsistent provenance")
    for native_interval in result.raw.get("intervals", []):
        for stream in native_interval.get("streams", []):
            if "sender" not in stream or "socket" not in stream:
                continue
            matches = [
                item
                for item in result.intervals
                if item.stream_id == stream["socket"] and item.start_seconds == stream.get("start")
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


def native_cases(executable: str) -> list[dict]:
    """Verify native configuration and measurements for representative methods."""
    from iperf3_lib import Client, ClientConfig, Protocol
    from iperf3_lib.intent import RateIntent

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
                rate=None if name == "tcp-forward" else 1_000_000,
                protocol=protocol,
                parallel=parallel,
                reverse=reverse,
                bidirectional=bidirectional,
            )
            intent = (
                RateIntent(aggregate_bps_per_direction=1_000_001) if name == "tcp-forward" else None
            )
            result = Client(config, rate_intent=intent).run()
        verify_native_case_result(result, config, intent, name=name)
        receipts.append(
            {
                "profile": name,
                "result": result.to_dict(),
                "artifact": verify_native_artifact(result, config, intent),
                "rate_intent": qualify_rate_intent(result, config, intent),
                "analysis": qualify_analysis(result),
                "transport": qualify_transport_evidence(result),
                "capabilities": qualify_capabilities(probe_native=True, result=result),
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
def native_server(executable: str, *, port: int | None = None) -> Iterator[tuple[str, int]]:
    """Own one fresh server per profile and wait without opening a probe connection."""
    host = "127.0.0.1"
    if port is None:
        with socket.socket() as reservation:
            reservation.bind((host, 0))
            port = reservation.getsockname()[1]
    elif type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("native fixture port must be an integer between 1 and 65535")
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


def validate_repeated_assessment(data: dict, *, package_version: str, native_version: str):
    """Validate retained installed trial evidence offline against the declared smoke plan."""
    from iperf3_lib.assessments import ci_exit_code
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.reports import render_junit, render_text, report_from_dict
    from iperf3_lib.trials import PlanBudget, TrialPolicy

    report = report_from_dict(data["report"])
    execution, assessment = report.execution, report.assessment
    if (
        not execution.execution_success
        or len(execution.trials) != 3
        or execution.plan.policy != TrialPolicy(repetitions=2, warmup_runs=1)
        or execution.plan.budget != PlanBudget(3, 375_000)
        or execution.plan.order_seed is not None
        or assessment.outcome != "pass"
        or not assessment.compatibility.compatible
        or report.policy.minimum_valid_trials != 2
        or report.policy.minimum_valid_baselines != 2
        or len(assessment.measurements) != 2
        or len(assessment.baseline_measurements) != 2
        or len(assessment.exclusions) != 1
        or assessment.exclusions[0].reason != "warmup_run"
        or assessment.median_throughput_bps != assessment.baseline_median_bps
        or assessment.effective_minimum_bps != assessment.baseline_median_bps
        or assessment.relative_change != 0
        or ci_exit_code(report) != 0
    ):
        raise ValueError("retained installed assessment lost its median/population contract")
    endpoints = set()
    for record in execution.trials:
        config, intent = record.spec.config, record.spec.rate_intent
        if (
            record.artifact is None
            or config != ClientConfig("127.0.0.1", port=config.port, duration=1, parallel=2)
            or record.spec.cell_id != "default"
            or intent is None
            or intent.aggregate_bps_per_direction != 1_000_001
        ):
            raise ValueError("retained installed trial differs from the bounded aggregate plan")
        endpoints.add((str(config.server), config.port))
        result = record.artifact.result
        if (
            record.artifact.producer.name != "iperf3-lib"
            or record.artifact.producer.version != package_version
        ):
            raise ValueError("retained trial producer differs from the installed package")
        if result.execution is None or not native_version_matches(
            result.execution.native_version, native_version
        ):
            raise ValueError("retained trial native version differs from the receipt")
        verify_native_provenance(result, config, intent)
        verify_native_case_result(result, config, intent)
        qualify_rate_intent(result, config, intent)
        qualify_analysis(result)
    if len(endpoints) != 1:
        raise ValueError("retained repeated plan changed its admitted endpoint")
    measured = [record.artifact for record in execution.trials if record.spec.phase == "measured"]
    if list(report.baselines or ()) != measured:
        raise ValueError("retained smoke baselines are not the same declared measured population")
    expected_ids = {
        f"trial:{record.spec.trial_id}"
        for record in execution.trials
        if record.spec.phase == "measured"
    }
    if {item.trial_id for item in assessment.measurements} != expected_ids:
        raise ValueError("retained assessment lost namespaced source references")
    rates = [
        item.throughput.throughput_bps
        for item in assessment.measurements
        if item.throughput.throughput_bps is not None
    ]
    if (
        len(rates) != 2
        or assessment.median_throughput_bps is None
        or not math.isclose(
            assessment.median_throughput_bps, statistics.median(rates), rel_tol=1e-12
        )
    ):
        raise ValueError("retained assessment median differs from measured arithmetic")
    if data["text"] != render_text(report) or data["junit"] != render_junit(report):
        raise ValueError("retained assessment rendering differs from its report")
    root = ET.fromstring(data["junit"])
    if root.get("tests") != "4" or root.get("failures") != "0" or root.get("errors") != "0":
        raise ValueError("retained successful assessment has an invalid JUnit outcome")
    return report


def validate_sweep(data: dict, *, package_version: str, native_version: str):
    """Check the retained two-cell native qualification using the strict v1 codec."""
    from iperf3_lib.analysis import ComparisonPolicy
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.intent import RateIntent
    from iperf3_lib.sweep_reports import sweep_report_from_dict
    from iperf3_lib.sweeps import SweepAxis
    from iperf3_lib.trials import PlanBudget, TrialPolicy

    report = sweep_report_from_dict(data["report"])
    sweep = report.result
    prepared, execution = sweep.prepared, sweep.execution
    config = prepared.base_config
    intent = RateIntent(aggregate_bps_per_direction=1_000_001)
    cell_ids = ("cell-0000", "cell-0001")
    if (
        report.producer.name != "iperf3-lib"
        or report.producer.version != package_version
        or config != ClientConfig("127.0.0.1", port=config.port, duration=1)
        or prepared.axes != (SweepAxis("parallel", (1, 2)),)
        or prepared.order != "declared"
        or prepared.seed is not None
        or prepared.rate_intent != intent
        or execution.plan.policy != TrialPolicy(repetitions=1)
        or execution.plan.budget != PlanBudget(2, 250_001)
        or execution.plan.order_seed is not None
        or not execution.execution_success
        or len(execution.trials) != 2
        or sweep.minimum_valid_trials != 1
        or tuple(cell.cell_id for cell in prepared.cells) != cell_ids
        or tuple(cell.cell_id for cell in sweep.cells) != cell_ids
        or sweep.comparison_policy
        != ComparisonPolicy(
            "installed-native-stream-sweep",
            ("loopback-client", "loopback-server"),
            varying_fields=("parallel",),
        )
    ):
        raise ValueError("retained sweep differs from the bounded declared stream plan")
    for parallel, (cell, summary, record) in enumerate(
        zip(prepared.cells, sweep.cells, execution.trials, strict=True), 1
    ):
        wanted = replace(config, parallel=parallel)
        resolved = replace(wanted, rate=1_000_001 // parallel)
        if (
            cell.config != wanted
            or cell.resolved_config != resolved
            or cell.parameters != {"parallel": parallel}
            or record.spec.config != wanted
            or record.spec.rate_intent != intent
            or record.spec.cell_id != cell.cell_id
            or record.spec.phase != "measured"
            or record.spec.repetition != 0
            or cell.trial_ids != (record.spec.trial_id,)
            or record.artifact is None
        ):
            raise ValueError("retained sweep lost the exact admitted per-cell allocation")
        artifact = record.artifact
        result = artifact.result
        if (
            artifact.producer.name != "iperf3-lib"
            or artifact.producer.version != package_version
            or result.execution is None
            or not native_version_matches(result.execution.native_version, native_version)
        ):
            raise ValueError("retained sweep artifact differs from package/native identity")
        verify_native_provenance(result, wanted, intent)
        verify_native_case_result(result, wanted, intent)
        qualify_rate_intent(result, wanted, intent)
        qualify_analysis(result)
        qualify_transport_evidence(result)
        if (
            summary.method != "forward"
            or len(summary.directions) != 1
            or summary.execution_counts
            != {
                "completed": 1,
                "failed": 0,
                "incomplete": 0,
                "exception": 0,
                "not_run": 0,
            }
        ):
            raise ValueError("retained sweep lost its complete forward population")
        direction = summary.directions[0]
        if (
            direction.direction != "client_to_server"
            or direction.observation != "receiver"
            or direction.quality != "complete"
            or len(direction.samples) != 1
            or direction.exclusions
        ):
            raise ValueError("retained sweep lacks a qualified receiver sample")
        sample = direction.samples[0]
        native = result.raw["end"]["sum_received"]
        if (
            sample.trial_id != record.spec.trial_id
            or sample.bytes != native["bytes"]
            or sample.measured_seconds != native["seconds"]
            or sample.throughput_bps != 8 * native["bytes"] / native["seconds"]
            or direction.median_throughput_bps != sample.throughput_bps
            or not sample.eligible_for_cell
            or {check.name for check in sample.setting_checks} != {"parallel", "rate"}
            or any(check.state != "matched" for check in sample.setting_checks)
        ):
            raise ValueError("retained sweep summary differs from receiver bytes/time or settings")
    if len(sweep.comparisons) != 1:
        raise ValueError("retained sweep lost its explicit comparison group")
    comparison = sweep.comparisons[0]
    if (
        comparison.method != "forward"
        or comparison.direction != "client_to_server"
        or comparison.cell_ids != cell_ids
        or comparison.quality != "complete"
        or comparison.reasons
        or comparison.compatibility.quality != "complete"
        or not comparison.compatibility.compatible
    ):
        raise ValueError("retained sweep failed its aggregate-rate compatibility check")
    return report


def validate_smoke_receipt(receipt: dict) -> None:
    """Verify receipt-v2 contents offline; the caller independently binds its manifest identity.

    No check depends on this verifier's installed version, platform, Python, or
    native library. Receipt identities are structurally checked here; the sealed
    release manifest remains the authority for expected source and artifact hash.
    Historical receipt v1 remains retained evidence, not this expanded contract.
    """
    from iperf3_lib.artifacts import artifact_from_dict
    from iperf3_lib.config import ClientConfig, Protocol
    from iperf3_lib.intent import RateIntent

    if (
        type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 2
        or receipt.get("ok") is not True
    ):
        raise ValueError("expanded installed qualification requires a successful receipt v2")
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_revision", "")):
        raise ValueError("installed receipt lacks a full source revision")
    identity = receipt["artifact"]
    if not re.fullmatch(r"[0-9a-f]{64}", identity.get("sha256", "")) or not identity.get(
        "filename", ""
    ).endswith((".whl", ".tar.gz")):
        raise ValueError("installed receipt lacks a distribution hash/name")
    for field in ("package_version", "native_version", "python", "installed_location"):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            raise ValueError(f"installed receipt lacks {field}")
    native_version, package_version = receipt["native_version"], receipt["package_version"]
    capabilities = receipt["capabilities"]
    validate_capabilities(capabilities["offline"], probe_native=False)
    validate_capabilities(capabilities["probed"], probe_native=True)
    if capabilities["probed"]["library"]["version"] != native_version:
        raise ValueError("retained probe version differs from native receipt identity")
    profiles = {
        "tcp-forward": ("tcp", 2, False, False),
        "tcp-reverse": ("tcp", 1, True, False),
        "tcp-bidirectional": ("tcp", 1, False, True),
        "udp": ("udp", 1, False, False),
        "sctp": ("sctp", 1, False, False),
    }
    if [case["profile"] for case in receipt["cases"]] != list(profiles):
        raise ValueError("installed receipt must retain every native profile in declared order")
    for case in receipt["cases"]:
        artifact = artifact_from_dict(case["artifact"])
        result = artifact.result
        if artifact.producer.name != "iperf3-lib" or artifact.producer.version != package_version:
            raise ValueError("retained profile producer differs from package identity")
        if (
            json_value(result.to_dict()) != case["result"]
            or result.execution is None
            or not native_version_matches(result.execution.native_version, native_version)
        ):
            raise ValueError("retained profile result or native version differs from artifact")
        extension: Any = result.extensions["iperf3_lib.rate_intent"]
        config = ClientConfig(**extension["caller_config"])
        intent = RateIntent(**extension["intent"]) if extension["intent"] is not None else None
        protocol, parallel, reverse, bidirectional = profiles[case["profile"]]
        expected_config = ClientConfig(
            "127.0.0.1",
            port=config.port,
            protocol=Protocol(protocol),
            duration=1,
            parallel=parallel,
            reverse=reverse,
            bidirectional=bidirectional,
            rate=None if case["profile"] == "tcp-forward" else 1_000_000,
        )
        if config != expected_config:
            raise ValueError("retained profile differs from its declared bounded configuration")
        if case["profile"] == "tcp-forward":
            if (
                config.rate is not None
                or intent is None
                or intent.aggregate_bps_per_direction != 1_000_001
            ):
                raise ValueError("installed aggregate profile lost its explicit remainder case")
        elif config.rate != 1_000_000 or intent is not None:
            raise ValueError("installed legacy rate profile changed its declared target")
        verify_native_provenance(result, config, intent)
        verify_native_case_result(result, config, intent, name=case["profile"])
        if not same_json(case["rate_intent"], qualify_rate_intent(result, config, intent)):
            raise ValueError("retained rate-intent report differs from native evidence")
        if case["analysis"] != qualify_analysis(result) or case[
            "transport"
        ] != qualify_transport_evidence(result):
            raise ValueError("retained analysis/transport reports differ from native evidence")
        validate_capabilities(case["capabilities"], probe_native=True, result=result)
        if case["capabilities"]["library"]["version"] != native_version:
            raise ValueError("retained executed capability probe changed native version")
    saved = receipt["saved_artifacts"]
    success = artifact_from_dict(saved["saved_native"])
    failure = artifact_from_dict(saved["failed_native"])
    for artifact in (success, failure):
        if artifact.producer.name != "iperf3-lib" or artifact.producer.version != package_version:
            raise ValueError("retained saved artifact producer differs from package identity")
        if (
            artifact.result.execution is None
            or artifact.result.completed_at_seconds is not None
            or artifact.result.execution.configuration.requested is not None
        ):
            raise ValueError("saved result invented operation completion or requested settings")
    if (
        success.result.execution is None
        or failure.result.execution is None
        or len(success.result.flows) != 1
        or success.result.flows[0].sender is None
        or success.result.flows[0].receiver is None
        or success.result.execution.timing.estimated_completed_at_seconds != 105
        or success.result.flows[0].sender.bits_per_second is not None
        or success.result.flows[0].receiver.bits_per_second != 0
        or failure.result.ok
        or failure.result.error != "saved native failure"
        or failure.result.execution.status != "failed"
    ):
        raise ValueError("retained saved failure/missing-zero semantics changed")
    validate_repeated_assessment(
        receipt["repeated_assessment"],
        package_version=package_version,
        native_version=native_version,
    )
    validate_sweep(receipt["sweep"], package_version=package_version, native_version=native_version)


def qualify(expected_version: str, expected_native_version: str) -> dict:
    """Run native clients against temporary, bounded loopback servers."""

    def forbidden_load(*args, **kwargs):
        raise ValueError("offline installed qualification attempted native loading")

    with patch("cffi.FFI.dlopen", side_effect=forbidden_load) as loader:
        location = require_installed_package(expected_version)
        offline = qualify_capabilities(probe_native=False)
        saved_artifacts = verify_saved_result_semantics()
        if loader.call_count:
            raise ValueError("offline capability/codec checks attempted native loading")
    probed = qualify_capabilities(probe_native=True)
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
        "schema_version": 2,
        "ok": True,
        "package_version": expected_version,
        "native_version": native_version,
        "python": sys.version,
        "installed_location": location,
        "cases": receipts,
        "saved_artifacts": saved_artifacts,
        "capabilities": {"offline": offline, "probed": probed},
        "repeated_assessment": qualify_repeated_assessment(executable),
        "sweep": qualify_sweep(executable),
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
        receipt = {"schema_version": 2, "ok": False, "error": f"{type(error).__name__}: {error}"}
        status = 1
    with args.distribution.open("rb") as source:
        artifact_hash = hashlib.file_digest(source, "sha256").hexdigest()
    receipt.update(
        {
            "source_revision": args.source_revision,
            "artifact": {"filename": args.distribution.name, "sha256": artifact_hash},
        }
    )
    if status == 0:
        try:
            validate_smoke_receipt(receipt)
        except Exception as error:
            receipt["ok"] = False
            receipt["error"] = f"{type(error).__name__}: {error}"
            status = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"installed native smoke: {'passed' if status == 0 else 'failed'} ({args.output})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
