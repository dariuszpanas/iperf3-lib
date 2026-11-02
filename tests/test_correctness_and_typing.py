"""Regression tests for address_family verification, consumer typing, and validation symmetries."""

from __future__ import annotations

import copy
import importlib
import json
import pkgutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Union

import pytest

import iperf3_lib
from iperf3_lib.adaptive import (
    AdaptiveUDPPolicy,
    prepare_adaptive_udp,
)
from iperf3_lib.adaptive_execution import run_adaptive_udp
from iperf3_lib.analysis import AnalysisTrial, ComparisonPolicy, check_compatibility
from iperf3_lib.artifacts import (
    ArtifactValidationError,
    artifact_from_result,
    artifact_to_dict,
    dumps_artifact,
    loads_artifact,
)
from iperf3_lib.assessments import AssessmentPolicy, assess_plan
from iperf3_lib.concurrent_execution import (
    ConcurrentExecutionPolicy,
    ConcurrentPlanResult,
    ConcurrentTrialRecord,
    prepare_concurrent_plan,
)
from iperf3_lib.concurrent_reports import (
    concurrent_report_from_execution,
    render_concurrent_junit,
)
from iperf3_lib.config import ClientConfig
from iperf3_lib.exporters.prometheus import render_text as render_prometheus_text
from iperf3_lib.iperf_client import Client, _observed_address_family
from iperf3_lib.reports import _convert
from iperf3_lib.result import (
    EndStats,
    ExecutionMetadata,
    Result,
    SumStats,
    VerifiedSetting,
    result_from_iperf_json,
)
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy, prepare_trials, run_plan

FIXTURE = Path(__file__).parent / "fixtures/native/3.21/tcp-forward-client.json"


def _worker_result(
    *,
    protocol: str = "tcp",
    family: str = "ipv4",
    dont_fragment: bool | None = None,
    bps: float = 4_000_000.0,
) -> Result:
    host = "2001:db8::1" if family == "ipv6" else "127.0.0.1"
    local = "fe80::1%eth0" if family == "ipv6" else "127.0.0.1"
    byte_count = int(bps / 8)
    start: dict[str, object] = {
        "version": "iperf 3.21",
        "system_info": "Linux synthetic fixture",
        "connecting_to": {"host": host, "port": 5201},
        "test_start": {
            "protocol": protocol.upper(),
            "duration": 1,
            "num_streams": 1 if protocol == "udp" else 2,
            "omit": 0,
            "reverse": 0,
            "bidir": 0,
            "blksize": 1200 if protocol == "udp" else 131072,
            "target_bitrate": int(bps),
            "tos": 0,
        },
        "connected": [
            {
                "socket": 5,
                "local_host": local,
                "local_port": 50000,
                "remote_host": host,
                "remote_port": 5201,
            }
        ],
    }
    raw: dict[str, object] = {
        "start": start,
        "end": {
            "sum_sent": {
                "bytes": byte_count,
                "seconds": 1.0,
                "bits_per_second": bps,
            },
            "sum_received": {
                "bytes": byte_count,
                "seconds": 1.0,
                "bits_per_second": bps,
                **(
                    {
                        "packets": 100,
                        "lost_packets": 0,
                        "lost_percent": 0.0,
                        "jitter_ms": 0.1,
                    }
                    if protocol == "udp"
                    else {}
                ),
            },
        },
    }
    result = result_from_iperf_json(raw, reporting_role="client")
    receipts: dict[str, dict[str, object]] = {
        "server": {"value": host},
        "port": {"value": 5201},
        "protocol": {"value": protocol},
        "duration": {"value": 1},
        "parallel": {"value": 1 if protocol == "udp" else 2},
        "omit": {"value": 0},
        "rate": {"value": int(bps)},
        "blksize": {"value": 1200 if protocol == "udp" else 131072},
    }
    if dont_fragment is not None:
        receipts["dont_fragment"] = {"value": dont_fragment}
    result.extensions["iperf3_lib.native_configuration"] = receipts
    return result


def test_observed_address_family_handles_ipv4_ipv6_zone_ids_and_invalid_inputs():
    """Connected socket endpoint literals determine verified IPv4/IPv6 family fail-closed."""
    assert _observed_address_family(
        {"start": {"connected": [{"local_host": "127.0.0.1", "remote_host": "127.0.0.1"}]}}
    ) == (
        "ipv4",
        ["/raw/start/connected/0/local_host", "/raw/start/connected/0/remote_host"],
    )
    assert _observed_address_family(
        {"start": {"connected": [{"local_host": "fe80::1%eth0", "remote_host": "2001:db8::1"}]}}
    ) == (
        "ipv6",
        ["/raw/start/connected/0/local_host", "/raw/start/connected/0/remote_host"],
    )
    for invalid in (
        {},
        {"start": {"connected": []}},
        {"start": {"connected": [{}]}},
        {"start": {"connected": ["not-a-dict"]}},
        {"start": {"connected": [{"local_host": "   "}]}},
        {"start": {"connected": [{"local_host": "localhost"}]}},
        {"start": {"connected": [{"local_host": "127.0.0.1", "remote_host": "::1"}]}},
    ):
        assert _observed_address_family(invalid) is None


