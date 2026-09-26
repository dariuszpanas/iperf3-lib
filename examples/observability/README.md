# Local Prometheus and Grafana qualification

This example runs real, bounded libiperf benchmarks through the complete path:

```text
Python Client -> loopback iperf3 server -> atomic .prom file
    -> node_exporter textfile collector -> Prometheus -> Grafana
```

It uses Docker Desktop's existing Kubernetes cluster and the dedicated
`iperf3-lib-observability` namespace. All benchmark traffic stays on loopback
inside one pod. No benchmark runs on a scrape or on a schedule: you explicitly
start each batch. The dashboard shows latest-run snapshots and their history,
not physical network capacity.

## Prerequisites and build

Enable Kubernetes in Docker Desktop and install `kubectl`, Docker, uv, and make.
Run the following commands from the repository root. They use the explicit
`docker-desktop` context without changing your current context.

First verify that the namespace is not already owned by another run:

```bash
kubectl --context docker-desktop get namespace iperf3-lib-observability --ignore-not-found
```

If it exists, inspect it before reusing it. For a new stack, run the non-native
gates first, then build and test the native image. The default native version is
3.21; repeat `make docker-test IPERF3_VERSION=3.19.1 DOCKER_IMAGE=iperf3-lib-test:min`
to exercise the minimum supported version as well.

```bash
uv sync --frozen --dev
uv run make check test workflow-lint
uv run make docker-test DOCKER_IMAGE=iperf3-lib-test:local
docker build --file examples/observability/Dockerfile --build-arg BASE_IMAGE=iperf3-lib-test:local --tag iperf3-lib-observability:dev .
kubectl --context docker-desktop apply --dry-run=client -k examples/observability
kubectl --context docker-desktop apply -k examples/observability
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/benchmark --timeout=120s
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/prometheus --timeout=120s
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/grafana --timeout=120s
```

The example image copies the current source tree over the locally built native
image. It uses `imagePullPolicy: Never`, so it cannot silently fetch a different
image. Docker Desktop Kubernetes and Docker share the local image store.
After source changes, repeat the example image build and restart only its pod:

```bash
kubectl --context docker-desktop -n iperf3-lib-observability rollout restart deployment/benchmark
kubectl --context docker-desktop -n iperf3-lib-observability rollout status deployment/benchmark --timeout=120s
```

Restarting that pod clears its ephemeral metric and evidence files. Start new
port forwards after any rollout that replaces the target pod.

## Open local endpoints

Run each command in a separate terminal. Port forwards bind only to loopback:

```bash
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/grafana 13000:3000
```

```bash
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/prometheus 19090:9090
```

```bash
kubectl --context docker-desktop -n iperf3-lib-observability port-forward --address 127.0.0.1 service/benchmark 19100:9100
```

Open the [Grafana dashboard](http://127.0.0.1:13000/d/iperf3-lib-observability).
Grafana has anonymous **Viewer** access for this local example. The services use
ClusterIP, with no ingress or NodePort. This configuration is intended for local
qualification; choose authentication and network access controls before using
it as a shared monitoring service.

## Generate actual results

```bash
kubectl --context docker-desktop -n iperf3-lib-observability exec deployment/benchmark -c benchmark -- python /app/examples/observability/benchmark.py run --scenario all
```

The batch takes about 50 seconds and performs five successful two-second runs:

| Profile | Native request |
| --- | --- |
| `tcp-forward` | Two TCP streams from client to server |
| `tcp-reverse` | Two TCP streams from server to client |
| `tcp-bidirectional` | Two TCP streams in each direction simultaneously |
| `udp` | One UDP stream with 1,200-byte datagrams |
| `transition` | Two TCP streams, then a connection to unused loopback port 5202 |

Every successful run requests 4,000,000 bits/s **per stream**. Runs are
serialized by a file lock. Six-second pauses allow the two-second scrape
schedule to capture each stage. Actual transferred bytes and measurements are
retained in the returned native JSON. The final deliberate failure replaces
`transition.prom` using the same stable labels: success becomes zero, the
completion time advances, the last-success timestamp remains, and throughput
from the prior run disappears from current queries.

UDP loss can be zero on loopback. Its measured ratio and jitter are still
checked against the native result; this example does not inject artificial
packet loss. Sender and receiver observations remain separate throughout.

`--scenario` also accepts one of the profile names above or `failure`. For a
manual success-to-failure experiment, run `transition`, observe a scrape, and
then run `failure`. The default container command only waits; it generates no
traffic itself.

## Verify the complete path

After the batch finishes and with all three port forwards active:

```bash
uv run python examples/observability/verify.py --output .vault/observability-validation.json
```

The verifier fails unless:

- Native JSON confirms the requested protocol, duration, stream count, rate,
  reverse flag, and bidirectional flag.
- node_exporter accepts the textfiles and Prometheus has a healthy, fresh scrape.
- Every expected sample matches native JSON in both Prometheus's instant-query
  API and Grafana's `POST /api/ds/query` dashboard API, including exact labels,
  bits-to-bytes, percent-to-ratio, and milliseconds-to-seconds conversions.
- No samples are missing, duplicated, nonfinite, or unexpected.
- The failed profile retains its previous last-success time and has no current
  throughput sample.
- Grafana has provisioned the expected dashboard.

Evidence older than 15 minutes fails by default; run another explicit batch to
refresh it. The `--max-run-age` option changes that evidence-age threshold, but
the current-scrape freshness check remains in place. The optional output receipt
contains native JSON and the independently queried vectors. The `.vault/` path
is ignored by Git.

Current status and freshness panels use instant queries. Throughput, loss, and
jitter plots show history: a previous successful sample remains in historical
queries after a failed run, while no current throughput value is published for
that failed profile. Check the rendered dashboard as well as the API receipt.

## Isolation and reproducibility

- All containers run without root, privilege escalation, service-account
  tokens, or added Linux capabilities. Root filesystems are read-only.
- There are no host mounts or host networking. Bounded `emptyDir` volumes hold
  textfiles, native temporary stream files, and ephemeral monitoring data.
- Declared container limits total 1.7 CPU cores and 1,984 MiB for the three
  steady-state pods. Prometheus retains at most two hours or 128 MB of samples.
- Prometheus 3.15.0, node_exporter 1.12.1, and Grafana 13.2.2 images are pinned by
  digest. Grafana uses its bundled plugins with automatic installation and
  updates disabled, so startup does not replace the pinned plugin files.

The manifest flags follow the official
[node_exporter textfile collector documentation](https://github.com/prometheus/node_exporter#textfile-collector),
[Prometheus scrape configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[Grafana provisioning documentation](https://grafana.com/docs/grafana/latest/administration/provisioning/),
and [Grafana plugin update settings](https://grafana.com/docs/grafana/latest/datasources/prometheus/#plugin-updates).

## Cleanup

Stop the three port-forward terminals with Ctrl+C, then remove only this
example's namespace:

```bash
kubectl --context docker-desktop delete namespace iperf3-lib-observability
```

This removes its deployments, services, generated ConfigMaps, and ephemeral
metrics and dashboards. It leaves other namespaces and the local Docker images
untouched.
