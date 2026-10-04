"""Deterministic procfs fixtures for non-reaping native resource inventories."""

import os
import runpy
import subprocess
from pathlib import Path

import _native_resource_inventory as inventory
import pytest


def _stat(pid, *, ppid=1, state="S", start_ticks=500, comm=b"worker"):
    fields = [state.encode(), str(ppid).encode(), *([b"0"] * 17), str(start_ticks).encode()]
    return str(pid).encode() + b" (" + comm + b") " + b" ".join(fields) + b"\n"


@pytest.fixture
def proc(tmp_path, monkeypatch):
    """Provide portable procfs files with mocked symlink reads on every platform."""
    root = tmp_path / "proc"
    root.mkdir()
    targets = {}

    def readlink(path):
        try:
            return targets[Path(path)]
        except KeyError as error:
            raise FileNotFoundError(str(path)) from error

    monkeypatch.setattr(inventory.os, "readlink", readlink)

    def process(
        pid=100, *, ppid=1, state="S", start_ticks=500, comm=b"worker", threads=None, fds=None
    ):
        directory = root / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "stat").write_bytes(
            _stat(pid, ppid=ppid, state=state, start_ticks=start_ticks, comm=comm)
        )
        (directory / "task").mkdir(exist_ok=True)
        for tid, children in (threads if threads is not None else {pid: []}).items():
            task = directory / "task" / str(tid)
            task.mkdir(exist_ok=True)
            (task / "children").write_text(" ".join(map(str, children)))
        (directory / "fd").mkdir(exist_ok=True)
        for number, target in (fds or {}).items():
            path = directory / "fd" / str(number)
            path.touch()
            targets[path] = target
        return directory

    return root, process, targets


@pytest.mark.parametrize(
    "comm", [b"name with spaces", b"left (middle) right))", b") ( )", b"bad\xffutf8\nname"]
)
def test_stat_identity_parses_arbitrary_comm_without_moving_numeric_fields(proc, comm):
    """The final comm delimiter separates state, PPID, and start ticks reliably."""
    root, process, _ = proc
    process(100, ppid=42, state="R", start_ticks=987654321, comm=comm)
    assert inventory.process_identity(100, proc_root=root) == {
        "pid": 100,
        "ppid": 42,
        "state": "R",
        "start_ticks": 987654321,
    }


def test_snapshot_collects_children_from_every_thread_and_socket_targets(proc, monkeypatch):
    """Worker-thread children are retained with their individual process birth identities."""
    root, process, _ = proc
    process(
        100,
        threads={100: [300], 101: [200]},
        fds={
            0: "/dev/null",
            4: "socket:[901]",
            6: "pipe:[902]",
            8: "anon_inode:[eventpoll]",
            9: "/tmp/socket:[903]",
            10: "socket:[not-inode]",
        },
    )
    process(200, ppid=100, start_ticks=602)
    process(300, ppid=100, state="Z", start_ticks=603)

    def forbidden(*args, **kwargs):
        pytest.fail("inventory must not signal, poll, wait, or reap")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "waitpid", forbidden)
    monkeypatch.setattr(subprocess.Popen, "poll", forbidden)
    monkeypatch.setattr(subprocess.Popen, "wait", forbidden)
    result = inventory.snapshot_process(100, proc_root=root)
    assert result["thread_children"] == {"100": [300], "101": [200]}
    assert result["children"] == [
        {"pid": 200, "ppid": 100, "state": "S", "start_ticks": 602},
        {"pid": 300, "ppid": 100, "state": "Z", "start_ticks": 603},
    ]
    assert result["fds"] == {
        "0": "/dev/null",
        "4": "socket:[901]",
        "6": "pipe:[902]",
        "8": "anon_inode:[eventpoll]",
        "9": "/tmp/socket:[903]",
        "10": "socket:[not-inode]",
    }
    assert result["socket_fds"] == {"4": "socket:[901]"}


def test_identity_matching_distinguishes_pid_reuse_and_unreaped_zombie(proc):
    """A zombie remains owned; only disappearance or different birth ticks means gone."""
    root, process, _ = proc
    directory = process(100, state="Z", start_ticks=11)
    assert inventory.process_identity_matches(100, 11, proc_root=root)
    (directory / "stat").write_bytes(_stat(100, start_ticks=12))
    assert not inventory.process_identity_matches(100, 11, proc_root=root)
    assert inventory.process_identity_matches(100, 12, proc_root=root)
    (directory / "stat").unlink()
    assert not inventory.process_identity_matches(100, 12, proc_root=root)
    with pytest.raises(inventory.ProcessGoneError):
        inventory.snapshot_process(100, proc_root=root)


