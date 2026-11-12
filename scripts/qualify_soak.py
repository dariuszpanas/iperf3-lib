"""Bounded mixed-workload soak qualification with pre-declared resource tolerances."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import importlib.metadata
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

SOAK_RECEIPT_KIND = "iperf3-lib.soak-qualification"
SOAK_SCHEMA_VERSION = 1
BASE_MODES = (
    "tcp-complete",
    "udp-events-complete",
    "native-failure",
    "worker-timeout",
    "async-cancel-tcp",
    "udp-cancel",
)


@dataclass(frozen=True)
class SoakTolerances:
    """Resource and operational bounds declared before soak execution begins."""

    total_cycles: int = 24
    warmup_cycles: int = 4
    max_rss_growth_kib: int = 16_384
    max_rss_slope_kib_per_cycle: float = 256.0
    max_final_fd_delta: int = 0
    max_cycle_fd_delta: int = 2
    max_surviving_children: int = 0
    max_active_listeners_after_cleanup: int = 0
    require_same_port_reuse: bool = True

    def __post_init__(self) -> None:
        """Validate pre-declared soak bounds before running any native workload."""
        for field_name, minimum in (
            ("total_cycles", 8),
            ("warmup_cycles", 1),
            ("max_rss_growth_kib", 0),
            ("max_final_fd_delta", 0),
            ("max_cycle_fd_delta", 0),
            ("max_surviving_children", 0),
            ("max_active_listeners_after_cleanup", 0),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{field_name} must be an integer >= {minimum}")
        if self.total_cycles - self.warmup_cycles < 4:
            raise ValueError("total_cycles must exceed warmup_cycles by at least 4 measured cycles")
        if (
            not isinstance(self.max_rss_slope_kib_per_cycle, (int, float))
            or isinstance(self.max_rss_slope_kib_per_cycle, bool)
            or not math.isfinite(self.max_rss_slope_kib_per_cycle)
            or self.max_rss_slope_kib_per_cycle < 0
        ):
            raise ValueError("max_rss_slope_kib_per_cycle must be a non-negative finite number")
        if type(self.require_same_port_reuse) is not bool:
            raise ValueError("require_same_port_reuse must be a boolean")


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _finite_non_negative(value: Any, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative finite number")
    return float(value)


def _stat_identity(data: bytes, pid: int) -> dict[str, Any]:
    try:
        heading, tail = data.rsplit(b")", 1)
        number, separator, _ = heading.partition(b"(")
        fields = tail.split()
        if not separator or not number.strip().isdigit() or int(number.strip()) != pid:
            raise ValueError("invalid procfs stat pid")
        if len(fields) < 20 or not fields[1].isdigit() or not fields[19].isdigit():
            raise ValueError("invalid procfs stat fields")
        state = fields[0].decode("ascii")
        if len(state) != 1:
            raise ValueError("invalid procfs process state")
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "state": state,
            "start_ticks": int(fields[19]),
        }
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"invalid procfs stat record for pid {pid}") from error


def process_identity_matches(
    pid: int, start_ticks: int, *, proc_root: Path = Path("/proc")
) -> bool:
    """Return True when the original process lifetime is still present in procfs."""
    _strict_int(pid, "pid", minimum=1)
    _strict_int(start_ticks, "start_ticks", minimum=0)
    try:
        raw = (proc_root / str(pid) / "stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return _stat_identity(raw, pid)["start_ticks"] == start_ticks


def read_proc_memory_kib(pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, int]:
    """Read VmRSS and VmHWM in KiB from a Linux procfs status file."""
    _strict_int(pid, "pid", minimum=1)
    status_path = proc_root / str(pid) / "status"
    values: dict[str, int] = {}
    for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, rest = line.split(":", 1)
            parts = rest.split()
            if len(parts) != 2 or not parts[0].isdigit() or parts[1].lower() != "kb":
                raise ValueError(f"unexpected {key} record in {status_path}")
            values[key] = int(parts[0])
    if "VmRSS" not in values:
        raise ValueError(f"VmRSS is missing from {status_path}")
    return {
        "rss_kib": values["VmRSS"],
        "hwm_kib": values.get("VmHWM", values["VmRSS"]),
    }


def port_has_tcp_listener(port: int, *, proc_root: Path = Path("/proc")) -> bool:
    """Check procfs IPv4 and IPv6 TCP tables for an active LISTEN socket on port."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be in 1..65535")
    for table_name in ("tcp", "tcp6"):
        table = proc_root / "net" / table_name
        if not table.is_file():
            continue
        for line in table.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            columns = line.split()
            if len(columns) > 3 and columns[3] == "0A":
                local_parts = columns[1].rsplit(":", 1)
                if len(local_parts) == 2 and int(local_parts[1], 16) == port:
                    return True
    return False


