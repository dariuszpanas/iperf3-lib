"""Verify explicit rate intent against returned minimum/latest native JSON."""

from __future__ import annotations

import pytest

from iperf3_lib.artifacts import artifact_from_result, dumps_artifact, loads_artifact
from iperf3_lib.capabilities import get_capabilities
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.intent import RateIntent
from iperf3_lib.iperf_client import Client


@pytest.mark.integration
@pytest.mark.parametrize("protocol", [Protocol.TCP, Protocol.UDP])
@pytest.mark.parametrize("mode", ["forward", "reverse", "bidirectional"])
@pytest.mark.parametrize("aggregate", [False, True])
def test_native_rate_intent_and_artifact_provenance(iperf3_server, protocol, mode, aggregate):
    """Returned settings and flow direction must agree with the admitted rate resolution."""
    host, port = iperf3_server
    config = ClientConfig(
        host,
        port=port,
        protocol=protocol,
        parallel=2,
        duration=1,
        reverse=mode == "reverse",
        bidirectional=mode == "bidirectional",
    )
    intent = (
        RateIntent(aggregate_bps_per_direction=1_000_003)
        if aggregate
        else RateIntent(per_stream_bps=750_000)
    )
    result = Client(config, rate_intent=intent).run()
    assert result.ok, result.error
    expected_rate = 500_001 if aggregate else 750_000
    native = result.raw["start"]["test_start"]
    assert native["protocol"] == protocol.value.upper()
    assert native["target_bitrate"] == expected_rate
    assert native["num_streams"] == 2
    assert native["reverse"] == (mode == "reverse")
    assert native["bidir"] == (mode == "bidirectional")
    assert {flow.direction for flow in result.flows} == (
        {"client_to_server", "server_to_client"}
        if mode == "bidirectional"
        else {"server_to_client"}
        if mode == "reverse"
        else {"client_to_server"}
    )
    assert result.execution.configuration.requested["rate"] == expected_rate
    effective = result.execution.configuration.effective["rate"]
    assert effective.state == "verified" and effective.value == expected_rate
    extension = result.extensions["iperf3_lib.rate_intent"]
    assert extension["caller_config"]["rate"] is None
    assert extension["resolution"]["native_per_stream_bps"] == expected_rate
    assert extension["resolution"]["unused_bps_per_direction"] == (1 if aggregate else 0)
    assert extension["resolution"]["aggregate_bps_all_directions"] == expected_rate * 2 * (
        2 if mode == "bidirectional" else 1
    )
    assert (
        loads_artifact(dumps_artifact(artifact_from_result(result))).result.extensions
        == result.extensions
    )
    report = get_capabilities(result=result)
    assert report.library.state == "available" and report.library.version
    assert report.execution.status == "completed"
    assert report.execution.native_version == result.raw["start"]["version"]
    rate_feature = next(feature for feature in report.features if feature.name == "rate")
    assert rate_feature.wrapper == "supported"
    assert rate_feature.symbols[0].state == "present"
    assert rate_feature.runtime == "not_run"
