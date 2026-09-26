"""Pure analysis contracts over unequal, missing and methodologically distinct data."""

import copy
import math
from dataclasses import replace

import pytest

from iperf3_lib.analysis import (
    AnalysisTrial,
    ComparisonPolicy,
    IntervalPolicy,
    Selection,
    SequentialMethodology,
    check_compatibility,
    interval_stability,
    sequential_asymmetry,
    simultaneous_asymmetry,
    stream_balance,
    stream_scaling,
    summary_throughput,
)
from iperf3_lib.result import (
    ConfigurationSnapshot,
    ExecutionMetadata,
    FlowStats,
    IntervalStats,
    Result,
    StreamStats,
    SumStats,
    VerifiedSetting,
)


def _result(*, reverse=False, parallel=2, count=1_750_000, duration=4):
    direction = "server_to_client" if reverse else "client_to_server"
    effective = {
        name: VerifiedSetting(value, "verified", [f"/raw/start/test_start/{name}"])
        for name, value in {
            "parallel": parallel,
            "duration": 4,
            "omit": 0,
            "rate": 4_000_000,
            "blksize": 1200,
            "tos": 0,
            "server": "127.0.0.1",
            "port": 5201,
        }.items()
    }
    return Result(
        True,
        raw={"start": {"test_start": {name: value.value for name, value in effective.items()}}},
        protocol="tcp",
        reporting_role="client",
        execution=ExecutionMetadata(
            "completed",
            "reverse" if reverse else "forward",
            configuration=ConfigurationSnapshot(effective=effective),
            native_version="iperf 3.21",
            native_system_info="same Linux endpoint",
        ),
        flows=[
            FlowStats(
                direction,
                SumStats(
                    bits_per_second=99_000_000,
                    bytes=count * 2,
                    duration_seconds=duration,
                    direction=direction,
                    observation="sender",
                ),
                SumStats(
                    bits_per_second=99_000_000,
                    bytes=count,
                    duration_seconds=duration,
                    direction=direction,
                    observation="receiver",
                ),
            )
        ],
        intervals=[
            IntervalStats(
                0,
                1,
                8_000_000,
                direction,
                "receiver",
                scope="aggregate",
                bytes=1_000_000,
                duration_seconds=1,
                omitted=False,
            ),
            IntervalStats(
                1,
                4,
                2_000_000,
                direction,
                "receiver",
                scope="aggregate",
                bytes=750_000,
                duration_seconds=3,
                omitted=False,
            ),
        ],
        streams=[
            StreamStats(
                direction,
                5,
                receiver=SumStats(
                    bytes=1_000_000, duration_seconds=4, direction=direction, observation="receiver"
                ),
            ),
            StreamStats(
                direction,
                7,
                receiver=SumStats(
                    bytes=750_000, duration_seconds=4, direction=direction, observation="receiver"
                ),
            ),
        ],
    )


def _stability(result=None, **kwargs):
    return interval_stability(
        result or _result(), selection=Selection("client_to_server", "receiver"), **kwargs
    )


def _policy(**kwargs):
    return ComparisonPolicy("lab-comparison", ("client-A", "server-B"), **kwargs)


def test_unequal_interval_arithmetic_and_quantile_definition():
    """Weights describe measured time and derive 3.5Mbps, not the unweighted 5Mbps."""
    analysis = _stability(threshold_bps=4_000_000, quantiles=(0, 0.5, 0.95, 1))
    assert analysis.quality == "complete"
    assert analysis.coverage.measured_seconds == 4
    assert analysis.coverage.measured_time_fraction == 1
    assert analysis.interval_bytes_throughput_bps == 3_500_000
    assert analysis.duration_weighted_mean_interval_average_bps == 3_500_000
    assert analysis.minimum_interval_average_bps == 2_000_000
    assert analysis.duration_weighted_stddev_interval_average_bps == pytest.approx(
        math.sqrt(6.75) * 1_000_000
    )
    assert analysis.interval_average_coefficient_of_variation == pytest.approx(
        math.sqrt(6.75) / 3.5
    )
    assert analysis.duration_weighted_interval_average_quantiles_bps == {
        0: 2_000_000,
        0.5: 2_000_000,
        0.95: 8_000_000,
        1: 8_000_000,
    }
    assert analysis.below_threshold_fraction_of_measured_time == 0.75
    assert [item.path for item in analysis.evidence] == ["/intervals/0", "/intervals/1"]