def snapshot_proc_resources(
    pid: int, *, proc_root: Path = Path("/proc"), attempts: int = 3
) -> dict[str, Any]:
    """Sample memory, open descriptors, sockets, and direct children for pid."""
    _strict_int(pid, "pid", minimum=1)
    _strict_int(attempts, "attempts", minimum=1)
    proc_dir = proc_root / str(pid)
    initial = _stat_identity((proc_dir / "stat").read_bytes(), pid)
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            before = _stat_identity((proc_dir / "stat").read_bytes(), pid)
            if before["start_ticks"] != initial["start_ticks"]:
                raise RuntimeError(f"process {pid} identity changed during snapshot")
            memory = read_proc_memory_kib(pid, proc_root=proc_root)
            task_dir = proc_dir / "task"
            tids = sorted((name for name in os.listdir(task_dir) if name.isdigit()), key=int)
            if not tids:
                raise RuntimeError("process has no observable threads")
            child_pids: set[int] = set()
            for tid in tids:
                for token in (task_dir / tid / "children").read_bytes().split():
                    if not token.isdigit() or int(token) < 1:
                        raise ValueError("invalid child PID in procfs children")
                    child_pids.add(int(token))
            children = []
            for child_pid in sorted(child_pids):
                child = _stat_identity(
                    (proc_root / str(child_pid) / "stat").read_bytes(), child_pid
                )
                if child["ppid"] != pid:
                    raise RuntimeError("child parent changed during snapshot")
                children.append(child)
            fd_dir = proc_dir / "fd"
            descriptors: dict[str, str] = {}
            for name in sorted((entry for entry in os.listdir(fd_dir) if entry.isdigit()), key=int):
                try:
                    descriptors[name] = os.readlink(fd_dir / name)
                except (FileNotFoundError, ProcessLookupError):
                    continue
            after_tids = sorted((name for name in os.listdir(task_dir) if name.isdigit()), key=int)
            if after_tids != tids:
                raise RuntimeError("thread set changed during snapshot")
            after = _stat_identity((proc_dir / "stat").read_bytes(), pid)
            if after["start_ticks"] != initial["start_ticks"]:
                raise RuntimeError(f"process {pid} identity changed during snapshot")
            socket_fds = [
                number
                for number, target in descriptors.items()
                if re.fullmatch(r"socket:\[[0-9]+\]", target)
            ]
            pipe_fds = [
                number
                for number, target in descriptors.items()
                if re.fullmatch(r"pipe:\[[0-9]+\]", target)
            ]
            return {
                "identity": after,
                "rss_kib": memory["rss_kib"],
                "hwm_kib": memory["hwm_kib"],
                "fd_count": len(descriptors),
                "socket_fd_count": len(socket_fds),
                "pipe_fd_count": len(pipe_fds),
                "child_count": len(children),
                "children": children,
                "fds": descriptors,
            }
        except (FileNotFoundError, ProcessLookupError, RuntimeError) as error:
            current = _stat_identity((proc_dir / "stat").read_bytes(), pid)
            if current["start_ticks"] != initial["start_ticks"]:
                raise RuntimeError(f"process {pid} identity changed during snapshot") from error
            last_error = error
    raise RuntimeError(f"process {pid} snapshot did not stabilize") from last_error


def linear_slope(values: Sequence[int | float]) -> float:
    """Compute the ordinary-least-squares per-step slope of a numeric series."""
    count = len(values)
    if count < 2:
        return 0.0
    mean_x = (count - 1) / 2.0
    mean_y = sum(float(item) for item in values) / count
    numerator = sum((idx - mean_x) * (float(val) - mean_y) for idx, val in enumerate(values))
    denominator = sum((idx - mean_x) ** 2 for idx in range(count))
    return 0.0 if denominator == 0.0 else numerator / denominator