def test_missing_process_is_not_an_empty_live_inventory(proc):
    """An absent PID cannot manufacture zero descriptors and zero children."""
    root, _, _ = proc
    with pytest.raises(inventory.ProcessGoneError):
        inventory.snapshot_process(404, proc_root=root)
    assert not inventory.process_identity_matches(404, 1, proc_root=root)


def test_missing_fd_directory_is_not_an_empty_descriptor_inventory(proc):
    """An unreadable FD directory cannot turn a still-present process into an empty sample."""
    root, process, _ = proc
    directory = process(100)
    (directory / "fd").rmdir()
    with pytest.raises(inventory.InventoryRaceError, match="did not stabilize after 2"):
        inventory.snapshot_process(100, proc_root=root, attempts=2)


def test_permanently_stale_child_list_has_bounded_retries(proc, monkeypatch):
    """An unresolved child identity fails after the requested number of observations."""
    root, process, _ = proc
    process(100, threads={100: [200]})
    original = inventory.process_identity
    observations = []

    def identity(pid, **kwargs):
        if pid == 200:
            observations.append(pid)
        return original(pid, **kwargs)

    monkeypatch.setattr(inventory, "process_identity", identity)
    with pytest.raises(inventory.InventoryRaceError, match="did not stabilize after 2"):
        inventory.snapshot_process(100, proc_root=root, attempts=2)
    assert observations == [200, 200]


def test_closed_fd_readlink_race_omits_only_that_descriptor(proc):
    """Self-inventory tolerates the short-lived FD opened by listing its FD directory."""
    root, process, _ = proc
    directory = process(100, fds={0: "/dev/null", 4: "socket:[700]"})
    (directory / "fd" / "7").touch()
    result = inventory.snapshot_process(100, proc_root=root)
    assert result["fds"] == {"0": "/dev/null", "4": "socket:[700]"}
    assert result["socket_fds"] == {"4": "socket:[700]"}
    assert result["identity"]["pid"] == 100


def test_process_disappearance_during_fd_read_does_not_return_live_snapshot(proc, monkeypatch):
    """Ignoring a vanished descriptor must still detect disappearance of its whole process."""
    root, process, _ = proc
    directory = process(100, fds={4: "socket:[700]"})

    def readlink(path):
        (directory / "stat").unlink(missing_ok=True)
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(inventory.os, "readlink", readlink)
    with pytest.raises(inventory.ProcessGoneError):
        inventory.snapshot_process(100, proc_root=root)


def test_reused_parent_pid_during_snapshot_is_not_silently_retargeted(proc, monkeypatch):
    """A replacement process with the same PID cannot inherit the original inventory."""
    root, process, _ = proc
    directory = process(100, start_ticks=1000, fds={4: "socket:[700]"})
    real_readlink = inventory.os.readlink

    def readlink(path):
        (directory / "stat").write_bytes(_stat(100, start_ticks=2000))
        return real_readlink(path)

    monkeypatch.setattr(inventory.os, "readlink", readlink)
    with pytest.raises(inventory.InventoryRaceError, match="replaced"):
        inventory.snapshot_process(100, proc_root=root)


def test_exiting_child_retries_and_observes_updated_thread_children(proc, monkeypatch):
    """A child exiting between the children list and stat read is retried, not fabricated."""
    root, process, _ = proc
    directory = process(100, threads={100: [], 101: [200]})
    child = process(200, ppid=100, start_ticks=600)
    real_identity = inventory.process_identity
    reads = []

    def identity(pid, **kwargs):
        if pid == 200:
            reads.append(pid)
            (child / "stat").unlink()
            (directory / "task" / "101" / "children").write_text("")
        return real_identity(pid, **kwargs)

    monkeypatch.setattr(inventory, "process_identity", identity)
    result = inventory.snapshot_process(100, proc_root=root)
    assert reads == [200]
    assert result["children"] == []
    assert result["thread_children"] == {"100": [], "101": []}


