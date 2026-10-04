"""Bounded UDP exploration against real native traffic and owned link impairment."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

CASES = ("clean-forward", "clean-reverse", "impaired-forward")

SCENARIO = textwrap.dedent(
    r"""
    import hashlib
    import importlib.metadata
    import json
    import os
    import pathlib
    import platform
    import socket
    import subprocess
    import sys
    import time

    from iperf3_lib.adaptive import AdaptiveUDPPolicy, prepare_adaptive_udp
    from iperf3_lib.adaptive_execution import run_adaptive_udp
    from iperf3_lib.adaptive_reports import (
        report_from_adaptive_udp, dumps_adaptive_udp_report, loads_adaptive_udp_report,
    )
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.ffi.api import ffi, lib
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.trials import PlanBudget, TrialPolicy

    case = sys.argv[1]
    impaired = case == 'impaired-forward'
    host = '127.0.0.1'
    with socket.socket() as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]
    server = None
    owned_qdisc = False
    impairment = None

    def tc(*arguments):
        return subprocess.check_output(['tc', *arguments], text=True, timeout=5)

    def qdiscs():
        return json.loads(tc('-j', '-s', 'qdisc', 'show', 'dev', 'lo'))

    def default_qdisc(items):
        return len(items) == 1 and items[0]['kind'] == 'noqueue' and items[0]['handle'] == '0:'

    def clear_impairment():
        global owned_qdisc
        if owned_qdisc:
            assert any(item['handle'] == '34:' and item['kind'] == 'prio' for item in qdiscs())
            tc('qdisc', 'del', 'dev', 'lo', 'root', 'handle', '34:')
            owned_qdisc = False
            assert default_qdisc(qdiscs())

    try:
        if impaired:
            assert os.environ.get('IPERF3_ADAPTIVE_IMPAIRMENT') == '1'
            assert pathlib.Path('/.dockerenv').is_file(), 'impairment requires a disposable Docker container'
            before = qdiscs()
            assert default_qdisc(before), 'refusing to replace an existing non-default loopback qdisc'
            commands = [
                ['qdisc', 'add', 'dev', 'lo', 'root', 'handle', '34:', 'prio'],
                ['qdisc', 'add', 'dev', 'lo', 'parent', '34:3', 'handle', '343:',
                 'netem', 'rate', '1000kbit', 'limit', '5'],
                ['filter', 'add', 'dev', 'lo', 'protocol', 'ip', 'parent', '34:',
                 'prio', '3', 'u32', 'match', 'ip', 'protocol', '17', '0xff',
                 'match', 'ip', 'dport', str(port), '0xffff', 'flowid', '34:3'],
            ]
            tc(*commands[0])
            owned_qdisc = True
            for command in commands[1:]:
                tc(*command)
            impairment = {
                'device': 'lo', 'rate_bps': 1_000_000, 'limit_packets': 5,
                'protocol': 'udp', 'destination_port': port, 'commands': commands,
                'before': before, 'configured': qdiscs(),
                'filters': json.loads(tc('-j', 'filter', 'show', 'dev', 'lo', 'parent', '34:')),
                'filters_text': tc('filter', 'show', 'dev', 'lo', 'parent', '34:'),
            }
        server = subprocess.Popen(
            ['iperf3', '-s', '-B', host, '-p', str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while True:
            assert server.poll() is None, 'native server exited before listening'
            try:
                with socket.create_connection((host, port), timeout=0.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, 'native server did not listen'
                time.sleep(0.02)
        config = ClientConfig(
            host, port=port, protocol='udp', duration=1, parallel=2, blksize=1200,
            reverse=case == 'clean-reverse',
        )
        prepared = prepare_adaptive_udp(
            config, (250_001, 2_000_001),
            # Five queued 1228-byte IPv4 datagrams have a nominal 49.12 ms service time.
            # Allow additional drain time before reusing the UDP port; scheduling varies.
            policy=TrialPolicy(repetitions=2, warmup_runs=1, pause_seconds=0.1, max_trials=18),
            budget=PlanBudget(max_active_seconds=18, max_payload_bytes=8_000_000),
            adaptive_policy=AdaptiveUDPPolicy(
                min_rate_bps=250_001, max_rate_bps=2_000_001, max_distinct_rates=3,
                max_refinement_depth=1, receiver_loss_percent=5,
                minimum_valid_trials=2, minimum_sender_fraction=0.9,
            ),
        )
        result = run_adaptive_udp(prepared)
        report = report_from_adaptive_udp(result)
        persisted = dumps_adaptive_udp_report(report)
        assert loads_adaptive_udp_report(persisted) == report
        failed = [
            {'trial_id': record.spec.trial_id, 'status': record.status,
             'error': record.artifact.result.error if record.artifact else None,
             'exception': str(record.exception)}
            for batch in result.batches for record in batch.execution.trials
            if record.status != 'completed'
        ]
        if failed:
            diagnostic = pathlib.Path(sys.argv[2])
            diagnostic.write_text(json.dumps({
                'case': case, 'report': json.loads(persisted),
                'impairment': impairment, 'qdisc': qdiscs() if impaired else None,
            }))
            raise AssertionError(json.dumps({'diagnostic': str(diagnostic), 'failures': failed}))
        measurements = []
        for batch in result.batches:
            assert batch.execution.execution_success
            for record in batch.execution.trials:
                raw = record.artifact.result.raw
                assert raw['start']['test_start']['protocol'] == 'UDP'
                sender, receiver = raw['end']['sum_sent'], raw['end']['sum_received']
                assert sender['bytes'] > 0 and receiver['bytes'] > 0
                assert sender['seconds'] > 0 and receiver['seconds'] > 0
                assert receiver['packets'] > 0
                measurements.append({
                    'trial_id': record.spec.trial_id,
                    'native_start': raw['start']['test_start'],
                    'sender': sender, 'receiver': receiver,
                })
        assert result.outcome == 'acceptable_tested_rates'
        if impaired:
            assert result.highest_eligible_bps < 2_000_001
            assert next(item for item in result.summaries if item.rate_bps == 250_001).status == 'eligible'
            assert next(item for item in result.summaries if item.rate_bps == 2_000_001).status == 'rejected'
            impairment['after_traffic'] = qdiscs()
            assert next(item for item in impairment['after_traffic'] if item['handle'] == '343:')['drops'] > 0
            clear_impairment()
            impairment['after_cleanup'] = qdiscs()
        else:
            assert result.highest_eligible_bps == 2_000_001 and result.ceiling_censored
        reuse = Client(ClientConfig(
            host, port=port, protocol='udp', duration=1, rate=250_000, blksize=1200,
        )).run()
        assert reuse.ok and reuse.raw['end']['sum_received']['bytes'] > 0
        server.terminate()
        server.wait(timeout=3)
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind((host, port))
        print(json.dumps({
            'case': case, 'port': port,
            'producer': {
                'package_version': importlib.metadata.version('iperf3-lib'),
                'python_version': platform.python_version(),
                'native_version': ffi.string(lib.iperf_get_iperf_version()).decode(),
            },
            'server_returncode': server.returncode, 'listener_released': True,
            'reuse_native': reuse.raw, 'measurements': measurements, 'impairment': impairment,
            'report_json': persisted, 'report_sha256': hashlib.sha256(persisted.encode()).hexdigest(),
        }))
    finally:
        clear_impairment()
        if server is not None and server.poll() is None:
            server.kill()
            server.wait(timeout=3)
    """
)


def test_adaptive_native_scenario_compiles():
    """Keep the bounded subprocess parseable without loading native code."""
    compile(SCENARIO, "<native-adaptive-scenario>", "exec")


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES)
def test_native_adaptive_udp_preserves_measured_decisions(case, record_property, tmp_path):
    """Real sender and receiver evidence establishes only tested adaptive decisions."""
    if case == "impaired-forward" and os.environ.get("IPERF3_ADAPTIVE_IMPAIRMENT") != "1":
        pytest.skip("owned impairment requires an explicitly enabled disposable container")
    diagnostic = tmp_path / "adaptive-failure.json"
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, case, str(diagnostic)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if diagnostic.is_file():
        record_property("adaptive_failure", diagnostic.read_text())
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["case"] == case and receipt["listener_released"] is True
    record_property("adaptive_udp", json.dumps(receipt))


def test_adaptive_native_failure_retains_full_report_before_asserting(monkeypatch, tmp_path):
    """CI properties retain failed native evidence before the disposable container exits."""
    evidence = {"case": "clean-forward", "report": {"failed": "native error"}}

    def fail(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(evidence))
        return SimpleNamespace(returncode=1, stdout="", stderr="native failure")

    monkeypatch.setattr(subprocess, "run", fail)
    properties = []
    with pytest.raises(AssertionError, match="native failure"):
        test_native_adaptive_udp_preserves_measured_decisions(
            "clean-forward", lambda name, value: properties.append((name, value)), tmp_path
        )
    assert properties == [("adaptive_failure", json.dumps(evidence))]
