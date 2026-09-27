"""Server constructor compatibility and configuration admission tests."""

from dataclasses import asdict

import pytest

from iperf3_lib.iperf_server import Server
from iperf3_lib.server_config import ServerConfig


def test_server_defaults_and_legacy_positional_arguments():
    """Preserve valid legacy constructor usage through the typed configuration."""
    assert Server().config == ServerConfig()
    server = Server(5222, "127.0.0.1")
    assert server.port == 5222
    assert server.bind_host == "127.0.0.1"
    assert server.config.bind_address == "127.0.0.1"


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_server_rejects_out_of_range_port(port):
    """Reject ports outside the valid TCP/UDP range at construction time."""
    with pytest.raises(ValueError, match="between 1 and 65535"):
        Server(port=port)


@pytest.mark.parametrize("port", [True, 5201.0, "5201", None])
def test_server_rejects_non_integer_port(port):
    """Reject values that only look like integer ports."""
    with pytest.raises(TypeError, match="integer"):
        Server(port=port)


@pytest.mark.parametrize("value", [False, 0, [], "", " ", "localhost\0ignored"])
def test_legacy_bind_host_rejects_falsey_or_truncated_addresses(value):
    """Never silently skip a requested bind or truncate it at a NUL byte."""
    with pytest.raises((TypeError, ValueError), match="bind_address"):
        Server(bind_host=value)


@pytest.mark.parametrize("legacy", [{"port": 5201}, {"bind_host": None}])
def test_server_config_cannot_mix_with_legacy_arguments(legacy):
    """Reject even explicit default legacy values alongside a configuration."""
    with pytest.raises(ValueError, match="cannot be combined"):
        Server(config=ServerConfig(), **legacy)


def test_server_config_type_and_detachment():
    """Detach the mutable dataclass and reject objects of another type."""
    with pytest.raises(TypeError, match="ServerConfig"):
        Server(config={})
    config = ServerConfig(bind_address="127.0.0.1", control_keepalive=(9, 2, 3))
    server = Server(config=config)
    config.port = 65535
    assert server.port == 5201
    assert asdict(server.config)["control_keepalive"] == (9, 2, 3)


def test_legacy_alias_setters_validate_without_replacing_good_config():
    """Keep valid legacy property writes while rejecting invalid mutations."""
    server = Server()
    server.port = 5222
    server.bind_host = "::1"
    assert server.config == ServerConfig(port=5222, bind_address="::1")
    with pytest.raises(ValueError, match="port"):
        server.port = 0
    with pytest.raises(ValueError, match="bind_address"):
        server.bind_host = ""
    assert server.port == 5222
    assert server.bind_host == "::1"


@pytest.mark.parametrize("mutation", [{"port": False}, {"bind_address": ""}])
def test_config_is_revalidated_before_worker_admission(monkeypatch, mutation):
    """Mutating an admitted dataclass cannot bypass execution validation."""
    import iperf3_lib.iperf_server as module

    monkeypatch.setattr(module, "_run_worker", lambda *a, **kw: pytest.fail("worker admitted"))
    server = Server()
    for key, value in mutation.items():
        setattr(server.config, key, value)
    with pytest.raises((ValueError, TypeError)):
        server.run_once()
    with pytest.raises((ValueError, TypeError)):
        server.serve_forever()


def test_replaced_config_rejected_before_worker_admission():
    """Reassignment to an unrelated object yields a clear type error."""
    server = Server()
    server.config = {}
    with pytest.raises(TypeError, match="ServerConfig"):
        server.run_once()
