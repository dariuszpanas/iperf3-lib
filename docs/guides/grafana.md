# View benchmark results in Grafana

Use the [Prometheus exporter](prometheus.md) to publish completed benchmark
results, then query them from Grafana:

```text
Python Client + libiperf -> atomic .prom file -> node_exporter
                                              -> Prometheus -> Grafana
```

The [runnable Kubernetes example](https://github.com/dariuszpanas/iperf3-lib/tree/main/examples/observability)
includes a benchmark runner, textfile collector, Prometheus datasource, and
provisioned Grafana dashboard. It works with a chosen kubeconfig context on
a cluster with Linux nodes, including kind, Colima, or a work cluster.
Follow its README for image building and delivery, deployment, port forwards,
and cleanup.

## Try the example

The example uses the dedicated `iperf3-lib-observability` namespace. After
following the setup instructions, select the same context in your Linux or
macOS shell and start a batch:

```sh
export KUBE_CONTEXT="$(kubectl config current-context)"
# Or: export KUBE_CONTEXT=your-context-name

kubectl --context "$KUBE_CONTEXT" -n iperf3-lib-observability exec \
  deployment/benchmark -c benchmark -- \
  python /app/examples/observability/benchmark.py run --scenario all

uv run --frozen python examples/observability/verify.py --context "$KUBE_CONTEXT"
```

The verifier defaults to the current kubeconfig context when `--context` is
omitted. It prints the selected context and namespace; `--output PATH` saves
them with the native results and queried metrics. Keep the port forwards pointed
at that same context.

Open the [Grafana dashboard](http://127.0.0.1:13000/d/iperf3-lib-observability)
while the example's Grafana port forward is active.

## Read the dashboard

The batch runs TCP forward, reverse, bidirectional, and UDP tests, followed by
a successful test and deliberate connection failure under the same
`transition` label.

- **Current status** shows whether the latest run succeeded and when it completed.
- **Last success** retains the previous successful completion after a failed run.
- **Throughput** separates each traffic direction and sender/receiver observation.
  Failed runs publish no current throughput; earlier values remain in history.
- **UDP loss and jitter** show their own measurements, separate from throughput.

The verifier compares actual native JSON with Prometheus and Grafana query
results, checks units and labels, and rejects stale or missing samples.
Run another explicit batch if results are more than 15 minutes old.
Scraping and dashboard refresh never start network tests.

All example traffic stays on loopback inside one pod. These values demonstrate
the monitoring integration; use your own endpoints and benchmark plan to
measure a network. For an existing monitoring deployment, start with
[writing textfiles](prometheus.md) and adapt the example's
[dashboard JSON](https://github.com/dariuszpanas/iperf3-lib/blob/main/examples/observability/dashboard.json).