def test_explicit_address_family_and_dont_fragment_qualify_across_gates(monkeypatch):
    """Explicit address_family and dont_fragment verify end-to-end across all gates."""
    monkeypatch.setattr(
        "iperf3_lib._execution.run_worker",
        lambda _role, requested_cfg, **_kwargs: _worker_result(
            protocol="udp",
            family="ipv4",
            dont_fragment=True,
            bps=float(requested_cfg["rate"]),
        ),
    )
    cfg = ClientConfig(
        "127.0.0.1",
        protocol="udp",
        duration=1,
        parallel=1,
        blksize=1200,
        rate=4_000_000,
        address_family="ipv4",
        dont_fragment=True,
    )
    first = Client(cfg).run()
    second = Client(cfg).run()

    assert first.execution is not None
    assert first.execution.configuration.effective["address_family"] == VerifiedSetting(
        "ipv4",
        "verified",
        ["/raw/start/connected/0/local_host", "/raw/start/connected/0/remote_host"],
    )
    assert first.execution.configuration.effective["dont_fragment"] == VerifiedSetting(
        True,
        "verified",
        ["/extensions/iperf3_lib.native_configuration/dont_fragment/value"],
    )
    assert not [d for d in first.diagnostics if d.code == "configuration.difference"]

    restored = loads_artifact(dumps_artifact(artifact_from_result(first)))
    assert restored.result.execution.configuration.effective["address_family"].value == "ipv4"
    assert restored.result.execution.configuration.effective["dont_fragment"].value is True

    compatibility = check_compatibility(
        [AnalysisTrial("t0", first), AnalysisTrial("t1", second)],
        policy=ComparisonPolicy("loopback", ("client", "server")),
    )
    assert compatibility.compatible

    plan = prepare_trials(
        cfg,
        policy=TrialPolicy(repetitions=2, warmup_runs=0),
        budget=PlanBudget(100, 100_000_000),
    )
    execution = run_plan(plan)
    report = assess_plan(
        execution,
        policy=AssessmentPolicy(minimum_throughput_bps=800, minimum_valid_trials=2),
        comparison=ComparisonPolicy("loopback", ("client", "server")),
    )
    assert report.assessment.outcome == "pass"

    sweep = prepare_sweep(
        cfg,
        (SweepAxis("blksize", (1200,)),),
        policy=TrialPolicy(repetitions=2, warmup_runs=0),
        budget=PlanBudget(100, 100_000_000),
    )
    sweep_summary = run_sweep(
        sweep,
        minimum_valid_trials=2,
        comparison_policy=ComparisonPolicy("loopback", ("client", "server")),
    )
    assert sweep_summary.cells[0].directions[0].quality == "complete"

    udp_cfg = ClientConfig(
        "127.0.0.1",
        protocol="udp",
        duration=1,
        parallel=1,
        blksize=1200,
        address_family="ipv4",
        dont_fragment=True,
    )
    adaptive_plan = prepare_adaptive_udp(
        udp_cfg,
        (2_000_000, 4_000_000),
        policy=TrialPolicy(repetitions=2, max_trials=30),
        budget=PlanBudget(30, 100_000_000),
        adaptive_policy=AdaptiveUDPPolicy(
            min_rate_bps=2_000_000,
            max_rate_bps=4_000_000,
            max_distinct_rates=4,
            max_refinement_depth=1,
            receiver_loss_percent=5,
            minimum_valid_trials=2,
            minimum_sender_fraction=0.9,
        ),
    )
    adaptive_result = run_adaptive_udp(adaptive_plan)
    assert adaptive_result.outcome == "acceptable_tested_rates"