def test_exiting_thread_retries_inventory_instead_of_losing_its_children(proc, monkeypatch):
    """A task disappearing during enumeration requires a fresh all-thread sample."""
    root, process, _ = proc
    directory = process(100, threads={100: [], 101: []})
    disappearing = directory / "task" / "101" / "children"
    original_read = Path.read_bytes
    observations = []

    def read_bytes(path):
        if path == disappearing:
            observations.append(True)
            path.unlink()
            path.parent.rmdir()
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    result = inventory.snapshot_process(100, proc_root=root)
    assert observations == [True]
    assert result["thread_children"] == {"100": []}


def test_changed_child_parent_does_not_yield_wrong_owned_child(proc):
    """A stale children entry cannot attribute an adopted process to the former parent."""
    root, process, _ = proc
    process(100, threads={100: [200]})
    process(200, ppid=999)
    with pytest.raises(inventory.InventoryRaceError, match="did not stabilize after 2"):
        inventory.snapshot_process(100, proc_root=root, attempts=2)


def test_child_pid_reuse_is_retried_with_new_birth_identity(proc, monkeypatch):
    """A child replaced during a sample contributes only its newly verified lifetime."""
    root, process, _ = proc
    directory = process(100, threads={100: [200]}, fds={4: "socket:[700]"})
    child = process(200, ppid=100, start_ticks=600)
    original_readlink = inventory.os.readlink
    observations = []

    def readlink(path):
        observations.append(True)
        (child / "stat").write_bytes(_stat(200, ppid=100, start_ticks=700))
        return original_readlink(path)

    monkeypatch.setattr(inventory.os, "readlink", readlink)
    result = inventory.snapshot_process(100, proc_root=root)
    assert len(observations) == 2
    assert result["children"] == [{"pid": 200, "ppid": 100, "state": "S", "start_ticks": 700}]
    assert (directory / "stat").exists()


@pytest.mark.parametrize("access", ["stat", "fd"])
def test_permission_denial_is_not_cleanup_evidence(proc, monkeypatch, access):
    """Unreadable process state fails inspection instead of claiming missing resources."""
    root, process, _ = proc
    directory = process(100, fds={4: "socket:[700]"})

    if access == "stat":
        original = Path.read_bytes

        def denied(path):
            if path == directory / "stat":
                raise PermissionError("stat denied")
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", denied)
        with pytest.raises(PermissionError):
            inventory.process_identity_matches(100, 500, proc_root=root)
    else:

        def denied(path):
            raise PermissionError("fd denied")

        monkeypatch.setattr(inventory.os, "readlink", denied)
    with pytest.raises(PermissionError):
        inventory.snapshot_process(100, proc_root=root)


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"100 no comm",
        b"100 (broken) R 1",
        _stat(101),
        _stat(100, state="running"),
        _stat(100, start_ticks=-1),
    ],
)
def test_invalid_stat_never_becomes_missing_process(proc, contents):
    """Malformed identity is an explicit inspection failure, not proof of process exit."""
    root, process, _ = proc
    directory = process(100)
    (directory / "stat").write_bytes(contents)
    with pytest.raises(inventory.InvalidProcDataError):
        inventory.process_identity_matches(100, 500, proc_root=root)


@pytest.mark.parametrize("children", ["0", "200 200", "200 malformed", "-1"])
def test_invalid_children_are_not_silently_discarded(proc, children):
    """Corrupted per-thread ownership data cannot make a no-descendant check pass."""
    root, process, _ = proc
    directory = process(100)
    (directory / "task" / "100" / "children").write_text(children)
    with pytest.raises(inventory.InvalidProcDataError):
        inventory.snapshot_process(100, proc_root=root)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_invalid_attempt_count_is_rejected(proc, value):
    """Retries require an explicit positive integer bound."""
    root, process, _ = proc
    process(100)
    with pytest.raises(ValueError):
        inventory.snapshot_process(100, proc_root=root, attempts=value)


@pytest.mark.parametrize("value", [0, -1, True, "100"])
def test_invalid_pid_is_rejected_before_procfs_access(proc, value):
    """PID lookup cannot silently coerce booleans, strings, or nonpositive values."""
    root, _, _ = proc
    with pytest.raises(ValueError):
        inventory.process_identity(value, proc_root=root)


def test_helper_loads_as_standalone_stdlib_file(proc):
    """The retained installed harness needs no iperf3-lib or test-package imports."""
    root, process, _ = proc
    process(100)
    namespace = runpy.run_path(inventory.__file__)
    snapshot = namespace["snapshot_process"](100, proc_root=root)
    assert snapshot["identity"]["pid"] == 100
    assert snapshot["children"] == []
