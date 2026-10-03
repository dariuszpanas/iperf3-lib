"""Async client worker admission and result/error preservation."""

from types import SimpleNamespace

import pytest

from iperf3_lib.config import ClientConfig, config_to_dict
from iperf3_lib.exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from iperf3_lib.result import Result, result_from_iperf_json


@pytest.mark.asyncio
async def test_client_arun_and_summary(monkeypatch):
    """Default async calls isolate native work and retain configuration and metrics."""
    from iperf3_lib import _execution
    from iperf3_lib import iperf_client as module

    cfg = ClientConfig(server="127.0.0.1", duration=1)
    calls = []

    def worker(role, options, **kwargs):
        calls.append((role, options, kwargs))
        return result_from_iperf_json({"end": {"sum_sent": {"bits_per_second": 2_000_000.0}}})

    monkeypatch.setattr(_execution, "run_worker", worker)
    monkeypatch.setattr(
        module,
        "lib",
        SimpleNamespace(iperf_new_test=lambda: pytest.fail("native work in host process")),
    )
    result = await module.Client(cfg).arun()
    assert result.ok and result.summary_mbps == pytest.approx(2)
    assert result.execution.configuration.requested == config_to_dict(cfg)
    assert calls[0][:2] == ("client", config_to_dict(cfg))
    assert calls[0][2]["timeout"] is None
    assert calls[0][2]["on_event"] is None
    assert calls[0][2]["_control"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid native setup"),
        IperfError("native setup rejected"),
        UnsupportedFeatureError("unsupported native option"),
        IperfLibraryError("native allocation failed"),
    ],
)
async def test_arun_preserves_explicit_worker_setup_errors(monkeypatch, error):
    """Moving default async calls to a worker does not fabricate failed results."""
    from iperf3_lib import _execution
    from iperf3_lib.iperf_client import Client

    def worker(*args, **kwargs):
        raise error

    monkeypatch.setattr(_execution, "run_worker", worker)
    with pytest.raises(type(error), match=str(error)) as captured:
        await Client(ClientConfig("127.0.0.1")).arun()
    assert captured.value is error


@pytest.mark.asyncio
async def test_arun_uses_owned_worker_without_invoking_run_override(monkeypatch):
    """Synchronous overrides cannot bypass async ownership or alter native results."""
    from iperf3_lib import _execution
    from iperf3_lib.iperf_client import Client

    class CustomClient(Client):
        """Supply an application override that async execution must not invoke."""

        def run(self, **kwargs):
            """Reject accidental delegation from the asynchronous method."""
            pytest.fail("arun invoked the synchronous run override")

    native_failure = Result(ok=False, error="retained native failure")

    def worker(role, options, **kwargs):
        assert role == "client"
        assert kwargs["_control"] is not None
        return native_failure

    monkeypatch.setattr(_execution, "run_worker", worker)
    assert await CustomClient(ClientConfig("127.0.0.1")).arun() is native_failure
    assert not native_failure.ok and native_failure.error == "retained native failure"


def test_result_raw_uses_independent_default_dicts():
    """Do not share mutable raw result state between model instances."""
    first = Result(ok=True)
    second = Result(ok=True)
    first.raw["changed"] = True
    assert second.raw == {}
