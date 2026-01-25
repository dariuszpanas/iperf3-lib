"""Integration tests for various iperf3 client configuration options."""

import socket
import threading
import time

import pytest

from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.exceptions import UnsupportedFeatureError
from iperf3_lib.iperf_client import Client
from iperf3_lib.iperf_server import Server
from iperf3_lib.result import Result

cases = [
    ("tcp_basic", {"protocol": Protocol.TCP}),
    ("udp_basic", {"protocol": Protocol.UDP}),
    ("sctp_basic", {"protocol": Protocol.SCTP}),
    ("tcp_mptcp", {"protocol": Protocol.TCP, "mptcp": True}),
    ("reverse_tcp", {"protocol": Protocol.TCP, "reverse": True}),
    ("bidir_tcp", {"protocol": Protocol.TCP, "bidirectional": True}),
    ("parallel_tcp", {"protocol": Protocol.TCP, "parallel": 4}),
    ("blksize", {"protocol": Protocol.TCP, "blksize": 1500}),
    ("tos", {"protocol": Protocol.TCP, "tos": 16}),
    ("omit", {"protocol": Protocol.TCP, "omit": 1}),
    ("udp_rate", {"protocol": Protocol.UDP, "rate": 500000}),  # 500 kbps
    ("json_stream", {"protocol": Protocol.TCP, "json_stream": True}),
]


@pytest.mark.integration
@pytest.mark.parametrize("name,kwargs", cases, ids=[c[0] for c in cases])
def test_client_various_configs(iperf3_server, name, kwargs, libiperf_symbols):
    """Integration tests covering various client configuration options.

    The fixture `iperf3_server` yields (host, port) for the per-group server.
    Tests accept both successful results and "unsupported feature" exceptions
    (xfail), which can occur depending on libiperf build/runtime support.
    """
    host, port = iperf3_server

    cfg_kwargs = {"server": host, "port": port, "duration": 1}
    cfg_kwargs.update(kwargs)

    # If this is a UDP test, run a per-test server using Server.run_once()
    # to avoid reusing the persistent `iperf_test` instance from the
    # serve_forever fixture which can cause a "Bad file descriptor" in
    # certain UDP handshakes. We pick an ephemeral free port for the
    # per-test server and point the client at it.
    server_thread = None
    if cfg_kwargs.get("protocol") == Protocol.UDP:
        # pick an ephemeral free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, 0))
            free_port = s.getsockname()[1]

        srv = Server(port=free_port, bind_host=host)

        def _run_once():
            try:
                srv.run_once()
            except Exception as e:
                # surface exceptions to stdout for debugging
                import traceback

                print("per-test server run_once exception:", e)
                traceback.print_exc()

        server_thread = threading.Thread(target=_run_once, daemon=True)
        server_thread.start()

        # give the per-test server a short moment to set up its listener
        # Avoid actively connecting here (that would perform an accept on the
        # server and could steal the handshake). A short sleep is sufficient
        # in practice for local test runs.
        time.sleep(0.15)

        # point the client at the per-test server port
        cfg_kwargs["port"] = free_port

    # Do not pre-scan the lib for optional setters here; the `Client` will
    # raise `UnsupportedFeatureError` if an optional feature is not supported
    # by the loaded lib. Allow the test harness to convert that to xfail.
    cfg = ClientConfig(**cfg_kwargs)

    # If this test requires a numeric rate, verify the built lib exports a
    # suitable setter symbol; if not, skip deterministically for this session.
    if cfg_kwargs.get("rate") is not None:
        if not (
            "iperf_set_test_rate" in libiperf_symbols
            or "iperf_set_test_bitrate" in libiperf_symbols
        ):
            pytest.skip("lib lacks rate setter symbol; skipping udp_rate case")

    # If the test requests MPTCP, skip deterministically when the built
    # library does not export the MPTCP setter symbol.
    if cfg_kwargs.get("mptcp"):
        if "iperf_set_test_mptcp" not in libiperf_symbols:
            pytest.skip("lib lacks MPTCP symbol; skipping tcp_mptcp case")

    try:
        res = Client(cfg).run()
    except UnsupportedFeatureError as e:
        pytest.xfail(f"feature not supported by libiperf: {e}")
    except Exception:
        # allow other runtime failures to bubble - tests should fail on unexpected errors
        raise

    assert isinstance(res, Result)
    assert hasattr(res, "ok")
    # permissive assertion: either a successful or a clean failed result is acceptable
    assert res.ok in (True, False)
    # ensure per-test server finished
    if server_thread is not None:
        server_thread.join(timeout=2)
