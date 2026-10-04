"""Read-only Linux process inventories for standalone installed qualification.

Snapshots sample procfs; they do not freeze processes or supervise descendants.
No helper polls a Popen object, signals a process, or consumes a child exit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class ProcessGoneError(ProcessLookupError):
    """The requested process disappeared while its procfs state was observed."""


class InventoryRaceError(RuntimeError):
    """Process identity or child ownership changed during a bounded snapshot."""


class InvalidProcDataError(ValueError):
    """A procfs record could not supply an unambiguous process identity."""


def _integer(value: int, name: str, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _decimal(value: bytes, name: str) -> int:
    if not value.isdigit():
        raise InvalidProcDataError(f"Invalid {name} in procfs record")
    try:
        return int(value)
    except ValueError as error:
        raise InvalidProcDataError(f"Invalid {name} in procfs record") from error


def _stat_identity(data: bytes, pid: int) -> dict:
    # comm is an arbitrary byte string, can contain spaces and ')' characters,
    # and need not be UTF-8. All fields after its final ')' are ASCII tokens.
    try:
        heading, tail = data.rsplit(b")", 1)
        number, separator, _ = heading.partition(b"(")
        fields = tail.split()
        if not separator or _decimal(number.strip(), "pid") != pid or len(fields) < 20:
            raise InvalidProcDataError("Invalid process stat layout")
        state = fields[0].decode("ascii")
        if len(state) != 1:
            raise InvalidProcDataError("Invalid process state")
        return {
            "pid": pid,
            "ppid": _decimal(fields[1], "ppid"),
            "state": state,
            "start_ticks": _decimal(fields[19], "start ticks"),
        }
    except (ValueError, UnicodeError) as error:
        if isinstance(error, InvalidProcDataError):
            raise
        raise InvalidProcDataError("Invalid process stat layout") from error


def process_identity(pid: int, *, proc_root: str | os.PathLike = "/proc") -> dict:
    """Read PID, process parent, state, and kernel birth ticks without reaping."""
    _integer(pid, "pid", minimum=1)
    try:
        data = (Path(proc_root) / str(pid) / "stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError) as error:
        raise ProcessGoneError(f"Process {pid} is absent from procfs") from error
    return _stat_identity(data, pid)


def process_identity_matches(
    pid: int, start_ticks: int, *, proc_root: str | os.PathLike = "/proc"
) -> bool:
    """Check one process lifetime; disappearance or PID reuse returns False.

    Zombies still match until their procfs entry disappears. Permission errors,
    invalid records, and other inspection failures are not evidence of cleanup.
    """
    _integer(start_ticks, "start_ticks", minimum=0)
    try:
        identity = process_identity(pid, proc_root=proc_root)
    except ProcessGoneError:
        return False
    return identity["start_ticks"] == start_ticks


def _same_lifetime(first: dict, second: dict) -> bool:
    return first["pid"] == second["pid"] and first["start_ticks"] == second["start_ticks"]


def _thread_children(directory: Path) -> dict[str, list[int]]:
    task = directory / "task"
    tids = sorted((name for name in os.listdir(task) if name.isascii() and name.isdigit()), key=int)
    if not tids:
        raise InventoryRaceError("Process has no observable threads")
    result = {}
    for tid in tids:
        children = (task / tid / "children").read_bytes().split()
        values = [_decimal(child, "child pid") for child in children]
        if any(child < 1 for child in values) or len(set(values)) != len(values):
            raise InvalidProcDataError("Invalid per-thread child PID list")
        result[tid] = sorted(values)
    after = sorted(
        (name for name in os.listdir(task) if name.isascii() and name.isdigit()), key=int
    )
    if tids != after:
        raise InventoryRaceError("Process thread set changed while reading children")
    return result


def _file_descriptors(directory: Path) -> dict[str, str]:
    fd_directory = directory / "fd"
    numbers = sorted(
        (name for name in os.listdir(fd_directory) if name.isascii() and name.isdigit()), key=int
    )
    result = {}
    for number in numbers:
        try:
            result[number] = os.readlink(fd_directory / number)
        except (FileNotFoundError, ProcessLookupError):
            # A descriptor can close after listing. In /proc/self/fd this also
            # includes the temporary directory descriptor used by listdir.
            # The caller rechecks process identity before returning a snapshot.
            continue
    return result


def snapshot_process(
    pid: int, *, proc_root: str | os.PathLike = "/proc", attempts: int = 3
) -> dict:
    """Sample identity, all-thread direct children, and descriptor/socket targets.

    Each child's birth ticks are retained. A disappearing thread or child causes
    a bounded retry; a different process reusing the requested PID is rejected.
    Transient closed FD links are omitted, then process existence is checked
    again. FD inventories are observed targets, not an atomic kernel snapshot.
    """
    _integer(attempts, "attempts", minimum=1)
    root = Path(proc_root)
    initial = process_identity(pid, proc_root=root)
    directory = root / str(pid)
    last_error = None
    for _ in range(attempts):
        before = process_identity(pid, proc_root=root)
        if not _same_lifetime(initial, before):
            raise InventoryRaceError(f"Process {pid} was replaced during its inventory")
        try:
            thread_children = _thread_children(directory)
            children = {}
            for child_pid in sorted(
                {child for values in thread_children.values() for child in values}
            ):
                child = process_identity(child_pid, proc_root=root)
                if child["ppid"] != pid:
                    raise InventoryRaceError("Child changed process parent during inventory")
                children[child_pid] = child
            descriptors = _file_descriptors(directory)
            if _thread_children(directory) != thread_children:
                raise InventoryRaceError("Process children changed during inventory")
            final_children = []
            for child_pid, child in children.items():
                current = process_identity(child_pid, proc_root=root)
                if not _same_lifetime(child, current) or current["ppid"] != pid:
                    raise InventoryRaceError("Child identity changed during inventory")
                final_children.append(current)
            after = process_identity(pid, proc_root=root)
            if not _same_lifetime(initial, after):
                raise InventoryRaceError(f"Process {pid} was replaced during its inventory")
            return {
                "identity": after,
                "thread_children": thread_children,
                "children": final_children,
                "fds": descriptors,
                "socket_fds": {
                    number: target
                    for number, target in descriptors.items()
                    if re.fullmatch(r"socket:\[[0-9]+\]", target)
                },
            }
        except (FileNotFoundError, ProcessLookupError, InventoryRaceError) as error:
            # Missing task/FD/child entries must never hide a missing or reused
            # requested process. Inspection permission errors are not retried.
            current = process_identity(pid, proc_root=root)
            if not _same_lifetime(initial, current):
                raise InventoryRaceError(
                    f"Process {pid} was replaced during its inventory"
                ) from error
            last_error = error
    raise InventoryRaceError(
        f"Process {pid} inventory did not stabilize after {attempts} attempts"
    ) from last_error