def test_summary_uses_selected_bytes_and_measured_duration_not_reported_or_requested_rate():
    """Observer selection, measured zero and measured duration remain independent."""
    result = _result()
    result.duration_seconds = 99
    result.execution.timing.elapsed_seconds = 100
    received = summary_throughput(result, direction="client_to_server", observation="receiver")
    sent = summary_throughput(result, direction="client_to_server", observation="sender")
    assert received.throughput_bps == 3_500_000
    assert sent.throughput_bps == 7_000_000
    result.flows[0].receiver.bytes = 0
    assert (
        summary_throughput(
            result, direction="client_to_server", observation="receiver"
        ).throughput_bps
        == 0
    )
    result.flows[0].receiver.duration_seconds = 0
    assert (
        summary_throughput(result, direction="client_to_server", observation="receiver").quality
        == "insufficient_data"
    )


def test_warmup_excluded_before_overlap_checks_and_other_populations_not_counted():
    """Overlapping warm-up and component streams cannot duplicate selected aggregate data."""
    result = _result()
    result.intervals.insert(
        0,
        IntervalStats(
            0,
            4,
            100_000_000,
            "client_to_server",
            "receiver",
            scope="aggregate",
            bytes=50_000_000,
            duration_seconds=4,
            omitted=True,
        ),
    )
    result.intervals.append(replace(result.intervals[1], scope="stream", stream_id=5))
    result.intervals.append(replace(result.intervals[1], observation="sender"))
    result.intervals.reverse()
    analysis = _stability(result)
    assert analysis.quality == "complete"
    assert analysis.coverage.omitted_count == 1
    assert analysis.coverage.included_count == 2
    assert analysis.interval_bytes_throughput_bps == 3_500_000


def test_unknown_omission_is_recorded_under_both_explicit_policies():
    """Missing omission is not treated as measured zero or confidently non-omitted."""
    result = _result()
    result.intervals[0].omitted = None
    excluded = _stability(result)
    assert excluded.quality == "insufficient_data"
    assert excluded.coverage.unknown_omission_count == 1
    assert excluded.coverage.measured_time_fraction is None
    included = _stability(result, policy=IntervalPolicy(unknown_omission="include"))
    assert included.quality == "partial"
    assert included.interval_bytes_throughput_bps == 3_500_000


def test_missing_bytes_rate_duration_and_explicit_duration_derivation():
    """Partial stability remains distinguishable from complete bytes/time throughput."""
    result = _result()
    result.intervals[0].bytes = None
    analysis = _stability(result)
    assert analysis.interval_rate_basis == "mixed"
    assert analysis.quality == "partial"
    assert analysis.coverage.byte_covered_seconds == 3
    assert analysis.interval_bytes_throughput_bps is None
    result.intervals[1].bytes = None
    assert _stability(result).interval_rate_basis == "reported"
    result.intervals[0].bits_per_second = None
    assert _stability(result).coverage.invalid_or_missing_count == 1
    result = _result()
    result.intervals[0].duration_seconds = None
    assert _stability(result).quality == "insufficient_data"
    derived = _stability(result, policy=IntervalPolicy(derive_duration_from_bounds=True))
    assert derived.quality == "partial"
    assert derived.interval_bytes_throughput_bps == 3_500_000
    assert any(item.code == "duration.derived" for item in derived.diagnostics)


def test_reported_rate_disagreement_preserves_derived_bytes_formula():
    """A native reported-rate disagreement is diagnosed and never replaces bytes/time."""
    result = _result()
    result.intervals[0].bits_per_second = 0
    analysis = _stability(result)
    assert analysis.quality == "partial"
    assert analysis.interval_bytes_throughput_bps == 3_500_000
    assert any(item.code == "measurement.rate_difference" for item in analysis.diagnostics)


