"""Expanded native getter evidence and event admission contracts."""

import pytest

from iperf3_lib import Client, ClientConfig, Server
from iperf3_lib.exceptions import IperfError
from iperf3_lib.result import ExecutionMetadata, Result, result_from_iperf_json


def test_event_callback_promotes_unknown_getter_evidence(monkeypatch):
    """Observed getters fill unknown settings without changing caller configuration."""
    from iperf3_lib import _execution

    config = ClientConfig("localhost")

    def run_worker(role, options, **kwargs):
        assert role == "client" and options["json_stream"] is True
        result = result_from_iperf_json({"end": {"sum_sent": {"bits_per_second": 1}}})
        result.extensions["iperf3_lib.native_configuration"] = {
            "json_stream": {"getter": "iperf_get_test_json_stream", "value": True},
            "duration": {"getter": "iperf_get_test_duration", "value": 10},
        }
        return result

    monkeypatch.setattr(_execution, "run_worker", run_worker)
    result = Client(config).run(on_event=lambda event: None)
    assert not config.json_stream
    assert result.execution.configuration.effective["json_stream"].value is True
    assert result.execution.configuration.effective["duration"].value == 10


def test_getter_does_not_hide_conflicting_native_json(monkeypatch):
    """Conflicting native rate headers remain unknown even with a getter receipt."""
    from iperf3_lib import _execution

    result = result_from_iperf_json(
        {"start": {"target_bitrate": 10, "test_start": {"target_bitrate": 20}}}
    )
    result.extensions["iperf3_lib.native_configuration"] = {
        "rate": {"getter": "iperf_get_test_rate", "value": 10}
    }
    monkeypatch.setattr(_execution, "run_worker", lambda *a, **k: result)
    result = Client(ClientConfig("localhost", title="test")).run()
    assert result.execution.configuration.effective["rate"].state == "unavailable"


@pytest.mark.parametrize("method", ["run_once", "serve_forever"])
def test_server_event_callback_enables_detached_streaming(monkeypatch, method):
    """Server and client callbacks share the implicit streaming admission rule."""
    import iperf3_lib.iperf_server as server_module

    def run_worker(options, **kwargs):
        assert options["json_stream"] is True
        return Result(ok=True)

    monkeypatch.setattr(server_module, "_run_worker", run_worker)
    server = Server()
    getattr(server, method)(on_event=lambda event: None)
    assert not server.config.json_stream


def test_server_without_result_handler_exposes_native_failure(monkeypatch):
    """A default serving loop cannot silently discard its native failure."""
    import iperf3_lib.iperf_server as server_module

    monkeypatch.setattr(
        server_module,
        "_run_worker",
        lambda *a, **k: Result(
            ok=False, error="listener failed", execution=ExecutionMetadata(status="failed")
        ),
    )
    with pytest.raises(IperfError, match="listener failed"):
        Server().serve_forever()


def test_minimum_native_authentication_failure_explains_known_native_limitation(monkeypatch):
    """Retain native failures and distinguish a known minimum-version crypto issue."""
    from iperf3_lib import _execution

    failed = Result(
        ok=False,
        error="test authorization failed",
        execution=ExecutionMetadata(status="failed", native_version="3.19.1"),
    )
    monkeypatch.setattr(_execution, "run_worker", lambda *a, **k: failed)
    result = Client(
        ClientConfig("localhost", username="test", rsa_public_key_path="public.pem"),
        password="ephemeral",
    ).run()
    assert result.error == "test authorization failed"
    assert any(
        d.code == "execution.native_authentication_compatibility" for d in result.diagnostics
    )
