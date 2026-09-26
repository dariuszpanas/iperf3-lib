"""Strict rate intent, exact unit parsing and finite admission estimates."""

from __future__ import annotations

import pytest

from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.intent import MAX_RATE, RateIntent, estimate_plan, parse_rate, resolve_rate


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"per_stream_bps": 1, "aggregate_bps_per_direction": 1},
        {"per_stream_bps": True},
        {"per_stream_bps": 1.0},
        {"per_stream_bps": "1"},
        {"per_stream_bps": -1},
        {"per_stream_bps": MAX_RATE + 1},
        {"aggregate_bps_per_direction": 0},
        {"aggregate_bps_per_direction": -1},
        {"aggregate_bps_per_direction": True},
        {"aggregate_bps_per_direction": MAX_RATE + 1},
    ],
)
def test_invalid_intent_is_rejected(kwargs):
    """Keep intent strict and reject conflicts even when their values agree."""
    with pytest.raises((TypeError, ValueError)):
        RateIntent(**kwargs)


@pytest.mark.parametrize("mode", [{}, {"reverse": True}, {"bidirectional": True}])
def test_aggregate_allocation_rounds_down_per_direction_and_preserves_remainder(mode):
    """Resolve each simultaneous direction separately without mutating config."""
    cfg = ClientConfig("host", parallel=3, **mode)
    result = resolve_rate(cfg, RateIntent(aggregate_bps_per_direction=10))
    assert cfg.rate is None
    assert result.native_per_stream_bps == 3
    assert result.aggregate_bps_per_direction == 9
    assert result.unused_bps_per_direction == 1
    assert result.aggregate_bps_all_directions == (18 if mode.get("bidirectional") else 9)
    assert result.active_directions == (
        ("client_to_server", "server_to_client")
        if mode.get("bidirectional")
        else ("server_to_client",)
        if mode.get("reverse")
        else ("client_to_server",)
    )
    assert result.source == "aggregate"
    assert result.to_dict()["active_directions"] == list(result.active_directions)


def test_too_small_aggregate_and_legacy_intent_conflict_are_rejected():
    """Avoid rounding to native unlimited and avoid competing sources of intent."""
    with pytest.raises(ValueError, match="parallel"):
        resolve_rate(ClientConfig("host", parallel=3), RateIntent(aggregate_bps_per_direction=2))
    for legacy in (0, 1):
        with pytest.raises(ValueError, match="cannot both"):
            resolve_rate(ClientConfig("host", rate=legacy), RateIntent(per_stream_bps=legacy))


@pytest.mark.parametrize("protocol", [Protocol.TCP, Protocol.UDP, Protocol.SCTP])
def test_protocol_defaults_and_explicit_unlimited_preserve_unknown_totals(protocol):
    """Differentiate a bounded UDP default from native unlimited defaults and zero."""
    cfg = ClientConfig("host", protocol=protocol, parallel=2)
    default = resolve_rate(cfg)
    assert default.source == "protocol_default"
    assert default.native_per_stream_bps == (1048576 if protocol is Protocol.UDP else None)
    assert default.aggregate_bps_per_direction == (2097152 if protocol is Protocol.UDP else None)
    for result in (
        resolve_rate(cfg, RateIntent(per_stream_bps=0)),
        resolve_rate(ClientConfig("host", rate=0)),
    ):
        assert result.native_per_stream_bps == 0
        assert result.aggregate_bps_per_direction is None
        assert result.aggregate_bps_all_directions is None
        assert result.unused_bps_per_direction is None


def test_per_stream_and_legacy_native_maximum_are_exact():
    """Use integer arithmetic even when aggregate estimates exceed native uint64."""
    result = resolve_rate(
        ClientConfig("host", parallel=128, bidirectional=True), RateIntent(per_stream_bps=MAX_RATE)
    )
    assert result.aggregate_bps_all_directions == MAX_RATE * 256
    assert result.source == "per_stream"
    assert resolve_rate(ClientConfig("host", rate=MAX_RATE)).source == "legacy"


