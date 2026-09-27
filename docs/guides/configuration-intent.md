# Rate intent, budgets, and capabilities

These APIs are available in 0.3.0. See [installation](../getting-started.md)
for package setup.

## Choose what the rate means

`ClientConfig.rate` keeps its existing meaning: bits per second **per stream**.
`RateIntent` makes either per-stream or aggregate-per-direction intent explicit:

```python
from iperf3_lib import Client, ClientConfig
from iperf3_lib.intent import RateIntent, parse_rate, resolve_rate

config = ClientConfig("192.0.2.10", parallel=3, bidirectional=True)
intent = RateIntent(aggregate_bps_per_direction=parse_rate("10 Mbit/s"))
allocation = resolve_rate(config, intent)
print(allocation.native_per_stream_bps)       # 3_333_333
print(allocation.unused_bps_per_direction)    # 1
print(allocation.aggregate_bps_all_directions)  # 19_999_998

result = Client(config, rate_intent=intent).run()
```

The resolver divides the aggregate target by `parallel`, rounding down. All
streams receive the same native rate. Any remainder stays unused; it is not
assigned to one stream. In simultaneous bidirectional mode, the same aggregate
target applies independently to each direction, so the total target doubles.
Forward and reverse runs have one active traffic direction.

Specify exactly one field in `RateIntent`: `per_stream_bps` or
`aggregate_bps_per_direction`. Combining an intent with `ClientConfig.rate`
raises `ValueError`, even when the values agree. Rates must be integers rather
than booleans or floats, between zero and `2**64 - 1`; aggregate intent must be
positive and at least the number of parallel streams.

Native rate zero disables pacing. To request unlimited traffic explicitly, use
`RateIntent(per_stream_bps=0)` or `ClientConfig(rate=0)`. Aggregate zero is
rejected so it cannot accidentally request unlimited traffic. TCP and SCTP
without a rate retain native unlimited defaults. UDP without a rate resolves
to 1,048,576 bits/s per stream. Unlimited aggregate estimates are `None`.

These are pacing targets. Returned measurements may differ due to transport,
kernel behavior, network conditions, and scheduling. A target is not a hard
traffic ceiling.

## Exact unit inputs

`parse_rate(text)` converts explicitly labeled quantities to whole integer
bits/s before constructing a config or intent. It never changes low-level
`ClientConfig` validation.

| Input | Result |
| --- | --- |
| `"1.5 Mbit/s"` | `1_500_000` |
| `"2 MB/s"` | `16_000_000` |
| `"0.125 B/s"` | `1` |
| `"0 bit/s"` | `0` (unlimited when used as a native per-stream rate) |

Supported decimal SI prefixes are none, `k`, `M`, `G`, and `T`; suffixes are
`bit/s`, `bps`, or `B/s`. Case distinguishes bits and bytes. The parser rejects
missing units, binary prefixes, exponent notation, negative values, fractional
resulting bits, and values above the native integer limit. Conversion uses exact
integer arithmetic.

## Estimate a sequential plan

```python
from iperf3_lib.intent import estimate_plan

estimate = estimate_plan(
    [config],
    [intent],
    max_payload_bytes=30_000_000,
    max_active_seconds=10,
)
print(estimate.active_seconds, estimate.estimated_payload_bytes)
```

The estimator accepts finite sequences and returns one `RunEstimate` per
configuration, plus totals. Intent sequences must have the same length; an
entry of `None` uses the existing config rate or protocol default. It includes
`duration + omit`, since warm-up generates traffic, and counts every active
direction. Total estimated bytes round upward from total intended payload bits.
An empty plan has zero estimated cost.

Count-terminated and unlimited-duration configurations have unknown active-time
and payload estimates. A finite cap rejects the unknown quantity. Finite trial
plans reject unlimited duration; count-based trials need explicit uncapped
estimate budgets. Sweeps continue to require a finite active-time budget.

Budgets are admission checks on these estimates. Exceeding either raises
`ValueError`; an unlimited/unknown rate cannot satisfy a finite payload budget.
The estimator does not execute a plan, validate host capabilities, or interrupt
a native call. Connection setup, delayed peers, shutdown, network overhead, and
native rate overshoot are outside the estimate. Actual elapsed operation time
and measured traffic belong to the returned results, not this estimate.

For observed totals, choose one endpoint observation per direction. Do not add
sender and receiver bytes for the same transfer, or aggregate intervals and
their component streams. End summaries can omit warm-up; compare like scopes.

## Preserve intent and native evidence

