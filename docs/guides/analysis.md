# Analyzing measurements

The `iperf3_lib.analysis` module provides standard-library calculations
over canonical results. It does not run a test, load libiperf, export metrics, or
reparse native JSON. Comparison checks resolve declared evidence pointers to
confirm receipts exist; they do not infer values from native fields.

A completed test and adequate measurement data are separate requirements. Every
analysis returns `quality`: `complete`, `partial`, or `insufficient_data`, plus
canonical evidence pointers and diagnostics. These describe data and methodology.
They do not identify physical bottlenecks or decide a performance acceptance rule.

## Measured throughput and interval stability

```python
from iperf3_lib.analysis import Selection, interval_stability, summary_throughput

received = summary_throughput(
    result, direction="client_to_server", observation="receiver"
)
print(received.quality, received.throughput_bps)

stability = interval_stability(
    result,
    selection=Selection("client_to_server", "sender", scope="aggregate"),
    threshold_bps=20_000_000,
    quantiles=(0.5, 0.95),
)
print(stability.coverage, stability.diagnostics)
```

Choose an observation that the result actually contains. A forward client's
intervals commonly describe the sender; the reverse client's intervals commonly
describe the receiver. Terminal flow summaries may contain both observations.
Direction, observer and aggregate/component-stream scope are independent choices.
`Selection(..., scope="stream", stream_id=5)` selects one run-local socket.

`summary_throughput` calculates `8 * bytes / measured_duration_seconds` for the
chosen flow endpoint. It never substitutes requested test duration, wrapper
operation time, a reported bitrate, or `summary_mbps`. Missing bytes or a zero
measured duration produces insufficient data; measured zero bytes remains zero.
Failed or incomplete executions cannot supply successful performance analysis.

For intervals with durations `d[i]` and rates `r[i]`:

| Output | Definition |
| --- | --- |
| Minimum interval-average rate | `min(r)` |
| Duration-weighted mean | `sum(d * r) / sum(d)` |
| Duration-weighted population standard deviation | `sqrt(sum(d * (r - mean)**2) / sum(d))` |
| Coefficient of variation | Standard deviation divided by mean; unavailable when mean is zero |
| Duration-weighted interval-average quantile | Smallest rate whose cumulative duration reaches the requested fraction, sorting by rate; no interpolation |
| Fraction of measured time below threshold | `sum(d where r < threshold) / sum(d)`; strict less-than |
| Interval bytes throughput | `8 * sum(bytes) / sum(d)` when every included interval has bytes |

A one-second interval at 8 Mbps and a three-second interval at 2 Mbps have a
3.5 Mbps weighted mean, a 2 Mbps weighted median, and 75% of measured time below
4 Mbps. The standard deviation is approximately 2.598 Mbps. These are statistics
of **interval-average throughput**, not packet throughput or latency percentiles.

### Omissions, missing values and coverage

Explicitly omitted warm-up intervals are excluded before overlap detection;
native warm-up clocks can overlap measured intervals. `IntervalPolicy()` excludes
unknown omission states, requires two intervals for stability, and uses a
one-microsecond boundary tolerance. Applications can explicitly choose
`unknown_omission="include"` or `derive_duration_from_bounds=True`; these decisions
are retained and the analysis becomes partial.

Bytes and measured duration determine each rate when available. A reported rate
can support partial stability when bytes are absent, but cannot establish complete
bytes throughput. A disagreement between bytes/time and reported rate is diagnosed.
Missing durations, boundaries and provenance are excluded with counts. Gaps reduce
coverage without inserting zeros. Overlapping or duplicate selected intervals make
stability insufficient. Aggregate and per-stream observations are never added
together. Unknown coverage denominators stay unavailable.

`coverage` records selected, included, omitted, unknown-omission and invalid/missing
counts, included measured seconds, byte-covered seconds, observed span and measured
time fraction. Insufficient stability can still retain descriptive statistics for
a single interval; callers must inspect quality before treating them as adequate
stability evidence.

## Stream balance and stream-count experiments

```python
from iperf3_lib.analysis import AnalysisTrial, ComparisonPolicy, stream_balance, stream_scaling

balance = stream_balance(result, direction="client_to_server", observation="receiver")

scaling = stream_scaling(
    [AnalysisTrial("one-stream", one_stream_result),
     AnalysisTrial("four-streams", four_stream_result)],
    direction="client_to_server",
    observation="receiver",
    compatibility=ComparisonPolicy("lab-run", ("client-host", "server-host")),
    best_fraction=0.95,
    minimum_valid_trials=1,
)
print(scaling.smallest_tested_qualifying_count, scaling.diagnostics)
```

Each stream uses its own endpoint byte count and measured duration. The report
includes all selected streams, valid/expected counts, coverage, minimum/maximum
rate ratio, population rate CV and Jain's fairness index
`sum(r)**2 / (N * sum(r**2))`. Missing streams remain missing; all-zero rates make
relative balance statistics unavailable. Socket IDs only identify streams within
one native run.

For mixed UDP terminal summaries whose endpoint attribution is unavailable,
`source="intervals"` explicitly selects attributable per-stream intervals. It
does not relabel the mixed end observations. The supplied interval policy also
applies to this source.

Scaling groups valid trials by verified parallel stream count and uses the median
measured flow throughput in each group. It chooses the smallest **tested** count
with median at least `best_fraction * best_observed_median`, subject to the
minimum-valid-trials requirement. Failed trials, missing evidence and insufficient
repetitions remain visible. All-zero medians yield no recommendation. There is no
interpolation of untested counts, confidence interval, or physical optimum claim.