def test_resolver_revalidates_config_and_argument_types():
    """Mutable configuration must pass its normal validation at admission."""
    cfg = ClientConfig("host")
    cfg.parallel = 0
    with pytest.raises(ValueError):
        resolve_rate(cfg)
    with pytest.raises(TypeError):
        resolve_rate("host")
    with pytest.raises(TypeError):
        resolve_rate(ClientConfig("host"), 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1bit/s", 1),
        ("0 bit/s", 0),
        ("1.5 Mbit/s", 1500000),
        ("0.125 B/s", 1),
        ("2 MB/s", 16000000),
        (" 2.25Gbps ", 2250000000),
        ("1 kbps", 1000),
        ("1 TB/s", 8 * 10**12),
        (str(MAX_RATE) + " bit/s", MAX_RATE),
    ],
)
def test_rate_units_parse_exactly(text, expected):
    """Decimal SI and byte conversion retain whole-bit precision without floats."""
    assert parse_rate(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1",
        "-1 bit/s",
        "+1 bit/s",
        "1e3 bit/s",
        "1 MiB/s",
        "1 mbps",
        "1 Mbps/s",
        "0.1 bit/s",
        "NaN bit/s",
        "inf bit/s",
        "1.2.3 Mbps",
        "1_000 bps",
        "1" * 101,
        str(MAX_RATE + 1) + " bit/s",
        1,
        True,
    ],
)
def test_rate_units_reject_ambiguous_or_invalid_input(text):
    """Require explicit units, bounded exact integers, and case-sensitive prefixes."""
    with pytest.raises((TypeError, ValueError)):
        parse_rate(text)


def test_plan_estimates_count_warmup_directions_and_payload_once():
    """Estimate payload targets separately from observed bytes and wall-clock duration."""
    configs = [
        ClientConfig("host", duration=2, omit=1, parallel=2, bidirectional=True),
        ClientConfig("host", duration=1, rate=1),
    ]
    intents = [RateIntent(aggregate_bps_per_direction=10), None]
    result = estimate_plan(configs, intents, max_payload_bytes=8, max_active_seconds=4)
    assert result.active_seconds == 4
    assert result.estimated_payload_bits == 61
    assert result.estimated_payload_bytes == 8
    assert result.runs[0].estimated_payload_bits == 60
    assert result.runs[0].rate.native_per_stream_bps == 5
    with pytest.raises(ValueError, match="payload admission"):
        estimate_plan(configs, intents, max_payload_bytes=7)
    with pytest.raises(ValueError, match="active-time"):
        estimate_plan(configs, intents, max_active_seconds=3)


def test_plan_unbounded_and_empty_estimates_are_distinct():
    """Unknown traffic cannot pass finite byte admission; an empty plan costs zero."""
    result = estimate_plan([ClientConfig("host")])
    assert result.estimated_payload_bits is None and result.estimated_payload_bytes is None
    with pytest.raises(ValueError, match="unbounded"):
        estimate_plan([ClientConfig("host")], max_payload_bytes=100)
    empty = estimate_plan([], max_active_seconds=0, max_payload_bytes=0)
    assert (
        empty.active_seconds == empty.estimated_payload_bits == empty.estimated_payload_bytes == 0
    )


@pytest.mark.parametrize(
    "configs,kwargs",
    [
        (iter([]), {}),
        ("host", {}),
        (["host"], {}),
        ([], {"intents": [None]}),
        ([], {"intents": "bad"}),
        ([], {"intents": iter([])}),
        ([], {"max_payload_bytes": True}),
        ([], {"max_payload_bytes": -1}),
        ([], {"max_active_seconds": 1.0}),
        ([], {"max_active_seconds": -1}),
    ],
)
def test_plan_rejects_invalid_inputs(configs, kwargs):
    """Admit finite typed sequences and strict nonnegative budget integers only."""
    with pytest.raises((TypeError, ValueError)):
        estimate_plan(configs, **kwargs)