def test_mismatched_explicit_address_family_emits_difference_and_blocks_adaptive_and_compatibility(
    monkeypatch,
):
    """Requesting ipv6 while connecting over ipv4 records a difference and fails adaptive/compatibility."""
    monkeypatch.setattr(
        "iperf3_lib._execution.run_worker",
        lambda _role, requested_cfg, **_kwargs: _worker_result(
            protocol="udp",
            family="ipv4",
            bps=float(requested_cfg["rate"]),
        ),
    )
    udp_cfg = ClientConfig(
        "127.0.0.1",
        protocol="udp",
        duration=1,
        parallel=1,
        blksize=1200,
        address_family="ipv6",
    )
    result = Client(replace(udp_cfg, rate=4_000_000)).run()
    assert any(
        d.code == "configuration.difference"
        and d.path == "/execution/configuration/effective/address_family"
        for d in result.diagnostics
    )

    adaptive_plan = prepare_adaptive_udp(
        udp_cfg,
        (2_000_000, 4_000_000),
        policy=TrialPolicy(repetitions=2, max_trials=30),
        budget=PlanBudget(30, 100_000_000),
        adaptive_policy=AdaptiveUDPPolicy(
            min_rate_bps=2_000_000,
            max_rate_bps=4_000_000,
            max_distinct_rates=4,
            max_refinement_depth=1,
            receiver_loss_percent=5,
            minimum_valid_trials=2,
            minimum_sender_fraction=0.9,
        ),
    )
    adaptive_result = run_adaptive_udp(adaptive_plan)
    assert adaptive_result.outcome == "inconclusive"

    monkeypatch.setattr(
        "iperf3_lib._execution.run_worker",
        lambda *_args, **_kwargs: _worker_result(protocol="udp", family="ipv6", bps=4_000_000.0),
    )
    ipv6_result = Client(
        ClientConfig(
            "2001:db8::1",
            protocol="udp",
            duration=1,
            parallel=1,
            blksize=1200,
            rate=4_000_000,
            address_family="ipv6",
        )
    ).run()
    compatibility = check_compatibility(
        [AnalysisTrial("t0", result), AnalysisTrial("t1", ipv6_result)],
        policy=ComparisonPolicy("loopback", ("client", "server")),
    )
    assert not compatibility.compatible


def test_public_modules_define_complete_all_exports():
    """Every public module in iperf3_lib defines __all__ with valid attribute names."""
    package_path = Path(iperf3_lib.__file__).parent
    checked = 0
    for module_info in pkgutil.walk_packages([str(package_path)], prefix="iperf3_lib."):
        short_name = module_info.name.rsplit(".", 1)[-1]
        if short_name.startswith("_"):
            continue
        module = importlib.import_module(module_info.name)
        assert hasattr(module, "__all__"), f"{module_info.name} is missing __all__"
        exports = module.__all__
        assert isinstance(exports, list) and exports, (
            f"{module_info.name}.__all__ must be non-empty"
        )
        for symbol in exports:
            assert isinstance(symbol, str)
            assert hasattr(module, symbol), f"{module_info.name}.__all__ exports missing {symbol!r}"
        checked += 1
    assert checked >= 20