def test_zero_rates_threshold_ties_and_single_interval_are_explicit():
    """Undefined ratios are null while zero is retained as an observed measurement."""
    result = _result()
    for interval in result.intervals:
        interval.bytes = interval.bits_per_second = 0
    analysis = _stability(result, threshold_bps=0)
    assert analysis.quality == "complete"
    assert analysis.interval_average_coefficient_of_variation is None
    assert analysis.interval_bytes_throughput_bps == 0
    assert analysis.below_threshold_fraction_of_measured_time == 0
    result.intervals.pop()
    assert _stability(result).quality == "insufficient_data"


@pytest.mark.parametrize(
    "alteration",
    [
        "overlap",
        "duplicate",
        "missing_bounds",
        "duration_conflict",
        "all_omitted",
        "unknown_scope",
        "unknown_direction",
        "unknown_observation",
        "missing_duration",
        "zero_duration",
    ],
)
def test_bad_or_missing_time_population_is_never_confidently_complete(alteration):
    """Temporal/provenance gaps are surfaced without interpolation or zero fill."""
    result = _result()
    if alteration == "overlap":
        result.intervals[1].start_seconds = 0
        result.intervals[1].end_seconds = 3
    elif alteration == "duplicate":
        result.intervals.append(copy.deepcopy(result.intervals[0]))
    elif alteration == "missing_bounds":
        result.intervals[0].start_seconds = None
    elif alteration == "duration_conflict":
        result.intervals[0].duration_seconds = 9
    elif alteration == "all_omitted":
        for interval in result.intervals:
            interval.omitted = True
    elif alteration == "unknown_scope":
        result.intervals[0].scope = "unknown"
    elif alteration == "unknown_direction":
        result.intervals[0].direction = "unknown"
    elif alteration == "unknown_observation":
        result.intervals[0].observation = None
    else:
        result.intervals[0].duration_seconds = None if alteration == "missing_duration" else 0
    analysis = _stability(result)
    assert analysis.quality != "complete"
    if alteration in {"overlap", "duplicate", "all_omitted"}:
        assert analysis.interval_bytes_throughput_bps is None


def test_time_gaps_reduce_coverage_without_inserting_zero_throughput():
    """Five seconds of observed span includes only four seconds of measured rates."""
    result = _result()
    result.intervals[1].start_seconds += 1
    result.intervals[1].end_seconds += 1
    analysis = _stability(result)
    assert analysis.quality == "partial"
    assert analysis.coverage.measured_time_fraction == 0.8
    assert analysis.interval_bytes_throughput_bps == 3_500_000


@pytest.mark.parametrize(
    "field,value",
    [
        ("bits_per_second", True),
        ("bits_per_second", float("nan")),
        ("duration_seconds", -1),
        ("duration_seconds", float("inf")),
        ("bytes", 1.5),
        ("bytes", -1),
        ("bytes", True),
        ("start_seconds", float("inf")),
        ("omitted", 1),
    ],
)
def test_manually_constructed_invalid_interval_values_raise(field, value):
    """Dataclass construction does not bypass finite numeric and count validation."""
    result = _result()
    setattr(result.intervals[0], field, value)
    with pytest.raises(ValueError):
        _stability(result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold_bps": True},
        {"threshold_bps": -1},
        {"quantiles": (float("nan"),)},
        {"quantiles": (True,)},
        {"quantiles": (1.1,)},
        {"quantiles": (0.5, 0.5)},
        {"quantiles": [0.5]},
        {"policy": {}},
    ],
)
def test_analysis_options_are_strict(kwargs):
    """Invalid user policies fail immediately instead of becoming misleading calculations."""
    with pytest.raises(ValueError):
        _stability(**kwargs)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Selection("unknown", "sender"),
        lambda: Selection("client_to_server", "unknown"),
        lambda: Selection("client_to_server", "sender", "stream"),
        lambda: Selection("client_to_server", "sender", stream_id=3),
        lambda: IntervalPolicy(minimum_intervals=0),
        lambda: IntervalPolicy(minimum_intervals=True),
        lambda: IntervalPolicy(time_tolerance_seconds=float("nan")),
        lambda: IntervalPolicy(unknown_omission="guess"),
        lambda: IntervalPolicy(derive_duration_from_bounds=1),
    ],
)
def test_invalid_selection_and_policy_constructors(factory):
    """A selection must identify one unambiguous known data population."""
    with pytest.raises(ValueError):
        factory()