def evaluate_soak_cycles(
    cycles: Sequence[Mapping[str, Any]],
    *,
    baseline: Mapping[str, Any],
    final: Mapping[str, Any],
    tolerances: SoakTolerances,
) -> dict[str, Any]:
    """Evaluate recorded soak cycles and procfs trends against pre-declared tolerances."""
    violations: list[str] = []
    if len(cycles) != tolerances.total_cycles:
        violations.append(f"expected {tolerances.total_cycles} cycles, observed {len(cycles)}")
    if len(cycles) < tolerances.warmup_cycles + 1:
        raise ValueError("insufficient recorded cycles to compute post-warmup soak trends")

    warmup_anchor = cycles[tolerances.warmup_cycles - 1]["after_reuse"]
    measured_cycles = list(cycles[tolerances.warmup_cycles :])

    anchor_rss = _strict_int(warmup_anchor["rss_kib"], "warmup_anchor.rss_kib", minimum=1)
    anchor_fds = _strict_int(warmup_anchor["fd_count"], "warmup_anchor.fd_count", minimum=1)
    final_rss = _strict_int(final["rss_kib"], "final.rss_kib", minimum=1)
    final_fds = _strict_int(final["fd_count"], "final.fd_count", minimum=1)
    final_children = _strict_int(final["child_count"], "final.child_count", minimum=0)
    baseline_children = _strict_int(baseline["child_count"], "baseline.child_count", minimum=0)

    rss_series = [
        _strict_int(c["after_reuse"]["rss_kib"], "cycle.rss_kib", minimum=1)
        for c in measured_cycles
    ]
    fd_series = [
        _strict_int(c["after_reuse"]["fd_count"], "cycle.fd_count", minimum=1)
        for c in measured_cycles
    ]
    child_series = [
        max(
            _strict_int(c["after_operation"]["child_count"], "after_operation.child_count"),
            _strict_int(c["after_reuse"]["child_count"], "after_reuse.child_count"),
        )
        for c in cycles
    ]

    rss_growth_kib = final_rss - anchor_rss
    max_rss_growth_kib = max([final_rss - anchor_rss, *(val - anchor_rss for val in rss_series)])
    rss_slope = round(linear_slope([anchor_rss, *rss_series]), 6)
    final_fd_delta = final_fds - anchor_fds
    max_cycle_fd_delta = max(val - anchor_fds for val in fd_series)
    max_children_observed = max([baseline_children, final_children, *child_series])
    active_listeners = sum(
        1
        for c in cycles
        if c.get("listener_active_after_operation") is not False
        or c.get("listener_active_after_reuse") is not False
    )
    reuse_passes = sum(
        1
        for c in cycles
        if isinstance(c.get("reuse"), Mapping)
        and c["reuse"].get("ok") is True
        and type(c["reuse"].get("bytes")) is int
        and c["reuse"]["bytes"] > 0
        and c["reuse"].get("distinct_worker") is True
    )
    unreaped_cycles = sum(1 for c in cycles if c.get("workers_reaped") is not True)
    unexpected_outcomes = sum(1 for c in cycles if c.get("outcome_matched") is not True)

    mode_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for idx, cycle in enumerate(cycles):
        if _strict_int(cycle.get("index"), "cycle.index") != idx:
            violations.append(f"cycle {idx} index mismatch")
        if cycle.get("warmup") is not (idx < tolerances.warmup_cycles):
            violations.append(f"cycle {idx} warmup flag mismatch")
        mode = cycle.get("mode")
        outcome = cycle.get("outcome")
        if not isinstance(mode, str) or not mode:
            violations.append(f"cycle {idx} missing mode")
        else:
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
        if not isinstance(outcome, str) or not outcome:
            violations.append(f"cycle {idx} missing outcome")
        else:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    if max_rss_growth_kib > tolerances.max_rss_growth_kib:
        violations.append(
            f"post-warmup RSS growth {max_rss_growth_kib} KiB exceeded {tolerances.max_rss_growth_kib} KiB"
        )
    if rss_slope > tolerances.max_rss_slope_kib_per_cycle:
        violations.append(
            f"post-warmup RSS slope {rss_slope:.3f} KiB/cycle exceeded {tolerances.max_rss_slope_kib_per_cycle:.3f} KiB/cycle"
        )
    if final_fd_delta > tolerances.max_final_fd_delta:
        violations.append(
            f"final FD delta {final_fd_delta} exceeded {tolerances.max_final_fd_delta}"
        )
    if max_cycle_fd_delta > tolerances.max_cycle_fd_delta:
        violations.append(
            f"cycle FD delta {max_cycle_fd_delta} exceeded {tolerances.max_cycle_fd_delta}"
        )
    if max_children_observed > tolerances.max_surviving_children:
        violations.append(
            f"surviving child count {max_children_observed} exceeded {tolerances.max_surviving_children}"
        )
    if active_listeners > tolerances.max_active_listeners_after_cleanup:
        violations.append(
            f"active listeners after cleanup {active_listeners} exceeded {tolerances.max_active_listeners_after_cleanup}"
        )
    if tolerances.require_same_port_reuse and reuse_passes != len(cycles):
        violations.append(f"same-port reuse passed {reuse_passes}/{len(cycles)} cycles")
    if unreaped_cycles:
        violations.append(f"unreaped workers observed in {unreaped_cycles} cycles")
    if unexpected_outcomes:
        violations.append(f"unexpected cycle outcomes observed in {unexpected_outcomes} cycles")
    for required_category in ("completed", "failed", "timeout", "cancelled"):
        if outcome_counts.get(required_category, 0) < 1:
            violations.append(f"mixed workload did not exercise outcome {required_category!r}")

    summary = {
        "total_cycles": len(cycles),
        "warmup_cycles": tolerances.warmup_cycles,
        "measured_cycles": len(measured_cycles),
        "baseline_rss_kib": _strict_int(baseline["rss_kib"], "baseline.rss_kib", minimum=1),
        "post_warmup_rss_kib": anchor_rss,
        "final_rss_kib": final_rss,
        "rss_growth_kib": rss_growth_kib,
        "max_rss_growth_kib": max_rss_growth_kib,
        "rss_slope_kib_per_cycle": rss_slope,
        "baseline_fd_count": _strict_int(baseline["fd_count"], "baseline.fd_count", minimum=1),
        "post_warmup_fd_count": anchor_fds,
        "final_fd_count": final_fds,
        "final_fd_delta": final_fd_delta,
        "max_cycle_fd_delta": max_cycle_fd_delta,
        "max_surviving_children": max_children_observed,
        "active_listeners_after_cleanup": active_listeners,
        "reuse_passes": reuse_passes,
        "mode_counts": mode_counts,
        "outcome_counts": outcome_counts,
    }
    return {"summary": summary, "violations": violations}