def test_consumer_static_typing_passes_ty_check(tmp_path):
    """A strict downstream consumer exercising public entrypoints passes ty check."""
    consumer = tmp_path / "consumer_typing_check.py"
    consumer.write_text(
        textwrap.dedent(
            """\
            from ipaddress import ip_address
            from iperf3_lib import Client, ClientConfig, Protocol, Result
            from iperf3_lib.adaptive import prepare_adaptive_udp
            from iperf3_lib.adaptive_execution import run_adaptive_udp
            from iperf3_lib.analysis import check_compatibility
            from iperf3_lib.artifacts import ResultArtifact, artifact_from_result
            from iperf3_lib.async_trials import arun_plan
            from iperf3_lib.concurrent_trials import arun_concurrent_plan
            from iperf3_lib.exporters.prometheus import render_text as render_prometheus_text
            from iperf3_lib.sweeps import prepare_sweep, run_sweep
            from iperf3_lib.trials import prepare_trials, run_plan

            cfg_v4 = ClientConfig(ip_address("127.0.0.1"), protocol="udp", duration=1)
            cfg_v6 = ClientConfig(ip_address("::1"), protocol=Protocol.TCP, duration=1)
            client = Client(cfg_v4)
            assert client.cfg.server == ip_address("127.0.0.1")
            assert cfg_v6.protocol is Protocol.TCP
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "ty", "check", str(consumer)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_validation_and_reporting_symmetries(monkeypatch):
    """Result, artifact, report, exporter, and concurrent JUnit validators enforce symmetry."""
    # 1. result_from_iperf_json rejects whitespace-only version and system_info
    for field_name in ("version", "system_info"):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw["start"][field_name] = "   "
        with pytest.raises(ValueError, match=f"iperf JSON {field_name} must be a nonempty string"):
            result_from_iperf_json(raw)

    # 2. artifacts.py rejects whitespace-only native_system_info, server, and bad extended settings
    base_result = result_from_iperf_json(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert base_result.execution is not None
    base_result.execution.configuration.requested = {
        "server": "127.0.0.1",
        "port": 5201,
        "protocol": "tcp",
        "duration": 1,
        "parallel": 2,
        "omit": 0,
        "reverse": False,
        "bidirectional": False,
        "mptcp": False,
        "blksize": 131072,
        "rate": 4_000_000,
        "tos": 0,
        "json_stream": False,
    }
    artifact_dict = artifact_to_dict(artifact_from_result(base_result))

    bad_sysinfo = copy.deepcopy(artifact_dict)
    bad_sysinfo["result"]["execution"]["native_system_info"] = "   "
    with pytest.raises(ArtifactValidationError, match="native_system_info"):
        loads_artifact(json.dumps(bad_sysinfo))

    bad_server = copy.deepcopy(artifact_dict)
    bad_server["result"]["execution"]["configuration"]["effective"]["server"]["value"] = "   "
    with pytest.raises(ArtifactValidationError, match="server"):
        loads_artifact(json.dumps(bad_server))

    bad_pointer = copy.deepcopy(artifact_dict)
    bad_pointer["result"]["execution"]["configuration"]["effective"]["server"]["evidence_paths"] = [
        "relative/pointer"
    ]
    with pytest.raises(ArtifactValidationError, match="JSON Pointer"):
        loads_artifact(json.dumps(bad_pointer))

    for field_name, invalid_value in (
        ("interval_seconds", 0.05),
        ("socket_buffer_bytes", -1),
        ("control_keepalive", [0, 0, 2**31]),
        ("bind_address", "127.0.0.1\x00"),
        ("sctp_bind_addresses", ["127.0.0.1\x00"]),
    ):
        bad_ext = copy.deepcopy(artifact_dict)
        bad_ext["result"]["execution"]["configuration"]["effective"][field_name] = {
            "value": invalid_value,
            "state": "verified",
            "evidence_paths": ["/raw/start"],
        }
        with pytest.raises(ArtifactValidationError):
            loads_artifact(json.dumps(bad_ext))

    # 3. reports._convert handles typing.Union[int, None] alongside int | None
    assert _convert(Union[int, None], 42, "field") == 42  # noqa: UP007
    assert _convert(Union[int, None], None, "field") is None  # noqa: UP007

    # 4. Prometheus legacy result.end fallback respects reverse and bidirectional
    sender = SumStats(bits_per_second=1000, bytes=125, duration_seconds=1)
    receiver = SumStats(bits_per_second=900, bytes=112, duration_seconds=1)
    rev_result = Result(
        ok=True,
        protocol="tcp",
        end=EndStats(sender, receiver),
        execution=ExecutionMetadata(status="completed", method="reverse"),
    )
    rev_text = render_prometheus_text(rev_result)
    assert 'direction="server_to_client"' in rev_text
    assert 'direction="client_to_server"' not in rev_text

    bidir_result = Result(
        ok=True,
        protocol="tcp",
        bidirectional=True,
        end=EndStats(sender, receiver),
        execution=ExecutionMetadata(status="completed", method="bidirectional"),
    )
    bidir_text = render_prometheus_text(bidir_result)
    assert "iperf3_flow_throughput_bits_per_second" not in bidir_text

    # 5. render_concurrent_junit includes events_observed and events_dropped in <system-out>
    from iperf3_lib.events import NativeEvent
    from iperf3_lib.trials import TrialException

    single_plan = prepare_trials(
        ClientConfig("127.0.0.1", duration=1, parallel=2, rate=4_000_000, address_family="ipv4"),
        policy=TrialPolicy(repetitions=1, warmup_runs=0),
        budget=PlanBudget(10, 100_000_000),
    )
    prepared, cpolicy, resources, keys, rates = prepare_concurrent_plan(
        single_plan,
        ConcurrentExecutionPolicy(1, 8_000_000),
    )
    crecord = ConcurrentTrialRecord(
        prepared.trials[0],
        "exception",
        keys[0],
        rates[0],
        1,
        0.0,
        1.0,
        1.0,
        artifact=None,
        exception=TrialException("example.WorkerError", "no final artifact"),
        started_at_seconds=100.0,
        completed_at_seconds=101.0,
        elapsed_seconds=1.0,
        cleanup_confirmed=True,
        partial_events=(NativeEvent("interval", {"sum": {"bytes": 20}}, 1, 101),),
        events_observed=2,
        events_dropped=1,
    )
    cexec = ConcurrentPlanResult(
        prepared,
        cpolicy,
        resources,
        (crecord,),
        100.0,
        101.0,
        1.0,
        None,
        None,
        None,
        None,
        None,
    )
    junit_xml = render_concurrent_junit(concurrent_report_from_execution(cexec))
    root = ET.fromstring(junit_xml)
    system_out = json.loads(root.findtext("./testcase/system-out") or "{}")
    assert system_out["events_observed"] == 2
    assert system_out["events_dropped"] == 1