def test_stream_balance_ratios_missing_streams_and_all_zero():
    """Missing streams differ from zero-rate streams and remain visible in coverage."""
    result = _result()
    analysis = stream_balance(result, direction="client_to_server", observation="receiver")
    assert analysis.quality == "complete"
    assert analysis.min_over_max_rate == 0.75
    assert analysis.jain_fairness_index == pytest.approx(1.75**2 / (2 * (1 + 0.75**2)))
    assert analysis.population_rate_cv == pytest.approx(1 / 7)
    result.streams[1].receiver = None
    partial = stream_balance(result, direction="client_to_server", observation="receiver")
    assert partial.quality == "partial"
    assert partial.stream_coverage_fraction == 0.5
    assert partial.per_stream[1].throughput_bps is None
    result = _result()
    for stream in result.streams:
        stream.receiver.bytes = 0
    zero = stream_balance(result, direction="client_to_server", observation="receiver")
    assert zero.min_over_max_rate is zero.population_rate_cv is zero.jain_fairness_index is None


def test_stream_interval_source_handles_udp_unattributed_end_data_explicitly():
    """Explicit interval selection supports UDP without relabeling mixed end summaries."""
    result = _result()
    result.protocol = "udp"
    for stream in result.streams:
        stream.unattributed = stream.receiver
        stream.receiver = None
    assert (
        stream_balance(result, direction="client_to_server", observation="receiver").quality
        == "insufficient_data"
    )
    result.intervals = [
        replace(interval, scope="stream", stream_id=stream.stream_id)
        for stream in result.streams
        for interval in result.intervals
    ]
    analysis = stream_balance(
        result, direction="client_to_server", observation="receiver", source="intervals"
    )
    assert analysis.quality == "complete"
    assert analysis.min_over_max_rate == 1


def test_duplicate_unknown_and_excess_stream_identities():
    """Run-local socket identities cannot be invented or counted twice."""
    result = _result()
    result.streams[1].stream_id = result.streams[0].stream_id
    with pytest.raises(ValueError, match="duplicate"):
        stream_balance(result, direction="client_to_server", observation="receiver")
    result.streams[1].stream_id = None
    assert (
        stream_balance(result, direction="client_to_server", observation="receiver").quality
        == "partial"
    )
    result = _result(parallel=1)
    assert (
        stream_balance(result, direction="client_to_server", observation="receiver").quality
        == "insufficient_data"
    )


def _trials(counts=(1, 2, 4, 8), amounts=(100, 150, 200, 195)):
    return [
        AnalysisTrial(f"trial-{count}", _result(parallel=count, count=amount, duration=1))
        for count, amount in zip(counts, amounts, strict=True)
    ]


def test_scaling_selects_smallest_tested_count_at_configurable_fraction():
    """Nonmonotonic observed throughput does not imply an untested optimum."""
    trials = _trials()
    analysis = stream_scaling(
        trials, direction="client_to_server", observation="receiver", compatibility=_policy()
    )
    assert analysis.quality == "complete"
    assert analysis.best_observed_bps == 1600
    assert analysis.smallest_tested_qualifying_count == 4
    assert [group.stream_count for group in analysis.per_count_trial_summary] == [1, 2, 4, 8]
    assert (
        stream_scaling(
            trials,
            direction="client_to_server",
            observation="receiver",
            compatibility=_policy(),
            best_fraction=0.7,
        ).smallest_tested_qualifying_count
        == 2
    )


def test_scaling_uses_per_count_trial_medians_and_retains_failed_trials():
    """A large outlier and failed run cannot hide the recorded trial population."""
    trials = [
        AnalysisTrial(str(index), _result(parallel=2, count=amount, duration=1))
        for index, amount in enumerate((100, 110, 10000))
    ]
    failed = _result(parallel=4)
    failed.ok = False
    failed.error = "failed"
    failed.execution.status = "failed"
    trials.append(AnalysisTrial("failed", failed))
    analysis = stream_scaling(
        trials,
        direction="client_to_server",
        observation="receiver",
        compatibility=_policy(),
        minimum_valid_trials=3,
    )
    assert analysis.best_observed_bps == 880
    assert analysis.excluded_trials == ("failed",)
    assert analysis.quality == "partial"
    insufficient = stream_scaling(
        trials,
        direction="client_to_server",
        observation="receiver",
        compatibility=_policy(),
        minimum_valid_trials=4,
    )
    assert insufficient.quality == "insufficient_data"
    assert insufficient.smallest_tested_qualifying_count is None