def _decode_strict_json(text: str) -> dict[str, Any]:
    def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, val in items:
            if key in obj:
                raise ValueError(f"duplicate JSON key in soak receipt: {key}")
            obj[key] = val
        return obj

    def _bad_constant(token: str) -> Any:
        raise ValueError(f"non-finite JSON constant in soak receipt: {token}")

    parsed = json.loads(text, object_pairs_hook=_pairs, parse_constant=_bad_constant)
    if not isinstance(parsed, dict):
        raise ValueError("soak receipt root must be a JSON object")
    return parsed


def validate_soak_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_revision: str | None = None,
    expected_python: str | None = None,
    expected_native_version: str | None = None,
) -> None:
    """Validate structure, runtime identity, and pre-declared tolerances of a soak receipt."""
    if (
        receipt.get("kind") != SOAK_RECEIPT_KIND
        or receipt.get("schema_version") != SOAK_SCHEMA_VERSION
    ):
        raise ValueError("unexpected soak receipt kind or schema_version")
    if receipt.get("status") != "passed":
        raise ValueError(f"soak receipt status is not passed: {receipt.get('status')!r}")
    revision = receipt.get("source_revision")
    if revision is not None and (
        not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        raise ValueError(
            "soak receipt source_revision must be a 40-character lowercase hex SHA or null"
        )
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("soak receipt source_revision does not match expected revision")

    runtime = receipt.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("soak receipt runtime must be an object")
    if not isinstance(runtime.get("platform"), str) or not runtime["platform"].startswith("Linux-"):
        raise ValueError("soak receipt must come from a Linux runtime")
    python_version = runtime.get("python_version")
    if (
        not isinstance(python_version, str)
        or re.fullmatch(r"\d+\.\d+\.\d+", python_version) is None
    ):
        raise ValueError("soak receipt runtime.python_version must be a final X.Y.Z string")
    if expected_python is not None and not python_version.startswith(f"{expected_python}."):
        raise ValueError(f"soak receipt Python {python_version} does not match {expected_python}")
    native_version = runtime.get("native_version")
    if not isinstance(native_version, str) or not native_version:
        raise ValueError("soak receipt runtime.native_version must be a non-empty string")
    if expected_native_version is not None and native_version not in (
        expected_native_version,
        f"iperf {expected_native_version}",
    ):
        raise ValueError(
            f"soak receipt native_version {native_version!r} does not match {expected_native_version!r}"
        )
    _finite_non_negative(receipt.get("elapsed_seconds"), "elapsed_seconds")

    tol_raw = receipt.get("tolerances")
    if not isinstance(tol_raw, Mapping):
        raise ValueError("soak receipt tolerances must be an object")
    tolerances = SoakTolerances(**dict(tol_raw))

    cycles = receipt.get("cycles")
    baseline = receipt.get("baseline")
    final = receipt.get("final")
    if (
        not isinstance(cycles, list)
        or not isinstance(baseline, Mapping)
        or not isinstance(final, Mapping)
    ):
        raise ValueError("soak receipt must retain baseline, cycles, and final snapshots")

    evaluated = evaluate_soak_cycles(cycles, baseline=baseline, final=final, tolerances=tolerances)
    if evaluated["violations"] or receipt.get("violations") != []:
        raise ValueError(f"soak receipt failed tolerances: {evaluated['violations']}")
    if json.dumps(receipt.get("summary"), sort_keys=True) != json.dumps(
        evaluated["summary"], sort_keys=True
    ):
        raise ValueError("soak receipt summary disagrees with recorded cycle evidence")


def load_and_validate_soak_receipt(
    path: Path,
    *,
    expected_revision: str | None = None,
    expected_python: str | None = None,
    expected_native_version: str | None = None,
) -> dict[str, Any]:
    """Read a soak JSON receipt strictly from disk and validate all invariants."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"soak receipt must be a regular file: {path}")
    receipt = _decode_strict_json(path.read_text(encoding="utf-8"))
    validate_soak_receipt(
        receipt,
        expected_revision=expected_revision,
        expected_python=expected_python,
        expected_native_version=expected_native_version,
    )
    return receipt


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_port_released(port: int) -> None:
    if port_has_tcp_listener(port):
        raise RuntimeError(f"TCP listener remained active on port {port}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def _positive_interval_bytes(kind: str, data: Any) -> int:
    if kind != "interval" or not isinstance(data, Mapping):
        return 0
    summary = data.get("sum")
    if not isinstance(summary, Mapping):
        return 0
    count = summary.get("bytes", 0)
    return count if type(count) is int and count > 0 else 0


async def _wait_until(predicate: Any, message: str, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        await asyncio.sleep(0.02)


async def _probe_sctp_support() -> str:
    """Check whether both libiperf and the Linux kernel support loopback SCTP."""
    from iperf3_lib import Client, ClientConfig, Server, ServerConfig
    from iperf3_lib.exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError

    port = _reserve_loopback_port()
    server = Server(config=ServerConfig(bind_address="127.0.0.1", port=port, interval_seconds=0.25))
    peer = asyncio.create_task(server.aserve_once(timeout=8))
    try:
        await _wait_until(
            lambda: port_has_tcp_listener(port) or peer.done(), "SCTP probe listener timeout"
        )
        if peer.done():
            await peer
            return "unsupported"
        result = await Client(
            ClientConfig("127.0.0.1", port=port, protocol="sctp", duration=1, rate=500_000)
        ).arun(timeout=8)
        await asyncio.wait_for(peer, timeout=8)
        return "supported" if result.ok else "unsupported"
    except (UnsupportedFeatureError, IperfError, IperfLibraryError, TimeoutError, OSError):
        return "unsupported"
    finally:
        if not peer.done():
            peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)


async def _execute_soak(
    tolerances: SoakTolerances,
    *,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Run the bounded mixed soak workload on Linux and assemble its receipt."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("native soak qualification requires Linux procfs")

    import sysconfig

    from iperf3_lib import Client, ClientConfig, Server, ServerConfig, _execution
    from iperf3_lib.capabilities import get_capabilities
    from iperf3_lib.events import IntervalPayload, NativeDocumentPayload, NativeEndPayload

    if sys.implementation.name != "cpython" or sys.version_info.releaselevel != "final":
        raise RuntimeError("soak qualification requires a final CPython interpreter")
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise RuntimeError("free-threaded builds are outside the qualified soak envelope")
    if snapshot_proc_resources(os.getpid())["child_count"] != 0:
        raise RuntimeError(
            "soak qualification requires an isolated parent process with no pre-existing children"
        )

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("failed to enable PR_SET_CHILD_SUBREAPER for soak qualification")

    capabilities = get_capabilities(probe_native=True)
    native_version = capabilities.library.version or "unknown"
    sctp_support = await _probe_sctp_support()
    await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.02) for _ in range(4)))
    gc.collect()

    modes = (*BASE_MODES, "sctp-complete") if sctp_support == "supported" else BASE_MODES

    tracked_processes: list[subprocess.Popen[Any]] = []
    tracked_identities: dict[int, int] = {}
    lock = threading.Lock()
    real_popen = _execution.subprocess.Popen

    def _observe_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        proc = real_popen(*args, **kwargs)
        cmd = args[0] if args else kwargs.get("args")
        if cmd == _execution._worker_command():
            ident = _stat_identity((Path("/proc") / str(proc.pid) / "stat").read_bytes(), proc.pid)
            with lock:
                tracked_processes.append(proc)
                tracked_identities[proc.pid] = ident["start_ticks"]
        return proc

    def _verify_workers_reaped(procs: Sequence[subprocess.Popen[Any]]) -> bool:
        for proc in procs:
            if proc.returncode is None:
                return False
            if proc.stdin is not None and not proc.stdin.closed:
                return False
            if proc.stdout is not None and not proc.stdout.closed:
                return False
            ticks = tracked_identities.get(proc.pid)
            if ticks is not None and process_identity_matches(proc.pid, ticks):
                return False
        return True

    def _emergency_reap(procs: Sequence[subprocess.Popen[Any]]) -> None:
        waitpid = getattr(os, "waitpid", None)
        if waitpid is None:
            return
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
        snap = snapshot_proc_resources(os.getpid())
        for child in snap["children"]:
            if child["ppid"] == os.getpid() and process_identity_matches(
                child["pid"], child["start_ticks"]
            ):
                try:
                    os.kill(child["pid"], 9)
                    waitpid(child["pid"], 0)
                except (ProcessLookupError, ChildProcessError):
                    pass

    async def _settle_resources(reference_fds: int) -> dict[str, Any]:
        gc.collect()
        latest = snapshot_proc_resources(os.getpid())
        deadline = time.monotonic() + 3.0
        while (
            latest["child_count"] > 0 or latest["fd_count"] > reference_fds
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.03)
            gc.collect()
            latest = snapshot_proc_resources(os.getpid())
        return latest

    async def _same_port_reuse(
        port: int, protocol: str, prior_worker_ids: set[str]
    ) -> dict[str, Any]:
        reuse_protocol = "tcp" if protocol not in ("tcp", "udp", "sctp") else protocol
        server = Server(
            config=ServerConfig(bind_address="127.0.0.1", port=port, interval_seconds=0.25)
        )
        peer = asyncio.create_task(server.aserve_once(timeout=10))
        try:
            await _wait_until(
                lambda: port_has_tcp_listener(port) or peer.done(),
                f"reuse server did not listen on {port}",
            )
            if peer.done():
                await peer
                raise RuntimeError("reuse server exited before client run")
            cfg = ClientConfig(
                "127.0.0.1",
                port=port,
                protocol=reuse_protocol,
                duration=1,
                rate=1_000_000,
                interval_seconds=0.25,
                blksize=1200 if reuse_protocol == "udp" else None,
            )
            res = await Client(cfg).arun(timeout=10, on_event=lambda _ev: None)
            peer_res = await asyncio.wait_for(peer, timeout=8)
            byte_count = res.raw.get("end", {}).get("sum_received", {}).get("bytes", 0)
            worker_meta = res.extensions.get("iperf3_lib.worker")
            worker_id = worker_meta.get("worker_id") if isinstance(worker_meta, Mapping) else None
            distinct = isinstance(worker_id, str) and worker_id not in prior_worker_ids
            return {
                "ok": bool(res.ok and peer_res.ok and type(byte_count) is int and byte_count > 0),
                "protocol": reuse_protocol,
                "bytes": byte_count if type(byte_count) is int else 0,
                "worker_id": worker_id,
                "distinct_worker": distinct,
            }
        finally:
            if not peer.done():
                peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)

    async def _run_single_mode(
        index: int, mode: str, port: int
    ) -> tuple[str, str, bool, int, int, set[str]]:
        prior_worker_ids: set[str] = set()
        delivered_bytes = 0
        result_bytes = 0
        outcome = "unknown"
        outcome_matched = False
        protocol = "tcp"

        if mode == "native-failure":
            _assert_port_released(port)
            res = await Client(
                ClientConfig(
                    "127.0.0.1",
                    port=port,
                    protocol="tcp",
                    duration=1,
                    connect_timeout_ms=500,
                )
            ).arun(timeout=6, on_event=lambda _ev: None)
            worker_meta = res.extensions.get("iperf3_lib.worker")
            if isinstance(worker_meta, Mapping):
                wid = worker_meta.get("worker_id")
                if isinstance(wid, str):
                    prior_worker_ids.add(wid)
            if not res.ok and res.execution is not None and res.execution.status == "failed":
                outcome = "failed"
                outcome_matched = True
        elif mode == "udp-events-complete":
            protocol = "udp"
            server = Server(
                config=ServerConfig(bind_address="127.0.0.1", port=port, interval_seconds=0.2)
            )

            async def _consume_server() -> Any:
                async with server.events_once(timeout=10) as s_stream:
                    async for _ in s_stream:
                        pass
                    return await s_stream.result()

            peer = asyncio.create_task(_consume_server())
            try:
                await _wait_until(
                    lambda: port_has_tcp_listener(port) or peer.done(),
                    "UDP events server listener timeout",
                )
                if peer.done():
                    await peer
                    raise RuntimeError("UDP events server exited before client run")
                client = Client(
                    ClientConfig(
                        "127.0.0.1",
                        port=port,
                        protocol="udp",
                        duration=1,
                        rate=1_000_000,
                        blksize=1200,
                        interval_seconds=0.2,
                    )
                )
                async with client.events(timeout=10) as stream:
                    async for event in stream:
                        payload = event.payload
                        if isinstance(
                            payload, (IntervalPayload, NativeEndPayload, NativeDocumentPayload)
                        ):
                            delivered_bytes += sum(
                                m.bytes or 0 for m in payload.measurements if (m.bytes or 0) > 0
                            )
                    res = await stream.result()
                peer_res = await asyncio.wait_for(peer, timeout=8)
                result_bytes = res.raw.get("end", {}).get("sum_received", {}).get("bytes", 0)
                worker_meta = res.extensions.get("iperf3_lib.worker")
                if isinstance(worker_meta, Mapping):
                    wid = worker_meta.get("worker_id")
                    if isinstance(wid, str):
                        prior_worker_ids.add(wid)
                if (
                    res.ok
                    and peer_res.ok
                    and delivered_bytes > 0
                    and type(result_bytes) is int
                    and result_bytes > 0
                ):
                    outcome = "completed"
                    outcome_matched = True
            finally:
                if not peer.done():
                    peer.cancel()
                await asyncio.gather(peer, return_exceptions=True)
        elif mode in ("tcp-complete", "sctp-complete"):
            protocol = "sctp" if mode == "sctp-complete" else "tcp"
            direction_idx = index % 3
            reverse = protocol == "tcp" and direction_idx == 1
            bidir = protocol == "tcp" and direction_idx == 2
            server = Server(
                config=ServerConfig(bind_address="127.0.0.1", port=port, interval_seconds=0.25)
            )
            peer = asyncio.create_task(server.aserve_once(timeout=10))
            try:
                await _wait_until(
                    lambda: port_has_tcp_listener(port) or peer.done(),
                    f"{protocol} server listener timeout",
                )
                if peer.done():
                    await peer
                    raise RuntimeError(f"{protocol} server exited before client run")
                res = await Client(
                    ClientConfig(
                        "127.0.0.1",
                        port=port,
                        protocol=protocol,
                        duration=1,
                        rate=1_000_000,
                        reverse=reverse,
                        bidirectional=bidir,
                        interval_seconds=0.25,
                    )
                ).arun(timeout=10, on_event=lambda _ev: None)
                peer_res = await asyncio.wait_for(peer, timeout=8)
                end = res.raw.get("end", {})
                primary = end.get("sum_received") or end.get("sum_sent") or {}
                result_bytes = primary.get("bytes", 0) if isinstance(primary, Mapping) else 0
                worker_meta = res.extensions.get("iperf3_lib.worker")
                if isinstance(worker_meta, Mapping):
                    wid = worker_meta.get("worker_id")
                    if isinstance(wid, str):
                        prior_worker_ids.add(wid)
                if res.ok and peer_res.ok and type(result_bytes) is int and result_bytes > 0:
                    outcome = "completed"
                    outcome_matched = True
            finally:
                if not peer.done():
                    peer.cancel()
                await asyncio.gather(peer, return_exceptions=True)
        else:
            protocol = "udp" if mode == "udp-cancel" else "tcp"
            server = Server(
                config=ServerConfig(bind_address="127.0.0.1", port=port, interval_seconds=0.2)
            )
            peer = asyncio.create_task(server.aserve_once(timeout=12, on_event=lambda _ev: None))
            client_task: asyncio.Task[Any] | None = None
            try:
                await _wait_until(
                    lambda: port_has_tcp_listener(port) or peer.done(),
                    "interrupted cycle listener timeout",
                )
                if peer.done():
                    await peer
                    raise RuntimeError("interrupted cycle server exited before client run")
                loop = asyncio.get_running_loop()
                active = asyncio.Event()

                def _on_event(event: Any) -> None:
                    nonlocal delivered_bytes
                    count = _positive_interval_bytes(event.kind, event.data)
                    if count > 0:
                        delivered_bytes += count
                        loop.call_soon_threadsafe(active.set)

                client_timeout = 2.0 if mode == "worker-timeout" else 12.0
                client_task = asyncio.create_task(
                    Client(
                        ClientConfig(
                            "127.0.0.1",
                            port=port,
                            protocol=protocol,
                            duration=20,
                            rate=1_000_000,
                            interval_seconds=0.2,
                            blksize=1200 if protocol == "udp" else 4096,
                        )
                    ).arun(timeout=client_timeout, on_event=_on_event)
                )
                await asyncio.wait_for(active.wait(), timeout=6.0)
                if mode in ("async-cancel-tcp", "udp-cancel"):
                    client_task.cancel("soak cancellation")
                try:
                    await client_task
                except asyncio.CancelledError:
                    if mode in ("async-cancel-tcp", "udp-cancel") and delivered_bytes > 0:
                        outcome = "cancelled"
                        outcome_matched = True
                except TimeoutError:
                    if mode == "worker-timeout" and delivered_bytes > 0:
                        outcome = "timeout"
                        outcome_matched = True
                await asyncio.wait_for(peer, timeout=8.0)
            finally:
                tasks = [t for t in (client_task, peer) if t is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        return protocol, outcome, outcome_matched, delivered_bytes, result_bytes, prior_worker_ids

    started_monotonic = time.monotonic()
    try:
        with patch.object(_execution.subprocess, "Popen", _observe_popen):
            baseline = snapshot_proc_resources(os.getpid())
            baseline_fds = baseline["fd_count"]
            cycles: list[dict[str, Any]] = []

            for index in range(tolerances.total_cycles):
                cycle_start = time.monotonic()
                with lock:
                    tracked_processes.clear()
                    tracked_identities.clear()

                mode = modes[index % len(modes)]
                port = _reserve_loopback_port()
                (
                    protocol,
                    outcome,
                    outcome_matched,
                    delivered_bytes,
                    result_bytes,
                    prior_worker_ids,
                ) = await _run_single_mode(index, mode, port)

                with lock:
                    op_procs = list(tracked_processes)
                op_reaped = _verify_workers_reaped(op_procs)
                after_operation = await _settle_resources(baseline_fds)
                listener_after_op = port_has_tcp_listener(port)
                if not listener_after_op:
                    _assert_port_released(port)

                reuse_info = await _same_port_reuse(port, protocol, prior_worker_ids)
                with lock:
                    all_procs = list(tracked_processes)
                all_reaped = op_reaped and _verify_workers_reaped(all_procs)
                after_reuse = await _settle_resources(baseline_fds)
                listener_after_reuse = port_has_tcp_listener(port)
                if not listener_after_reuse:
                    _assert_port_released(port)
                _emergency_reap(all_procs)

                cycles.append(
                    {
                        "index": index,
                        "warmup": index < tolerances.warmup_cycles,
                        "mode": mode,
                        "protocol": protocol,
                        "port": port,
                        "outcome": outcome,
                        "outcome_matched": outcome_matched,
                        "delivered_bytes": delivered_bytes,
                        "result_bytes": result_bytes,
                        "workers_spawned": len(all_procs),
                        "workers_reaped": all_reaped,
                        "listener_active_after_operation": listener_after_op,
                        "listener_active_after_reuse": listener_after_reuse,
                        "after_operation": {
                            key: after_operation[key]
                            for key in (
                                "rss_kib",
                                "hwm_kib",
                                "fd_count",
                                "socket_fd_count",
                                "pipe_fd_count",
                                "child_count",
                            )
                        },
                        "after_reuse": {
                            key: after_reuse[key]
                            for key in (
                                "rss_kib",
                                "hwm_kib",
                                "fd_count",
                                "socket_fd_count",
                                "pipe_fd_count",
                                "child_count",
                            )
                        },
                        "reuse": reuse_info,
                        "elapsed_seconds": round(time.monotonic() - cycle_start, 4),
                    }
                )

            final = await _settle_resources(baseline_fds)
    finally:
        _emergency_reap(tracked_processes)

    compact_baseline = {
        key: baseline[key]
        for key in (
            "rss_kib",
            "hwm_kib",
            "fd_count",
            "socket_fd_count",
            "pipe_fd_count",
            "child_count",
        )
    }
    compact_final = {
        key: final[key]
        for key in (
            "rss_kib",
            "hwm_kib",
            "fd_count",
            "socket_fd_count",
            "pipe_fd_count",
            "child_count",
        )
    }
    evaluated = evaluate_soak_cycles(
        cycles,
        baseline=compact_baseline,
        final=compact_final,
        tolerances=tolerances,
    )
    return {
        "kind": SOAK_RECEIPT_KIND,
        "schema_version": SOAK_SCHEMA_VERSION,
        "status": "passed" if not evaluated["violations"] else "failed",
        "source_revision": source_revision,
        "package_version": importlib.metadata.version("iperf3-lib"),
        "runtime": {
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "native_version": native_version,
            "sctp_support": sctp_support,
        },
        "tolerances": asdict(tolerances),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 4),
        "baseline": compact_baseline,
        "final": compact_final,
        "summary": evaluated["summary"],
        "violations": evaluated["violations"],
        "cycles": cycles,
    }


def _write_atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute or verify a bounded mixed-workload soak qualification receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_cmd = subparsers.add_parser("run", help="Run mixed soak qualification and write receipt")
    run_cmd.add_argument("--output", type=Path, required=True)
    run_cmd.add_argument("--cycles", type=int, default=24)
    run_cmd.add_argument("--warmup-cycles", type=int, default=4)
    run_cmd.add_argument("--max-rss-growth-kib", type=int, default=16_384)
    run_cmd.add_argument("--max-rss-slope-kib", type=float, default=256.0)
    run_cmd.add_argument("--revision", default=None)
    run_cmd.add_argument("--expected-python", default=None)
    run_cmd.add_argument("--expected-native-version", default=None)

    verify_cmd = subparsers.add_parser("verify", help="Verify an existing soak JSON receipt")
    verify_cmd.add_argument("--receipt", type=Path, required=True)
    verify_cmd.add_argument("--revision", default=None)
    verify_cmd.add_argument("--expected-python", default=None)
    verify_cmd.add_argument("--expected-native-version", default=None)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "verify":
            receipt = load_and_validate_soak_receipt(
                args.receipt,
                expected_revision=args.revision,
                expected_python=args.expected_python,
                expected_native_version=args.expected_native_version,
            )
            summary = receipt["summary"]
            print(
                f"soak receipt verified: {summary['total_cycles']} cycles, "
                f"rss_growth={summary['rss_growth_kib']} KiB, "
                f"fd_delta={summary['final_fd_delta']}, "
                f"reuse={summary['reuse_passes']}/{summary['total_cycles']}"
            )
            return 0

        if args.revision is not None and re.fullmatch(r"[0-9a-f]{40}", args.revision) is None:
            raise ValueError("--revision must be a full 40-character lowercase commit SHA")
        tolerances = SoakTolerances(
            total_cycles=args.cycles,
            warmup_cycles=args.warmup_cycles,
            max_rss_growth_kib=args.max_rss_growth_kib,
            max_rss_slope_kib_per_cycle=args.max_rss_slope_kib,
        )
        args.output.unlink(missing_ok=True)
        receipt = asyncio.run(_execute_soak(tolerances, source_revision=args.revision))
        _write_atomic_json(args.output, receipt)
        validate_soak_receipt(
            receipt,
            expected_revision=args.revision,
            expected_python=args.expected_python,
            expected_native_version=args.expected_native_version,
        )
        summary = receipt["summary"]
        print(
            f"soak qualification passed: {summary['total_cycles']} cycles in "
            f"{receipt['elapsed_seconds']}s, rss_growth={summary['rss_growth_kib']} KiB, "
            f"slope={summary['rss_slope_kib_per_cycle']} KiB/cycle, "
            f"fd_delta={summary['final_fd_delta']} ({args.output})"
        )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        detail = str(error)
        message = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
        print(f"soak qualification failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
