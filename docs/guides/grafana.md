# Validate the Grafana integration

The [local observability example](https://github.com/dariuszpanas/iperf3-lib/tree/main/examples/observability)
exercises the complete path:

```text
Python Client + libiperf -> atomic .prom file -> node_exporter
                                              -> Prometheus -> Grafana
```

It uses the existing **Docker Desktop Kubernetes** context and a dedicated
`iperf3-lib-observability` namespace. Services use ClusterIP and are accessed
through loopback port forwards. There is no ingress, host-network access, or
dependency on other applications in the cluster.

## What this establishes

The fixture runs two-second native loopback tests for TCP forward, reverse,
bidirectional, and UDP traffic. It then runs a successful test followed by an
intentional connection refusal using the same `transition` profile.

The verifier compares returned native JSON with the actual samples queried from
Prometheus and Grafana, including direction, observation point, and unit
conversions. It checks fresh scrapes, textfile parsing, success/failure state,
retention of the last-success timestamp, and absence of throughput after failure.
Missing, extra, duplicate, non-finite, or incorrect values fail the check.

This validates delivery and interpretation of benchmark results. Loopback
throughput does not measure an external network's capacity.

## Start the fixture

Requirements: Docker Desktop with Kubernetes enabled, the `docker-desktop`
context, `kubectl`, and the repository development environment. From the
repository root:

```bash
make docker-build DOCKER_IMAGE=iperf3-lib-test:local
uv run --frozen python scripts/docker_validate.py build --dockerfile examples/observability/Dockerfile --image iperf3-lib-observability:dev
kubectl --context docker-desktop apply -k examples/observability
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/benchmark
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/prometheus
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/grafana
```

The fixture stays idle until you explicitly request a benchmark. Native calls
are serialized, use two streams for TCP and one for UDP, and request 4 Mbit/s
per stream. Containers have bounded CPU, memory, and ephemeral storage.

Open three terminals for the local service forwards:

```bash
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/grafana 13000:3000
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/prometheus 19090:9090
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/benchmark 19100:9100
```

The local Grafana instance grants anonymous **Viewer** access to this disposable
fixture. Its datasource and dashboard are provisioned from the example files.
It is not a production deployment template.

## Run and verify

```bash
kubectl --context docker-desktop -n iperf3-lib-observability exec deployment/benchmark -c benchmark -- python /app/examples/observability/benchmark.py run --scenario all
uv run --frozen python examples/observability/verify.py
```

Open the [local Grafana dashboard](http://127.0.0.1:13000/d/iperf3-lib-observability).
Confirm that panels have rendered data, the transition profile shows failure,
and its last-success age is older than its latest-completion age. Historical
throughput remains visible for earlier successful runs; current failure samples
contain no throughput. Review loss and jitter separately from throughput.

The verifier exits unsuccessfully when benchmark evidence is too old. Run the
scenarios again to refresh it. Save a JSON receipt with `--output PATH`; use an
ignored location for transient local evidence. Scraping and dashboard refresh
never trigger more traffic.

## Update or stop

After changing runtime code, rebuild the example image and restart the benchmark
deployment so the running pod uses the new code. Reconnect affected port forwards
after any deployment rolls. A successful dashboard HTTP response alone does not
prove that its datasource can query metrics; rerun the verifier and inspect the
rendered panels.

The [example README](https://github.com/dariuszpanas/iperf3-lib/blob/main/examples/observability/README.md)
contains detailed setup, version information, and cleanup commands. Its storage
is ephemeral. Deleting only the `iperf3-lib-observability` namespace removes this
fixture and its stored measurements.

Remaining result-contract work is tracked in [#27](https://github.com/dariuszpanas/iperf3-lib/issues/27),
and exporter qualification in [#29](https://github.com/dariuszpanas/iperf3-lib/issues/29).