def test_concrete_compatibility_beyond_labels_and_explicit_override_reasons():
    """Equal group labels cannot hide different native versions or unknown configuration."""
    trials = _trials()
    trials[1].result.execution.native_version = "iperf 3.19.1"
    analysis = stream_scaling(
        trials, direction="client_to_server", observation="receiver", compatibility=_policy()
    )
    assert analysis.quality == "insufficient_data"
    assert analysis.smallest_tested_qualifying_count is None
    allowed = stream_scaling(
        trials,
        direction="client_to_server",
        observation="receiver",
        compatibility=_policy(
            allowed_differences={"native_version": "Deliberate comparison of native versions."}
        ),
    )
    assert allowed.smallest_tested_qualifying_count == 4
    assert allowed.quality == "partial"
    trials[0].result.execution.configuration.effective["tos"].state = "unavailable"
    assert (
        stream_scaling(
            trials,
            direction="client_to_server",
            observation="receiver",
            compatibility=_policy(allowed_differences={"native_version": "Intentional."}),
        ).quality
        == "insufficient_data"
    )


def test_fixed_aggregate_rate_intent_records_why_native_per_stream_rate_varies():
    """A #30 aggregate target differs from a native fixed-per-stream sweep."""
    trials = _trials()
    for trial in trials:
        parallel = trial.result.execution.configuration.effective["parallel"].value
        trial.result.execution.configuration.effective["rate"].value = 8_000_000 // parallel
        trial.result.extensions["iperf3_lib.rate_intent"] = {
            "schema_version": 1,
            "intent": {"aggregate_bps_per_direction": 8_000_000},
            "resolution": {"source": "aggregate"},
        }
    assert (
        stream_scaling(
            trials, direction="client_to_server", observation="receiver", compatibility=_policy()
        ).quality
        == "complete"
    )
    del trials[0].result.extensions["iperf3_lib.rate_intent"]
    assert (
        stream_scaling(
            trials, direction="client_to_server", observation="receiver", compatibility=_policy()
        ).quality
        == "insufficient_data"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"best_fraction": 0},
        {"best_fraction": True},
        {"best_fraction": float("inf")},
        {"best_fraction": 1.1},
        {"minimum_valid_trials": 0},
        {"minimum_valid_trials": True},
    ],
)
def test_scaling_rejects_invalid_thresholds(kwargs):
    """Scaling thresholds do not silently coerce booleans or nonfinite values."""
    with pytest.raises(ValueError):
        stream_scaling(
            _trials(),
            direction="client_to_server",
            observation="receiver",
            compatibility=_policy(),
            **kwargs,
        )


def test_scaling_zero_reference_missing_counts_and_duplicate_trial_ids():
    """Zero throughput never produces an apparently adequate stream-count recommendation."""
    analysis = stream_scaling(
        _trials(amounts=(0, 0, 0, 0)),
        direction="client_to_server",
        observation="receiver",
        compatibility=_policy(),
    )
    assert analysis.best_observed_bps == 0
    assert analysis.smallest_tested_qualifying_count is None
    trials = _trials()
    trials[0].result.execution.configuration.effective["parallel"].state = "unavailable"
    assert (
        "trial-1"
        in stream_scaling(
            trials, direction="client_to_server", observation="receiver", compatibility=_policy()
        ).excluded_trials
    )
    with pytest.raises(ValueError, match="unique"):
        stream_scaling(
            [trials[1], trials[1]],
            direction="client_to_server",
            observation="receiver",
            compatibility=_policy(),
        )


def _bidir():
    result = _result(count=200, duration=1)
    result.bidirectional = True
    result.execution.method = "bidirectional"
    result.flows.extend(_result(reverse=True, count=100, duration=1).flows)
    return result


