"""Bounded real-listener qualification for the Python libiperf server wrapper."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import textwrap

import pytest

SCENARIO = textwrap.dedent(
    r"""
    import concurrent.futures
    import dataclasses
    import json
    import pathlib
    import socket
    import sys
    import time

    from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact
    from iperf3_lib.config import ClientConfig
    from iperf3_lib.iperf_client import Client
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.server_config import ServerConfig

    case = json.loads(sys.argv[1])
    host = case.get("host", "127.0.0.1")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]

    cfg = ServerConfig(
        port=port, bind_address=host,
        address_family="ipv6" if family == socket.AF_INET6 else "ipv4",
        **case.get("server", {}),
    )
    server = Server(config=cfg)
    results = []
    events = []
    on_event = events.append if cfg.json_stream else None

    def retain(result):
        results.append(result)
        if case.get("stop_after_first"):
            server.stop()

    def listener_ready(future):
        # Reading the kernel listener table avoids a probe connection consuming
        # the one-shot server's only native run.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if future.done():
                future.result()
                raise AssertionError("server ended before listening")
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                path = pathlib.Path(table)
                if not path.exists():
                    continue
                for line in path.read_text().splitlines()[1:]:
                    columns = line.split()
                    if columns[3] == "0A" and int(columns[1].split(":")[1], 16) == port:
                        return
            time.sleep(0.02)
        raise AssertionError("server listener did not appear")

    mode = case.get("mode", "once")
    if mode == "idle":
        result = server.run_once(timeout=6)
        assert not result.ok
        assert result.execution.status == "incomplete"
        assert result.reporting_role == "server"
        assert result.error
        print(json.dumps({"idle": True}))
        sys.exit(0)
    if mode == "timeout":
        try:
            server.run_once(timeout=0.5)
        except TimeoutError:
            print(json.dumps({"timeout": True}))
            sys.exit(0)
        raise AssertionError("unconnected listener was not bounded")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        if mode == "loop":
            future = pool.submit(
                server.serve_forever, on_result=retain, on_event=on_event,
                max_runs=case.get("max_runs", 2), timeout=15,
            )
        else:
            future = pool.submit(server.run_once, on_event=on_event, timeout=15)
        listener_ready(future)
        clients = []
        for reverse in case.get("reverse", [False]):
            client_options = dict(server=host, port=port, duration=1, rate=500_000, reverse=reverse)
            client_options.update(case.get("client", {}))
            result = Client(ClientConfig(**client_options), password=case.get("password")).run(timeout=10)
            if case.get("expect_failure"):
                assert not result.ok, "server policy unexpectedly accepted the client"
            else:
                assert result.ok, result.error
            clients.append(result)
            if mode == "loop" and len(clients) < len(case["reverse"]):
                deadline = time.monotonic() + 4
                while len(results) < len(clients) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert len(results) == len(clients)
        completed = future.result(timeout=17)
        if mode != "loop":
            results.append(completed)

    if case.get("expect_idle_after_success"):
        assert len(results) == len(clients) + 1
        idle = results.pop()
        assert not idle.ok
        assert idle.execution.status == "incomplete"
        assert idle.reporting_role == "server"
        assert idle.error
        assert idle.raw == {}, "idle iteration reused a previous native JSON document"
        assert loads_artifact(dumps_artifact(artifact_from_result(idle))).result == idle
    assert len(results) == len(clients)
    for result, client in zip(results, clients, strict=True):
        assert result.reporting_role == "server"
        expected_options = json.loads(json.dumps(dataclasses.asdict(cfg)))
        assert result.extensions["iperf3_lib.server_config"] == expected_options
        archived = dumps_artifact(artifact_from_result(result))
        assert loads_artifact(archived).result == result
        if case.get("password"):
            assert case["password"] not in archived
            assert case["password"] not in dumps_artifact(artifact_from_result(client))
        if case.get("expect_failure"):
            assert not result.ok
            assert result.execution.status == "failed"
            if case["failure_match"] not in result.error.lower():
                # Native authentication rejection can clear server i_errno;
                # retain the failure without inventing a cause absent from JSON.
                assert case["failure_match"] in client.error.lower() or client.error == "control socket has closed unexpectedly", client.error
                status = result.extensions["iperf3_lib.native_status"]
                assert status["returncode"] < 0 and status["error_code"] == 0
                assert any(d.code == "execution.native_error_detail_missing" for d in result.diagnostics)
            continue
        assert result.ok, result.error
        assert result.execution.status == "completed"
        assert result.raw["start"]["test_start"]["reverse"] == (client.execution.method == "reverse")
        assert result.raw["start"]["test_start"]["target_bitrate"] == case.get("client", {}).get("rate", 500_000)
        assert result.flows
        assert all(item["local_host"] == host for item in result.raw["start"]["connected"])
        evidence = result.extensions["iperf3_lib.native_configuration"]
        assert evidence["bind_address"]["value"] == host
        if cfg.bind_device:
            assert evidence["bind_device"]["value"] == cfg.bind_device
        if cfg.extra_data:
            assert evidence["extra_data"]["value"] == cfg.extra_data
            if not cfg.json_stream:
                assert result.raw["extra_data"] == cfg.extra_data
        assert evidence["interval_seconds"]["value"] == cfg.interval_seconds
    if case.get("server", {}).get("json_stream"):
        assert events, "streaming enabled but no native events delivered"
        assert all(event.sequence >= 0 for event in events)
        assert all(event.kind for event in events)
    # A successful operation also releases its native listener and worker.
    with socket.socket(family, socket.SOCK_STREAM) as released:
        released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        released.bind((host, port))
    print(json.dumps({
        "runs": len(results),
        "reverse": [result.execution.method == "reverse" for result in results],
        "events": len(events),
        "failed": [not result.ok for result in results],
        "idle_after_success": bool(case.get("expect_idle_after_success")),
    }))
    """
)


def _run_case(case):
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO, json.dumps(case)],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def test_server_native_scenario_script_compiles():
    """Catch errors in the bounded subprocess harness without native execution."""
    compile(SCENARIO, "<server-listener-scenario>", "exec")


@pytest.mark.integration
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_server_binds_requested_address_and_returns_real_result(host):
    """Verify listener reachability and returned native local-address evidence."""
    assert _run_case({"host": host})["runs"] == 1


@pytest.mark.integration
def test_server_device_binding_accepts_loopback_traffic():
    """Exercise Linux loopback device selection with a real bound server."""
    assert _run_case({"server": {"bind_device": "lo"}})["runs"] == 1


@pytest.mark.integration
def test_server_loop_preserves_bind_and_captures_each_fresh_native_test():
    """Run forward then reverse tests with the same configuration and retain both results."""
    receipt = _run_case(
        {"mode": "loop", "reverse": [False, True], "server": {"extra_data": "persistent-server"}}
    )
    assert receipt["runs"] == 2
    assert receipt["reverse"] == [False, True]


@pytest.mark.integration
def test_server_success_followed_by_idle_does_not_reuse_previous_json():
    """A later idle iteration is incomplete and retains no stale traffic measurements."""
    receipt = _run_case(
        {
            "mode": "loop",
            "max_runs": None,
            "reverse": [False],
            "server": {"idle_timeout_seconds": 1},
            "expect_idle_after_success": True,
        }
    )
    assert receipt["runs"] == 1
    assert receipt["idle_after_success"] is True


@pytest.mark.integration
def test_server_loop_stops_after_result_callback():
    """Cooperative callback stop ends a persistent worker after its first test."""
    receipt = _run_case(
        {"mode": "loop", "max_runs": None, "stop_after_first": True, "reverse": [False]}
    )
    assert receipt["runs"] == 1


@pytest.mark.integration
def test_server_json_stream_delivers_native_events_and_final_result():
    """Retain both streaming delivery and the complete canonical server result."""
    receipt = _run_case(
        {"server": {"json_stream": True, "interval_seconds": 0.25, "extra_data": "server-test"}}
    )
    assert receipt["runs"] == 1
    assert receipt["events"] > 0


@pytest.mark.integration
def test_server_idle_exit_is_incomplete_without_invented_measurements():
    """An idle listener ending cleanly has no successful measurement result."""
    assert _run_case({"mode": "idle", "server": {"idle_timeout_seconds": 1}}) == {"idle": True}


@pytest.mark.integration
def test_server_blocked_listener_has_process_timeout():
    """Bound an unconnected listener independently of native traffic settings."""
    assert _run_case({"mode": "timeout"}) == {"timeout": True}


@pytest.mark.integration
@pytest.mark.parametrize("limit,failed", [(10_000_000, False), (100_000, True)])
def test_server_bitrate_limit_admits_or_rejects_requested_native_rate(limit, failed):
    """Verify native policy enforcement against the actual requested rate."""
    receipt = _run_case(
        {
            "server": {"bitrate_limit_bps": limit, "bitrate_limit_interval_seconds": 1},
            "expect_failure": failed,
            "failure_match": "larger than server limit",
        }
    )
    assert receipt["failed"] == [failed]


@pytest.mark.integration
def test_server_maximum_duration_enforced_or_explicitly_unsupported():
    """Latest native rejects excessive duration and minimum reports its missing option."""
    from iperf3_lib.exceptions import UnsupportedFeatureError
    from iperf3_lib.ffi.api import ffi, lib
    from iperf3_lib.iperf_server import Server
    from iperf3_lib.server_config import ServerConfig

    version = ffi.string(lib.iperf_get_iperf_version()).decode()
    if version in {"3.19.1", "iperf 3.19.1"}:
        with pytest.raises(UnsupportedFeatureError, match="max_duration_seconds"):
            Server(config=ServerConfig(max_duration_seconds=1)).run_once(timeout=5)
        return
    receipt = _run_case(
        {
            "server": {"max_duration_seconds": 1},
            "client": {"duration": 2},
            "expect_failure": True,
            "failure_match": "duration",
        }
    )
    assert receipt["failed"] == [True]


@pytest.fixture
def server_authentication_files(tmp_path):
    """Create temporary RSA and salted native credentials without persistent secrets."""
    executable = shutil.which("openssl")
    assert executable is not None, "native authentication qualification requires openssl"
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    users = tmp_path / "users.csv"
    for args in (
        [
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        ["pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
    ):
        subprocess.run([executable, *args], check=True, capture_output=True, timeout=15)
    # Official iperf_auth.c hashes the literal {username} prefix plus password.
    password = "ephemeral-server-qualification-password"
    digest = hashlib.sha256(("{qualification}" + password).encode()).hexdigest()
    users.write_text(f"qualification,{digest}\n", encoding="utf-8")
    return private_key, public_key, users, password


@pytest.mark.integration
@pytest.mark.parametrize("valid_credentials", [True, False])
def test_server_authentication_accepts_good_and_rejects_bad_credentials(
    server_authentication_files, valid_credentials
):
    """Exercise real RSA authentication and retain failures without password leakage."""
    private_key, public_key, users, password = server_authentication_files
    from iperf3_lib.ffi.api import ffi, lib

    # The unpatched minimum release fails even CLI-to-CLI with OpenSSL 3:
    # its EVP_PKEY_encrypt output length is zero (fixed in native 3.21).
    minimum_native = "3.19.1" in ffi.string(lib.iperf_get_iperf_version()).decode()
    expected_failure = not valid_credentials or minimum_native
    receipt = _run_case(
        {
            "server": {
                "rsa_private_key_path": str(private_key),
                "authorized_users_path": str(users),
                "time_skew_threshold_seconds": 30,
            },
            "client": {"username": "qualification", "rsa_public_key_path": str(public_key)},
            "password": password if valid_credentials else "incorrect-ephemeral-password",
            "expect_failure": expected_failure,
            "failure_match": "authorization",
        }
    )
    assert receipt["failed"] == [expected_failure]
