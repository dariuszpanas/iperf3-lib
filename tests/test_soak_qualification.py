"""Unit and integration tests for bounded mixed-workload soak qualification."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts import qualify_soak as soak


def _synthetic_snapshot(
    *,
    rss_kib: int = 32_000,
    fd_count: int = 8,
    child_count: int = 0,
) -> dict[str, Any]:
    return {
        "rss_kib": rss_kib,
        "hwm_kib": max(rss_kib, 34_000),
        "fd_count": fd_count,
        "socket_fd_count": 0,
        "pipe_fd_count": 0,
        "child_count": child_count,
    }


def _synthetic_receipt(
    *,
    total_cycles: int = 12,
    warmup_cycles: int = 4,
    rss_step_kib: int = 0,
) -> dict[str, Any]:
    tolerances = soak.SoakTolerances(total_cycles=total_cycles, warmup_cycles=warmup_cycles)
    baseline = _synthetic_snapshot(rss_kib=30_000, fd_count=8)
    modes = soak.BASE_MODES
    outcomes = {
        "tcp-complete": "completed",
        "udp-events-complete": "completed",
        "native-failure": "failed",
        "worker-timeout": "timeout",
        "async-cancel-tcp": "cancelled",
        "udp-cancel": "cancelled",
    }
    cycles = []
    for idx in range(total_cycles):
        mode = modes[idx % len(modes)]
        post_warmup_idx = max(0, idx - (warmup_cycles - 1))
        rss_val = 32_000 + post_warmup_idx * rss_step_kib
        snap = _synthetic_snapshot(rss_kib=rss_val, fd_count=8, child_count=0)
        cycles.append(
            {
                "index": idx,
                "warmup": idx < warmup_cycles,
                "mode": mode,
                "protocol": "udp" if "udp" in mode else "tcp",
                "port": 40_000 + idx,
                "outcome": outcomes[mode],
                "outcome_matched": True,
                "delivered_bytes": 64_000 if mode != "native-failure" else 0,
                "result_bytes": 125_000 if outcomes[mode] == "completed" else 0,
                "workers_spawned": 4,
                "workers_reaped": True,
                "listener_active_after_operation": False,
                "listener_active_after_reuse": False,
                "after_operation": copy.deepcopy(snap),
                "after_reuse": copy.deepcopy(snap),
                "reuse": {
                    "ok": True,
                    "protocol": "udp" if "udp" in mode else "tcp",
                    "bytes": 125_000,
                    "worker_id": f"{idx:032x}",
                    "distinct_worker": True,
                },
                "elapsed_seconds": 1.25,
            }
        )
    final = copy.deepcopy(cycles[-1]["after_reuse"])
    evaluated = soak.evaluate_soak_cycles(
        cycles, baseline=baseline, final=final, tolerances=tolerances
    )
    return {
        "kind": soak.SOAK_RECEIPT_KIND,
        "schema_version": soak.SOAK_SCHEMA_VERSION,
        "status": "passed" if not evaluated["violations"] else "failed",
        "source_revision": "a" * 40,
        "package_version": "0.4.2",
        "runtime": {
            "python_version": "3.15.0",
            "implementation": "CPython",
            "platform": "Linux-6.8.0-x86_64-with-glibc2.39",
            "native_version": "iperf 3.22",
            "sctp_support": "supported",
        },
        "tolerances": asdict(tolerances),
        "elapsed_seconds": 15.0,
        "baseline": baseline,
        "final": final,
        "summary": evaluated["summary"],
        "violations": evaluated["violations"],
        "cycles": cycles,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_cycles": 7},
        {"total_cycles": True},
        {"warmup_cycles": 0},
        {"total_cycles": 8, "warmup_cycles": 5},
        {"max_rss_growth_kib": -1},
        {"max_rss_slope_kib_per_cycle": -0.1},
        {"max_rss_slope_kib_per_cycle": float("nan")},
        {"max_rss_slope_kib_per_cycle": True},
        {"max_final_fd_delta": -1},
        {"require_same_port_reuse": 1},
    ],
)
def test_soak_tolerances_reject_invalid_bounds(kwargs: dict[str, Any]) -> None:
    """Reject malformed, boolean, non-finite, or undersized tolerance declarations."""
    with pytest.raises(ValueError):
        soak.SoakTolerances(**kwargs)


def test_procfs_parsers_read_memory_listeners_and_process_identity(tmp_path: Path) -> None:
    """Parse synthetic procfs status, stat, and tcp listener records deterministically."""
    pid = 4242
    proc_dir = tmp_path / str(pid)
    (proc_dir / "task" / str(pid)).mkdir(parents=True)
    (proc_dir / "fd").mkdir(parents=True)
    fields = ["S", "1", *(["0"] * 17), "987654", "0", "0"]
    (proc_dir / "stat").write_bytes(f"{pid} (python3.15) {' '.join(fields)}\n".encode("ascii"))
    (proc_dir / "status").write_text(
        "Name:\tpython3.15\nVmHWM:\t   41200 kB\nVmRSS:\t   38400 kB\n",
        encoding="utf-8",
    )
    (proc_dir / "task" / str(pid) / "children").write_bytes(b"")

    assert soak.process_identity_matches(pid, 987654, proc_root=tmp_path) is True
    assert soak.process_identity_matches(pid, 111111, proc_root=tmp_path) is False
    assert soak.process_identity_matches(9999, 987654, proc_root=tmp_path) is False
    assert soak.read_proc_memory_kib(pid, proc_root=tmp_path) == {
        "rss_kib": 38400,
        "hwm_kib": 41200,
    }
    snap = soak.snapshot_proc_resources(pid, proc_root=tmp_path)
    assert snap["rss_kib"] == 38400
    assert snap["child_count"] == 0
    assert snap["fd_count"] == 0

    net_dir = tmp_path / "net"
    net_dir.mkdir()
    (net_dir / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        f"   0: 0100007F:{5201:04X} 00000000:0000 0A 00000000:00000000\n"
        f"   1: 0100007F:{5202:04X} 0100007F:9999 01 00000000:00000000\n",
        encoding="utf-8",
    )
    assert soak.port_has_tcp_listener(5201, proc_root=tmp_path) is True
    assert soak.port_has_tcp_listener(5202, proc_root=tmp_path) is False


def test_evaluate_and_validate_clean_soak_receipt(tmp_path: Path) -> None:
    """Accept a valid mixed soak receipt and verify CLI round-trip validation."""
    receipt = _synthetic_receipt(total_cycles=12, warmup_cycles=4, rss_step_kib=16)
    soak.validate_soak_receipt(
        receipt,
        expected_revision="a" * 40,
        expected_python="3.15",
        expected_native_version="3.22",
    )
    path = tmp_path / "soak-receipt.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert (
        soak.main(
            [
                "verify",
                "--receipt",
                str(path),
                "--revision",
                "a" * 40,
                "--expected-python",
                "3.15",
                "--expected-native-version",
                "3.22",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "rss-ceiling",
        "rss-slope",
        "final-fd-leak",
        "cycle-fd-spike",
        "surviving-child",
        "active-listener",
        "failed-reuse",
        "reused-worker-id",
        "unreaped-worker",
        "missing-cancelled",
    ],
)
def test_soak_evaluator_detects_every_resource_and_reuse_violation(mutation: str) -> None:
    """Report explicit tolerance violations for memory, FD, child, listener, and reuse faults."""
    receipt = _synthetic_receipt(total_cycles=12, warmup_cycles=4, rss_step_kib=0)
    tolerances = soak.SoakTolerances(**receipt["tolerances"])
    cycles = receipt["cycles"]
    final = receipt["final"]

    if mutation == "rss-ceiling":
        cycles[-1]["after_reuse"]["rss_kib"] = 32_000 + tolerances.max_rss_growth_kib + 1
        final["rss_kib"] = cycles[-1]["after_reuse"]["rss_kib"]
    elif mutation == "rss-slope":
        tolerances = soak.SoakTolerances(
            total_cycles=12,
            warmup_cycles=4,
            max_rss_growth_kib=50_000,
            max_rss_slope_kib_per_cycle=100.0,
        )
        for idx in range(4, 12):
            cycles[idx]["after_reuse"]["rss_kib"] = 32_000 + (idx - 3) * 200
        final["rss_kib"] = cycles[-1]["after_reuse"]["rss_kib"]
    elif mutation == "final-fd-leak":
        final["fd_count"] = 9
    elif mutation == "cycle-fd-spike":
        cycles[6]["after_reuse"]["fd_count"] = 11
    elif mutation == "surviving-child":
        cycles[5]["after_operation"]["child_count"] = 1
    elif mutation == "active-listener":
        cycles[7]["listener_active_after_reuse"] = True
    elif mutation == "failed-reuse":
        cycles[8]["reuse"]["ok"] = False
    elif mutation == "reused-worker-id":
        cycles[8]["reuse"]["distinct_worker"] = False
    elif mutation == "unreaped-worker":
        cycles[4]["workers_reaped"] = False
    elif mutation == "missing-cancelled":
        for cycle in cycles:
            if cycle["outcome"] == "cancelled":
                cycle["outcome"] = "completed"

    evaluated = soak.evaluate_soak_cycles(
        cycles, baseline=receipt["baseline"], final=final, tolerances=tolerances
    )
    assert evaluated["violations"]


def test_soak_receipt_validator_rejects_tampered_or_prerelease_evidence(tmp_path: Path) -> None:
    """Reject duplicate JSON keys, prerelease runtimes, mismatched revisions, and stale summaries."""
    duplicate_path = tmp_path / "dup.json"
    duplicate_path.write_text('{"kind": "a", "kind": "b"}\n', encoding="utf-8")
    assert soak.main(["verify", "--receipt", str(duplicate_path)]) == 1

    prerelease = _synthetic_receipt()
    prerelease["runtime"]["python_version"] = "3.15.0rc3"
    with pytest.raises(ValueError, match="final"):
        soak.validate_soak_receipt(prerelease)

    wrong_rev = _synthetic_receipt()
    with pytest.raises(ValueError, match="expected revision"):
        soak.validate_soak_receipt(wrong_rev, expected_revision="b" * 40)

    tampered = _synthetic_receipt()
    tampered["summary"]["rss_growth_kib"] = -999999
    with pytest.raises(ValueError, match="summary disagrees"):
        soak.validate_soak_receipt(tampered)


def test_execute_soak_refuses_parent_with_preexisting_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to arm PR_SET_CHILD_SUBREAPER when the caller already owns child processes."""
    import asyncio
    import sysconfig

    monkeypatch.setattr(soak.sys, "platform", "linux")
    monkeypatch.setattr(sysconfig, "get_config_var", lambda _name: 0)
    monkeypatch.setattr(
        soak,
        "snapshot_proc_resources",
        lambda _pid: _synthetic_snapshot(child_count=1),
    )
    with pytest.raises(RuntimeError, match="no pre-existing children"):
        asyncio.run(soak._execute_soak(soak.SoakTolerances(total_cycles=8, warmup_cycles=2)))


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs soak qualification")
def test_live_soak_qualification_short_run(tmp_path: Path) -> None:
    """Run an 8-cycle mixed soak workload in an isolated Linux subprocess."""
    output = tmp_path / "live-soak.json"
    script_path = Path(soak.__file__).resolve()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script_path),
            "run",
            "--cycles",
            "8",
            "--warmup-cycles",
            "2",
            "--output",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=180)
    finally:
        if proc.poll() is None:
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    assert proc.returncode == 0, stdout + stderr
    receipt = soak.load_and_validate_soak_receipt(output)
    assert receipt["status"] == "passed"
    assert receipt["summary"]["reuse_passes"] == 8
    assert receipt["summary"]["final_fd_delta"] <= 0
    assert receipt["summary"]["max_surviving_children"] == 0