def test_simultaneous_asymmetry_keeps_observation_and_methodology_explicit():
    """Both directions come from one simultaneous run and the same endpoint observer."""
    analysis = simultaneous_asymmetry(_bidir(), observation="receiver")
    assert analysis.quality == "complete"
    assert analysis.methodology == "simultaneous_bidirectional"
    assert analysis.signed_difference_bps == 800
    assert analysis.client_to_server_over_server_to_client == 2
    assert analysis.normalized_signed_difference == 0.5
    assert simultaneous_asymmetry(_result(), observation="receiver").quality == "insufficient_data"


def test_sequential_asymmetry_requires_methods_compatibility_and_recorded_order():
    """Sequential comparisons cannot silently combine simultaneous or incompatible tests."""
    methodology = SequentialMethodology("pair-1", _policy(), "forward_then_reverse", 1)
    analysis = sequential_asymmetry(
        _result(count=200),
        _result(reverse=True, count=100),
        observation="receiver",
        methodology=methodology,
    )
    assert analysis.quality == "complete"
    assert analysis.methodology == "sequential_forward_reverse"
    assert analysis.sequential_methodology == methodology
    assert {item.trial_id for item in analysis.evidence} == {"forward", "reverse"}
    assert (
        sequential_asymmetry(
            _bidir(), _result(reverse=True), observation="receiver", methodology=methodology
        ).quality
        == "insufficient_data"
    )
    assert (
        sequential_asymmetry(
            _result(),
            _result(reverse=True),
            observation="receiver",
            methodology=replace(methodology, cooldown_seconds=None),
        ).quality
        == "partial"
    )


@pytest.mark.parametrize(
    "forward,reverse,ratio,difference", [(0, 0, None, None), (100, 0, None, 1), (0, 100, 0, -1)]
)
def test_asymmetry_zero_denominators_remain_unavailable(forward, reverse, ratio, difference):
    """Zero rates are measured facts; undefined relative statistics do not become infinity."""
    result = _bidir()
    result.flows[0].receiver.bytes = forward
    result.flows[1].receiver.bytes = reverse
    analysis = simultaneous_asymmetry(result, observation="receiver")
    assert analysis.client_to_server_over_server_to_client == ratio
    assert analysis.normalized_signed_difference == difference
    assert analysis.signed_difference_bps == 8 * (forward - reverse)


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_failed_and_incomplete_execution_is_not_a_performance_pass(status):
    """Partial measurements remain in Result without silently becoming successful analysis."""
    result = _result()
    result.ok = False
    result.execution.status = status
    assert (
        summary_throughput(result, direction="client_to_server", observation="receiver").quality
        == "insufficient_data"
    )
    assert _stability(result).quality == "insufficient_data"
    assert (
        stream_balance(result, direction="client_to_server", observation="receiver").quality
        == "insufficient_data"
    )


def test_analysis_is_native_and_raw_independent_and_does_not_mutate_input(monkeypatch):
    """Canonical calculations validate declared receipts without native loading or parsing."""
    import iperf3_lib.ffi.api as api

    def forbidden(*args, **kwargs):
        raise AssertionError("analysis must not use native or raw parsing")

    monkeypatch.setattr(api, "_dlopen", forbidden)
    monkeypatch.setattr("iperf3_lib.result.result_from_iperf_json", forbidden)
    result = _result()
    before = copy.deepcopy(result)
    _stability(result)
    summary_throughput(result, direction="client_to_server", observation="receiver")
    stream_balance(result, direction="client_to_server", observation="receiver")
    assert result == before


@pytest.mark.parametrize("field", ["parallel", "native_version", "rate", "tos"])
def test_public_compatibility_requires_observed_varying_fields(field):
    """Declaring an experimental variable or reason cannot supply missing evidence."""
    trials = _trials(counts=(2, 4), amounts=(100, 200))
    if field == "native_version":
        trials[0].result.execution.native_version = None
    else:
        trials[0].result.execution.configuration.effective[field].state = "unavailable"
    policy = _policy(
        varying_fields=("parallel", field) if field != "parallel" else (field,),
        allowed_differences={field: "Deliberate comparison."},
    )
    result = check_compatibility(trials, policy=policy)
    assert result.compatible is False
    assert result.quality == "insufficient_data"
    assert any(field in item.message for item in result.diagnostics)


