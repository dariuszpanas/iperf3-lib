"""Fail-closed startup and real Linux creator-thread lifetime qualification."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from iperf3_lib import _execution, _worker_lifetime


@pytest.fixture
def linux_sigkill(monkeypatch):
    """Expose the Linux-only signal while exercising guard logic on Windows."""
    monkeypatch.setattr(_worker_lifetime.signal, "SIGKILL", 9, raising=False)


def test_parent_guard_arms_sigkill_before_checking_parent(monkeypatch, linux_sigkill):
    """The race check occurs after the kernel has armed process termination."""
    calls = []

    class Prctl:
        """Record the foreign call while allowing its ctypes signature."""

        def __call__(self, *args):
            calls.append(args)
            return 0

    def parent_pid():
        assert calls == [(1, signal.SIGKILL, 0, 0, 0)]
        return 123

    monkeypatch.setattr(_worker_lifetime.sys, "platform", "linux")
    monkeypatch.setattr(_worker_lifetime.os, "getppid", parent_pid)
    monkeypatch.setattr(
        _worker_lifetime.ctypes, "CDLL", lambda name, use_errno: SimpleNamespace(prctl=Prctl())
    )
    _worker_lifetime.protect_parent(123)


@pytest.mark.parametrize("failure", ["syscall", "parent_changed", "unsupported"])
def test_bootstrap_guard_failure_never_imports_the_package(
    monkeypatch, capsys, failure, linux_sigkill
):
    """A failed guard exits before package import or protocol identity is available."""

    class Prctl:
        """Model the kernel call result without changing this test process."""

        def __call__(self, *args):
            return -1 if failure == "syscall" else 0

    monkeypatch.setattr(_worker_lifetime.sys, "argv", ["bootstrap", "123"])
    monkeypatch.setattr(
        _worker_lifetime.sys, "platform", "win32" if failure == "unsupported" else "linux"
    )
    monkeypatch.setattr(_worker_lifetime.os, "getppid", lambda: 456)
    monkeypatch.setattr(
        _worker_lifetime.ctypes, "CDLL", lambda name, use_errno: SimpleNamespace(prctl=Prctl())
    )
    monkeypatch.setattr(_worker_lifetime.ctypes, "get_errno", lambda: 1)
    monkeypatch.setattr(
        _worker_lifetime.runpy, "run_module", lambda *a, **k: pytest.fail("package imported")
    )
    with pytest.raises(SystemExit) as captured:
        _worker_lifetime.main()
    assert captured.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    expected = {
        "syscall": "Cannot arm worker parent-death protection",
        "parent_changed": "Worker parent exited",
        "unsupported": "requires Linux",
    }
    assert expected[failure] in output.err


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_worker_launch_selects_linux_bootstrap_with_expected_parent(monkeypatch, platform):
    """Only qualified Linux workers acquire the guard before importing their package."""
    monkeypatch.setattr(_execution.sys, "platform", platform)
    command = _execution._worker_command()
    assert command[0] == sys.executable
    if platform == "linux":
        assert Path(command[1]).is_absolute()
        assert Path(command[1]).samefile(_worker_lifetime.__file__)
        assert command[2] == str(os.getpid())
    else:
        assert command[1:] == ["-m", "iperf3_lib._worker"]


GUARD_CHILD = textwrap.dedent(
    """
    import json, os, pathlib, runpy, sys, time
    bootstrap, expected, mode, release = sys.argv[1:]
    namespace = runpy.run_path(bootstrap)
    if mode == 'before_guard':
        os.write(1, (json.dumps({'before_guard': True}) + '\\n').encode())
        deadline = time.monotonic() + 5
        while not pathlib.Path(release).exists():
            assert time.monotonic() < deadline, 'startup barrier timed out'
            time.sleep(0.01)
    if mode == 'package_import':
        sys.path.insert(0, str(pathlib.Path(release).parent / 'package-root'))
        sys.argv = [bootstrap, expected]
        namespace['main']()
        raise AssertionError('blocked package import unexpectedly returned')
    try:
        namespace['protect_parent'](int(expected))
    except RuntimeError:
        raise SystemExit(23)
    os.write(1, (json.dumps({'armed': True}) + '\\n').encode())
    time.sleep(30)
    """
)

GUARD_PARENT = textwrap.dedent(
    """
    import json, os, pathlib, subprocess, sys, threading, time
    child_code, bootstrap, mode, release = sys.argv[1:]
    children = []
    def launch():
        child = subprocess.Popen([
            sys.executable, '-u', '-c', child_code, bootstrap,
            str(os.getpid()), mode, release,
        ])
        children.append(child)
        os.write(1, (json.dumps({'worker_pid': child.pid}) + '\\n').encode())
        if mode == 'creator_thread':
            deadline = time.monotonic() + 5
            while not pathlib.Path(release).exists():
                assert time.monotonic() < deadline, 'thread-exit barrier timed out'
                time.sleep(0.01)
    if mode == 'creator_thread':
        thread = threading.Thread(target=launch)
        thread.start()
        thread.join()
        outcome = {'worker_returncode': children[0].wait(timeout=5)}
        os.write(1, (json.dumps(outcome) + '\\n').encode())
    else:
        launch()
    time.sleep(30)
    """
)

GUARD_SUPERVISOR = textwrap.dedent(
    """
    import ctypes, json, os, pathlib, signal, subprocess, sys, time
    parent_code, child_code, bootstrap, mode, release = sys.argv[1:]
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    parent = subprocess.Popen([
        sys.executable, '-u', '-c', parent_code, child_code, bootstrap, mode, release,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.set_blocking(parent.stdout.fileno(), False)
    pending = b''
    evidence = {}
    worker_pid = None
    def until(key):
        global pending, worker_pid
        deadline = time.monotonic() + 5
        while key not in evidence and time.monotonic() < deadline:
            try:
                pending += os.read(parent.stdout.fileno(), 65536)
            except BlockingIOError:
                pass
            while b'\\n' in pending:
                line, pending = pending.split(b'\\n', 1)
                evidence.update(json.loads(line))
                worker_pid = evidence.get('worker_pid')
            time.sleep(0.01)
        assert key in evidence, evidence
    try:
        until('worker_pid')
        until('before_guard' if mode == 'before_guard' else 'armed')
        if mode == 'creator_thread':
            pathlib.Path(release).touch()
            until('worker_returncode')
            assert evidence['worker_returncode'] == -signal.SIGKILL
            assert parent.poll() is None
            worker_pid = None
        else:
            parent.kill()
            parent.wait(timeout=5)
            pathlib.Path(release).touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                reaped, status = os.waitpid(worker_pid, os.WNOHANG)
                if reaped:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError('worker was not reaped')
            worker_pid = None
            if mode == 'before_guard':
                assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 23
            else:
                assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        print(json.dumps({'mode': mode, 'qualified': True}))
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if os.waitpid(worker_pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError('failed-test worker did not reap')
            except (ProcessLookupError, ChildProcessError):
                pass
        parent.stdout.close()
        parent.stderr.close()
    """
)


def test_guard_scenario_scripts_compile():
    """Keep the externally bounded Linux programs checked on every platform."""
    for source in (GUARD_CHILD, GUARD_PARENT, GUARD_SUPERVISOR):
        compile(source, "<parent-lifetime-qualification>", "exec")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel parent-death semantics")
@pytest.mark.parametrize(
    "mode", ["before_guard", "after_guard", "creator_thread", "package_import"]
)
def test_linux_guard_handles_startup_and_creator_thread_exit(tmp_path, mode):
    """Real processes prove startup refusal, SIGKILL, and thread ownership semantics."""
    if mode == "package_import":
        package = tmp_path / "package-root" / "iperf3_lib"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "import os, time\nos.write(1, b'{\"armed\": true}\\n')\ntime.sleep(30)\n"
        )
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            GUARD_SUPERVISOR,
            GUARD_PARENT,
            GUARD_CHILD,
            str(Path(_worker_lifetime.__file__).resolve()),
            mode,
            str(tmp_path / "release"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as scenario:
        try:
            output, errors = scenario.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(scenario.pid, signal.SIGKILL)
            scenario.communicate(timeout=5)
            raise
    assert scenario.returncode == 0, output + errors
    assert json.loads(output) == {"mode": mode, "qualified": True}
