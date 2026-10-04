# Choose an iperf integration

`iperf3-lib` gives Python applications a reusable measurement API: validated
test settings, normalized results, repeatable experiments, and reports. It
combines these pieces so each application can focus on its benchmark policy
instead of building and maintaining its own command wrapper.

The upstream CLI already provides [JSON and streaming JSON output](https://software.es.net/iperf/invoking.html).
Using `subprocess` does not require scraping terminal tables. The value of
`iperf3-lib` is the tested configuration, interpretation, and experiment layer
around the native measurement engine.

## Reuse the work around each test

These features are available in **0.3.0**. They can also be built around CLI
commands; the library supplies a shared implementation and documented contracts.

| Application need | With your own command wrapper | What iperf3-lib supplies |
| --- | --- | --- |
| Configure a test | Build arguments, validate inputs, and map requested settings to native behavior. | [Validated dataclasses](../reference/configuration.md), a [native-option inventory](../reference/native-options.md), and explicit [rate intent and configuration evidence](configuration-intent.md). |
| Interpret output | Load native JSON and handle direction, endpoint roles, missing fields, and protocol/version differences. | [Normalized flows and intervals](results.md), separate sender/receiver observations, missing values kept distinct from zero, diagnostics, and retained native JSON. |
| Compare measurements | Select compatible observations, define warm-ups and repetitions, and retain failed trials. | [Trial plans and baseline assessments](trials.md), [bounded sweeps](sweeps.md), and [analysis](analysis.md) with explicit selection and data-quality rules. |
| Save and exchange evidence | Define a storage format and preserve configuration, measurements, and provenance together. | [Versioned portable artifacts](artifacts.md) and JSON, text, and JUnit assessment/sweep reports. Saved artifacts can be inspected without loading libiperf. |
| Feed monitoring | Choose metric units and labels, handle missing or failed measurements, and write snapshots safely. | [Prometheus output](prometheus.md) with validated caller labels, latest-run gauges, and atomic textfile replacement. Scheduling and label cardinality stay with your application. |

Normalization covers a documented subset of native output. Retained native
evidence and diagnostics remain available when a field cannot be interpreted
reliably. Native JSON is parsed data, not a byte-for-byte archive; streaming on
libiperf 3.19.1 reconstructs it from events and labels that provenance. A
requested setting or matching native getter does not prove the
network achieved that rate or the kernel applied that setting.

## Native integration and process ownership

The library loads the **libiperf shared library through CFFI**. It does not
launch the `iperf3` executable or parse its human-readable terminal output.
Expanded controls use libiperf's public argument parser inside an isolated
Python/CFFI worker, so that path also works without the CLI executable.

Process isolation remains part of the implementation. On current `main`, basic
synchronous native calls run in the caller and must be serialized because
libiperf has process-global state. Async operations, expanded controls, event delivery, and explicit execution
timeouts use Python worker processes. The library manages their protocol,
results, and cleanup; native traffic generation still happens in libiperf.

!!! note "Additional ownership APIs on current main"
    Changes after 0.3.0 make async cancellation await worker cleanup and any
    active callback, reporting unconfirmed cleanup as `IperfCleanupError`.
    [Owned live-event streams](live-events.md), [bounded concurrent plans](concurrent-plans.md),
    and [adaptive UDP experiments](adaptive-udp.md) also belong to the unreleased
    0.4.0 work. See the [upgrade guide](migration-0.4.md) before relying on these
    behaviors. Version 0.3.0 does not stop an executor-based async operation
    merely because its await is cancelled.

A custom subprocess wrapper can provide its own timeout, cancellation, and
cleanup policy. The library provides one shared contract for supported APIs;
forced process termination cannot prove that native C finalizers ran. See
[execution limits](../reference/compatibility.md#feature-boundaries).

## Keep existing orchestration

Ansible's [command module](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/command_module.html)
and [async execution](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_async.html)
can run and coordinate commands on remote hosts. Use Ansible to install
dependencies, prepare hosts, run a Python benchmark application, and collect
its artifacts. `iperf3-lib` supplies the measurement API inside that application;
it does not replace inventory, SSH transport, provisioning, or fleet orchestration.

You can also keep an existing CLI runner and use the library only for result
interpretation. For a **complete client-side JSON document** saved from an
`iperf3 -J` run:

```python
import json
from pathlib import Path

from iperf3_lib.result import result_from_iperf_json

raw = json.loads(Path("iperf-client.json").read_text(encoding="utf-8"))
result = result_from_iperf_json(raw, reporting_role="client")

for flow in result.flows:
    if flow.receiver is not None:
        print(flow.direction, flow.receiver.bits_per_second)

print(result.ok, result.diagnostics)
```

Importing and normalizing this saved document does not load libiperf or start a
benchmark. Use the actual reporting role for server output; a JSON event stream
is not a complete `-J` document. Imported JSON retains unknown completion time
and unverified configuration rather than inventing live-run evidence. See
[results](results.md) and [portable artifacts](artifacts.md).

## Choose what fits your workflow

- **Use a CLI command or short subprocess script** for a one-off measurement or
  an existing workflow that already provides the interpretation and reporting
  you need. It also gives direct access to CLI options outside the Python API.
- **Use iperf3-lib** when measurements become application data: repeatable tests,
  compatible baseline decisions, retained evidence, and monitoring integrations.
  Install a supported shared library and check the [tested platforms and native
  versions](../reference/compatibility.md).
- **Use Ansible with either approach** when you need to prepare or coordinate
  remote machines. Keep fleet orchestration there and choose the measurement
  interface independently.

Both approaches use iperf's native measurement engine. This project does not
claim higher throughput, more accurate network measurements, or lower execution
overhead than the CLI. The choice is about the application behavior you can reuse.