def test_public_compatibility_checks_parallel_and_retains_concrete_fingerprints():
    """Baseline comparison keeps stream count fixed unless explicitly varied."""
    trials = _trials(counts=(2, 4), amounts=(100, 200))
    fixed = check_compatibility(trials, policy=_policy())
    assert not fixed.compatible
    assert fixed.fingerprints["trial-2"]["parallel"] == 2
    assert {ref.trial_id for item in fixed.diagnostics for ref in item.evidence} == {
        "trial-2",
        "trial-4",
    }
    assert check_compatibility(trials, policy=_policy(varying_fields=("parallel",))).compatible
    allowed = check_compatibility(
        trials,
        policy=_policy(allowed_differences={"parallel": "Intentional stream-count experiment."}),
    )
    assert allowed.compatible and allowed.quality == "partial"
    assert "Intentional" in allowed.diagnostics[0].message
    assert not check_compatibility([], policy=_policy()).compatible


@pytest.mark.parametrize("counts", [(2, 2), (2, 4)])
def test_aggregate_intent_cannot_hide_a_wrong_effective_rate(counts):
    """Intent is checked against native allocation even with matching target labels."""
    trials = [
        AnalysisTrial(str(index), _result(parallel=count)) for index, count in enumerate(counts)
    ]
    for trial in trials:
        parallel = trial.result.execution.configuration.effective["parallel"].value
        trial.result.execution.configuration.effective["rate"].value = 8_000_000 // parallel
        trial.result.extensions["iperf3_lib.rate_intent"] = {
            "schema_version": 1,
            "intent": {"aggregate_bps_per_direction": 8_000_000},
            "resolution": {"source": "aggregate"},
        }
    policy = _policy(varying_fields=("parallel",))
    assert check_compatibility(trials, policy=policy).compatible
    trials[1].result.execution.configuration.effective["rate"].value += 1
    result = check_compatibility(trials, policy=policy)
    assert not result.compatible
    assert any(item.code == "comparison.rate_intent_conflict" for item in result.diagnostics)


@pytest.mark.parametrize("version", ["3.19.1", "3.21"])
def test_public_compatibility_on_real_native_client_fixtures(version):
    """Captured native config supplies duration, block size, TOS and endpoint evidence."""
    import json
    from pathlib import Path

    from iperf3_lib.result import result_from_iperf_json

    raw = json.loads(
        (
            Path(__file__).parent / "fixtures" / "native" / version / "tcp-forward-client.json"
        ).read_text()
    )
    result = result_from_iperf_json(raw)
    trials = [AnalysisTrial("first", result), AnalysisTrial("second", copy.deepcopy(result))]
    compared = check_compatibility(trials, policy=_policy())
    assert compared.compatible, compared.diagnostics
    assert compared.quality == "complete"
    assert compared.fingerprints["first"]["blksize"] == raw["start"]["test_start"]["blksize"]
    assert compared.fingerprints["first"]["tos"] == raw["start"]["test_start"]["tos"]


@pytest.mark.parametrize(
    "pointers",
    [
        ["/raw/missing"],
        ["/execution/configuration/requested/parallel"],
        ["/raw"],
        ["/extensions/unnamespaced/value"],
        [],
    ],
)
def test_verified_setting_requires_an_existing_observed_receipt(pointers):
    """Setter intentions and absent receipts cannot establish effective stream count."""
    result = _result()
    result.execution.configuration.effective["parallel"].evidence_paths = pointers
    result.execution.configuration.requested = {"parallel": 2}
    assert (
        stream_balance(
            result, direction="client_to_server", observation="receiver"
        ).expected_streams
        is None
    )
    comparison = check_compatibility([AnalysisTrial("one", result)], policy=_policy())
    assert not comparison.compatible
    assert any("parallel" in item.message for item in comparison.diagnostics)


@pytest.mark.parametrize("pointer", ["", "no-slash", "/raw/~bad", 123])
def test_malformed_evidence_pointer_rejects(pointer):
    """Malformed pointers fail explicitly instead of looking like verified settings."""
    result = _result()
    result.execution.configuration.effective["parallel"].evidence_paths = [pointer]
    with pytest.raises(ValueError, match="pointer"):
        check_compatibility([AnalysisTrial("one", result)], policy=_policy())