## Reusing compatibility checks

```python
from iperf3_lib.analysis import check_compatibility

comparison = check_compatibility(
    [AnalysisTrial("current", current), AnalysisTrial("baseline", baseline)],
    policy=ComparisonPolicy("regression-check", ("client-host", "server-host")),
)
print(comparison.compatible, comparison.fingerprints, comparison.diagnostics)
```

`ComparisonPolicy` records caller-owned group and endpoint identities. Concrete
fingerprints compare protocol, execution method, native version/system information,
server/port, configured duration/omit, parallel streams, native rate, block size,
TOS, and rate intent. Verified settings require an existing receipt under `raw` or
a namespaced `extensions` object. Request values and successful setter calls are
insufficient. Native environment strings include host identity; deliberate changes
need an explicit policy reason.

`varying_fields=("parallel",)` declares a measured experimental variable: its
values must be known, but equality is not required. `allowed_differences` maps
field names to nonempty reasons, for example `{"native_version": "Intentional
version comparison"}`. An allowed observed difference makes quality partial and
retains its reason. Neither mechanism supplies missing evidence. Baseline checks
keep parallel count fixed by default; scaling automatically varies parallel,
and sequential asymmetry automatically varies execution method.

When the rate-intent extension records the same aggregate target per direction,
per-stream rates may differ only if every verified rate equals
`aggregate_target // verified_parallel`. Fixed per-stream and fixed aggregate
experiments retain different intent fingerprints. Compatibility alone does not
establish execution success or adequate throughput measurements.

### Advanced configuration in comparisons

An advanced `ClientConfig` field becomes part of every trial's comparison
fingerprint when any retained request gives it a nondefault value, or when
`ComparisonPolicy.varying_fields` or `allowed_differences` explicitly names it.
This includes `mptcp` and `json_stream`. Every compared trial then needs verified
native observations for that field, including trials that requested its default.
The request selects what to compare; the returned native value or matching
getter receipt supplies the observation.

For example, comparing a `no_delay=True` trial with a default-configured trial
requires retained evidence of both native no-delay values. An absent receipt
for the default-configured peer cannot be replaced with an assumed `False`.
Equal requests also cannot fill missing observations. A known difference must
be a declared varying field or have a reason in `allowed_differences`; neither
policy permits unknown values.

Options accepted by the native parser may lack a returned value or exposed
getter. Those runs remain available for inspection, but comparisons requiring
that observation are incompatible. Request validity, execution success and
comparison eligibility are separate conclusions. These rules apply equally to
retained baselines and advanced settings fixed across sweep cells.

Default-only comparisons retain their existing fingerprints. Archived v1
reports validate the recorded advanced fields with frozen compatibility rules;
reading an old report does not add fields from a newer `ClientConfig`.

## Directional asymmetry

`simultaneous_asymmetry(result, observation="receiver")` requires both directions
from one explicitly bidirectional run. `sequential_asymmetry(forward, reverse,
observation="receiver", methodology=...)` requires separate forward and reverse
runs and a `SequentialMethodology(pair_id, comparison_policy, execution_order,
cooldown_seconds)`. Execution order is `forward_then_reverse` or
`reverse_then_forward`. Unknown cooldown is explicit `None` and makes quality
partial. Caller/runner provenance establishes order; wall clocks alone do not.

Both APIs use the same chosen observation for both directions. With measured
rates `F` and `R`, they report `F - R`, `F / R` when `R > 0`, and
`(F - R) / max(F, R)` when either is positive. Undefined or out-of-range ratios
remain unavailable. Sequential and simultaneous methodologies remain distinct in
the output. Neither compares a sender observation to a receiver observation.

## TCP and endpoint CPU evidence

`IntervalStats.tcp`, per-stream sender `SumStats.tcp`, and `Result.cpu` preserve
qualified evidence independently of analysis. RTT fields are seconds; congestion
window, advertised send window and path MTU are bytes. TCP interval RTT is a
smoothed local TCP sample. Native min/max/mean RTT summaries describe sampled TCP
information, not packet latency percentiles. Source-defined `-1` getter values
become unavailable with explicit availability evidence. See the native
[TCP getter units](https://github.com/esnet/iperf/blob/3.21/src/tcp_info.c#L135-L231)
and [summary sampling](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L3527-L3634).

Normalization is qualified against Linux output from libiperf 3.19.1 and 3.21.
TCP information belongs to the local socket. Only attributable local sender
observations are normalized; remote sender entries can contain local receiver
TCP values or placeholders. Unknown producers, platforms or contradictory sender
provenance retain raw evidence and explicit uncertainty. The retained fixtures
cover both versions and both reporting endpoints.

CPU values describe the iperf process, retaining endpoint identity and local/remote
provenance. Total/user/system percentages can exceed 100% because process CPU
time can accumulate across threads. The native implementation divides process CPU
time by elapsed time ([source](https://github.com/esnet/iperf/blob/3.21/src/iperf_util.c#L179-L212)).
Qualified local CPU values are retained. Final remote CPU is established in client
output; server output's remote fields are kept unknown. Unestablished endpoint
identity remains `"unknown"`. Each present measurement has an evidence pointer.

TCP retransmissions are not an exact packet-loss percentage. UDP jitter is not
application latency. CPU, RTT and window observations are facts with provenance;
these APIs produce no speculative root-cause hypotheses.