Each `Client.run()` snapshots and validates the caller's config, resolves rate
intent once, and executes a detached low-level config. Canonical
`execution.configuration.requested` records that **resolved** configuration,
including the derived rate. `effective` remains independently verified by
returned native settings. Intent resolution cannot turn a request into native
verification.

The result extension `iperf3_lib.rate_intent` has `schema_version=1` and contains:

- `caller_config`: original low-level request before rate resolution.
- `intent`: the two explicit intent fields, or `None` for legacy/default use.
- `resolution`: native per-stream rate, realized aggregate targets per direction
  and across directions, unused remainder, active directions, and `source`
  (`legacy`, `per_stream`, `aggregate`, or `protocol_default`).

This extension is retained by [versioned artifacts](artifacts.md), including
failed results returned after native execution. Admission errors raise before
native allocation. The caller's mutable config is not modified by resolution.

## Inspect capability evidence

```python
from dataclasses import asdict
from iperf3_lib.capabilities import get_capabilities

offline = get_capabilities(probe_native=False)
print(asdict(offline))

# Explicitly inspect the local library's version and symbols, without traffic.
report = get_capabilities(result=result)
print(report.library.state, report.library.version)
print(report.execution.status, report.execution.verified_settings)
```

Importing `iperf3_lib.capabilities` and requesting an offline report do not load
libiperf. An explicit native probe reads version/symbols; it does not allocate a
test, alter settings, or generate traffic.

| Layer | Meaning |
| --- | --- |
| `library.state` | `unprobed`, `available`, `unavailable` (load failure), or `error` (unexpected probe failure). Diagnostics retain the reason. |
| `feature.wrapper` | Implemented `supported`, explicitly `unsupported`, or evaluated `unimplemented`. |
| `symbol.declared` | Whether this wrapper declares the symbol in its CFFI ABI. |
| `symbol.state` | `present`, `absent`, or `unknown`. Undeclared and unprobed symbols are unknown. |
| Tested versions/platforms | The explicit qualification matrix, separate from the current host/library version. A symbol does not qualify a new platform. |
| `feature.constraints` | Kernel, protocol, execution, or qualification limitations. |
| `feature.runtime` | `not_run`: a capability probe does not benchmark features. |
| `execution` | Optional supplied result's outcome, native version, protocol, and verified setting names. This may come from another machine/library. It does not upgrade static feature claims. |

Legacy `HAS_*` flags remain available and resolve when accessed. Native-symbol
flags still collapse lookup/load failures to false; use the report when those
distinctions matter. Parser-backed worker controls are distinct from dedicated
setter probes. SCTP and MPTCP still depend on the native build and kernel. Basic
direct calls remain non-reentrant; cancelling an await alone does not stop
traffic. An explicit execution timeout selects the isolated worker.

## Profiles and native option decisions

Keep named profiles in the consuming application, with explicit configuration
and rate intent. For example, an application can own a versioned factory named
`branch_office_upload_v1` that returns a `ClientConfig` and `RateIntent`. Record
its name under an application namespace such as `example.profile` in
`result.extensions`. Application factories receive normal strict validation.
The library does not ship opinionated built-in rates, durations, or protocol
profiles; suitable values depend on the environment. Asymmetric simultaneous
direction budgets are deferred because the current native configuration has
one shared per-stream rate.

The [native option inventory](../reference/native-options.md) now accounts for
every tagged parser option in 3.19.1 and 3.21. Expanded controls are exposed
through typed configuration and an isolated Python/CFFI worker; the worker uses
the public native parser where no dedicated setter exists. There is no raw CLI
argument passthrough or `iperf3` executable requirement.

| Option | Interpretation and evidence boundary |
| --- | --- |
| Pacing timer | A stored microsecond interval is not a packet-spacing guarantee. |
| Socket buffers | Native getter receipts preserve requests; kernel send/receive sizes can differ. |
| Congestion control | Algorithm request and actual kernel behavior remain separate evidence. |
| Server output | The remote server determines whether its output is JSON or text. |
| Socket pacing (`fq-rate`) | Typed worker configuration uses the public parser; availability and effective pacing depend on the platform. |
| MPTCP / live JSON | Worker-backed controls preserve native build/kernel limits and bounded event-delivery semantics. |

See the versioned public headers for
[3.19.1](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.h) and
[3.21](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.h), the
[native rate and bidirectional manual](https://github.com/esnet/iperf/blob/3.21/src/iperf3.1#L313-L424),
and [TCP socket buffer observations](https://github.com/esnet/iperf/blob/3.21/src/iperf_tcp.c#L464-L516).
Public symbol presence establishes an API entry point, not successful operating
system behavior. Every future accepted option must be applied once or rejected,
and qualified using returned JSON or the matching native getter.