def test_namespaced_receipt_uses_json_pointer_escaping_without_native_parsing():
    """Alternative getter receipts retain explicit provenance without a native dependency."""
    result = _result()
    result.extensions["example.getters"] = {"items/with~escape": [{"parallel": 2}]}
    result.execution.configuration.effective["parallel"].evidence_paths = [
        "/extensions/example.getters/items~1with~0escape/0/parallel"
    ]
    assert check_compatibility([AnalysisTrial("one", result)], policy=_policy()).compatible


@pytest.mark.parametrize(
    "name,value",
    [
        ("parallel", 0),
        ("parallel", 129),
        ("port", 65536),
        ("port", 0),
        ("rate", 2**64),
        ("tos", 256),
        ("duration", 86401),
        ("omit", 601),
        ("blksize", 1048577),
        ("server", ""),
    ],
)
def test_effective_configuration_range_validation(name, value):
    """Manually mutated effective values cannot bypass field-specific compatibility bounds."""
    result = _result()
    result.execution.configuration.effective[name].value = value
    with pytest.raises(ValueError):
        check_compatibility([AnalysisTrial("one", result)], policy=_policy())


@pytest.mark.parametrize(
    "field,value", [("protocol", "quic"), ("method", "parallel"), ("native_version", 1)]
)
def test_compatibility_metadata_validation(field, value):
    """Unknown configuration domains do not become compatible merely by equality."""
    result = _result()
    setattr(result if field == "protocol" else result.execution, field, value)
    with pytest.raises(ValueError):
        check_compatibility([AnalysisTrial("one", result)], policy=_policy())


def test_zero_width_interval_is_excluded_with_partial_unknown_coverage():
    """Native empty boundary observations provide no duration or fabricated zero throughput."""
    result = _result()
    result.intervals.append(
        IntervalStats(
            4,
            4,
            0,
            "client_to_server",
            "receiver",
            scope="aggregate",
            bytes=0,
            duration_seconds=0,
            omitted=False,
        )
    )
    analysis = _stability(result)
    assert analysis.quality == "partial"
    assert analysis.interval_bytes_throughput_bps == 3_500_000
    assert analysis.coverage.invalid_or_missing_count == 1
    assert analysis.coverage.measured_time_fraction is None
    assert any(item.code == "interval.zero_width" for item in analysis.diagnostics)
    result.intervals = [result.intervals[-1]]
    assert _stability(result).quality == "insufficient_data"


def test_aggregate_intent_cannot_resolve_to_native_unlimited_zero():
    """A target below stream count has no positive allocation and is incompatible."""
    result = _result(parallel=2)
    result.execution.configuration.effective["rate"].value = 0
    result.extensions["iperf3_lib.rate_intent"] = {
        "schema_version": 1,
        "intent": {"aggregate_bps_per_direction": 1},
        "resolution": {"source": "aggregate"},
    }
    compared = check_compatibility([AnalysisTrial("one", result)], policy=_policy())
    assert not compared.compatible
    assert any(item.code == "comparison.rate_intent_conflict" for item in compared.diagnostics)
    result.extensions["iperf3_lib.rate_intent"]["intent"]["aggregate_bps_per_direction"] = 2**64
    with pytest.raises(ValueError, match="uint64"):
        check_compatibility([AnalysisTrial("one", result)], policy=_policy())


def test_derived_numerical_overflow_is_explicit_and_ratios_never_infinite():
    """Finite inputs do not silently produce nonfinite outputs in derived arithmetic."""
    result = _result()
    result.flows[0].receiver.duration_seconds = 5e-324
    with pytest.raises(ValueError, match="finite"):
        summary_throughput(result, direction="client_to_server", observation="receiver")
    result = _bidir()
    result.flows[0].receiver.duration_seconds = 1e-200
    result.flows[1].receiver.duration_seconds = 1e200
    analysis = simultaneous_asymmetry(result, observation="receiver")
    assert analysis.client_to_server_over_server_to_client is None
    assert analysis.normalized_signed_difference == 1
    assert any(item.code == "ratio.out_of_range" for item in analysis.diagnostics)
