"""Pytest configuration and fixtures for iperf3-lib integration and unit tests."""

import os
import shutil
import socket
import subprocess
import time

import pytest


def _port_for_group(group: str | None) -> int:
    """Return a stable port number for a given group name or None."""
    base = int(os.getenv("IPERF3_BASE_PORT", "5201"))
    if not group:
        return base
    idx = abs(hash(group)) % 2000
    port = base + idx
    if port < 1024:
        port += 1024
    if port > 60000:
        port = base
    return port


@pytest.fixture(scope="session")
def iperf3_server(request):
    """Start a system iperf3 server as a subprocess and yield (host, port).

    This fixture uses the `iperf3` executable on PATH. It starts a background
    server process and waits for the port to accept connections. On teardown
    the process is terminated. This is deliberately simple and avoids the
    complexity of embedding the server via the lib API inside the test process.
    """
    iperf_exe = shutil.which("iperf3")
    if not iperf_exe:
        pytest.skip("iperf3 executable not found on PATH; skipping integration tests")

    host = os.getenv("IPERF3_HOST", "127.0.0.1")
    port_env = os.getenv("IPERF3_PORT")

    # allow xdist group marker to pick a per-group port if present
    marker = None
    try:
        marker = request.node.get_closest_marker("xdist_group")
    except Exception:
        # request.node may not be available at session-scope in some pytest versions
        marker = None
    group = None
    if marker:
        if marker.args:
            group = marker.args[0]
        elif marker.kwargs:
            group = marker.kwargs.get("name")

    if port_env:
        port = int(port_env)
    else:
        # derive a stable port (per-group or base) so multiple groups don't clash
        port = _port_for_group(group)

    # Ensure the FFI and client modules are reloaded so any previous unit
    # test that reloaded the client with a dummy lib does not leave a
    # client module referencing a mocked lib. Reloading here restores the
    # real cffi-backed `lib` object before we start integration servers.
    try:
        import importlib

        import iperf3_lib.ffi.api as api_mod
        import iperf3_lib.iperf_client as client_mod

        importlib.reload(api_mod)
        importlib.reload(client_mod)
    except Exception:
        # best-effort; proceed even if reload fails
        pass

    # launch iperf3 server (foreground) in background
    # avoid -D (daemon) so we can keep process handle and stop it cleanly
    args = [iperf_exe, "-s", "-p", str(port)]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # wait for port readiness
    start = time.time()
    ready = False
    while time.time() - start < 5.0:
        if proc.poll() is not None and proc.returncode != 0:
            break
        try:
            with socket.create_connection((host, port), timeout=0.5):
                ready = True
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)

    if not ready:
        try:
            proc.terminate()
        except Exception:
            pass
        proc.wait(timeout=1)
        pytest.skip("system iperf3 server failed to start; skipping integration tests")

    try:
        yield host, port
    finally:
        # terminate the server process on teardown
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# Keep collection-time enforcement so tests explicitly mark integration
def pytest_collection_modifyitems(config, items):
    """Enforce that tests using the iperf3_server fixture are marked as integration."""
    offenders = []
    for item in items:
        if "iperf3_server" in getattr(item, "fixturenames", []):
            if not item.get_closest_marker("integration"):
                offenders.append(item.nodeid)
    if offenders:
        msg = (
            "The following tests use the 'iperf3_server' fixture but are not "
            "marked with @pytest.mark.integration:\n" + "\n".join(offenders)
        )
        pytest.exit(msg)


@pytest.fixture(scope="session")
def libiperf_symbols():
    """Return a set of exported symbol names from /usr/local/lib/libiperf.so.

    This uses the `nm` tool inside the test container to deterministically
    discover which optional setters are exported by the built lib. If `nm`
    or the shared object is not available, return an empty set.
    """
    import shutil
    import subprocess

    path = "/usr/local/lib/libiperf.so"
    symbols = set()
    nm = shutil.which("nm")
    if nm and os.path.exists(path):
        try:
            p = subprocess.run(
                [nm, "-D", "--defined-only", path], capture_output=True, text=True, check=False
            )
            if p.returncode == 0:
                for line in p.stdout.splitlines():
                    parts = line.strip().split()
                    if parts:
                        # symbol name is last column
                        symbols.add(parts[-1])
        except Exception:
            pass
    return symbols


@pytest.fixture(autouse=True)
def reload_ffi_and_client_each_test():
    """Reload the FFI and client modules before each test.

    Some unit tests intentionally reload the client module with dummy ffi/lib
    implementations. If they don't fully restore state, later integration
    tests may pick up a mocked client. Reloading the authoritative modules
    before each test makes the test environment deterministic.
    """
    try:
        import importlib

        import iperf3_lib.ffi.api as api_mod
        import iperf3_lib.iperf_client as client_mod

        importlib.reload(api_mod)
        importlib.reload(client_mod)
    except Exception:
        # best-effort; don't break tests if reload fails
        pass
    yield
    # no teardown; each test starts with fresh reload
