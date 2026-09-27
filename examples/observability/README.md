# Prometheus and Grafana example

Run bounded libiperf benchmarks and view the results through this pipeline:

```text
Python Client -> loopback iperf3 server -> atomic .prom file
    -> node_exporter textfile collector -> Prometheus -> Grafana
```

Use any chosen Kubernetes context with Linux nodes: kind, Colima, or a work
cluster. All test traffic stays on loopback inside the benchmark pod. Tests run
only when you request them; scraping and dashboard refresh generate no traffic.

## Choose a context

These commands use a POSIX shell on Linux or macOS, from the repository root.
Install `kubectl`, uv, and Docker for the image build below. A containerd cluster
can import the built image from another machine or pull it from a registry.
Choose an existing cluster:

```sh
kubectl config get-contexts
export KUBE_CONTEXT="$(kubectl config current-context)"
# Or: export KUBE_CONTEXT=your-context-name
export OBS_NAMESPACE=iperf3-lib-observability

kubectl --context "$KUBE_CONTEXT" get namespace "$OBS_NAMESPACE" --ignore-not-found
uv sync --frozen --dev
```

Use the dedicated namespace above; inspect an existing namespace before reusing
it. You need permission to create the example's namespace and workloads, exec
into its benchmark container, and port-forward its services. Passing
`--context` selects the target without changing the current kubeconfig context.
See [Kubernetes context selection](https://kubernetes.io/docs/tasks/access-application-cluster/configure-access-multiple-clusters/).

The services use ClusterIP and no ingress. Grafana permits anonymous Viewer
access; Prometheus and node_exporter also have no authentication. Other cluster
workloads may reach those services even though the local port forwards bind to
loopback. In a shared cluster, use a suitable sandbox and your cluster's network
and access policies.

## Build and deliver the image

The two benchmark containers use `iperf3-lib-observability:dev` with
`imagePullPolicy: Never`. Building on your laptop alone does not put that image
on every Kubernetes node. Choose the matching delivery path below.

### Docker build

For kind, a registry-based cluster, or Colima with the Docker runtime:

```sh
docker build -t iperf3-lib-test:local .
docker build -f examples/observability/Dockerfile \
  --build-arg BASE_IMAGE=iperf3-lib-test:local \
  -t iperf3-lib-observability:dev .
```

The first image builds libiperf; the second adds the example runner.
Build for the Linux architecture of your cluster's nodes. The commands use
the builder's default architecture; an ARM laptop image will not run on an
AMD64-only cluster without a matching image build.

### kind

For a kind cluster named `observability` (context `kind-observability`), load
the image into its nodes after building:

```sh
export KUBE_CONTEXT=kind-observability
kind load docker-image iperf3-lib-observability:dev --name observability
```

Use your existing cluster's name. To create a new one first, use
`kind create cluster --name observability`. The
[kind image-loading guide](https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster)
explains loading into named clusters and local-image pull policies.

### Colima

With Colima's **Docker runtime**, build using that profile's Docker engine.
For the default profile, `export DOCKER_CONTEXT=colima` selects it for the Docker
commands above; choose its Kubernetes context separately with `KUBE_CONTEXT`.
A new default profile can be started with `colima start --kubernetes`.

With Colima's **containerd runtime**, Kubernetes uses the `k8s.io` image
namespace. Export the completed image from your Docker builder, copy the archive
to the Colima host if needed, and import it there:

```sh
# On the Docker builder:
docker save -o iperf3-lib-observability.tar iperf3-lib-observability:dev
# On the Colima host:
colima nerdctl --namespace k8s.io load < iperf3-lib-observability.tar
```

A new containerd profile can be started with
`colima start --runtime containerd --kubernetes`. Use the matching profile for
all commands. See [Colima's runtime and image guidance](https://github.com/abiosoft/colima#kubernetes)
and [nerdctl image loading](https://github.com/containerd/nerdctl#usage).
Docker images in another engine are not automatically available to containerd.

### Work or remote cluster: registry overlay

Push a uniquely tagged image to a registry reachable by your cluster:

```sh
export REGISTRY_REPO=registry.example.com/your-team/iperf3-lib-observability
export IMAGE_TAG=example-1
docker tag iperf3-lib-observability:dev "$REGISTRY_REPO:$IMAGE_TAG"
docker push "$REGISTRY_REPO:$IMAGE_TAG"
```

Replace the registry and tag with your own. Create a temporary Kustomize overlay
that changes both benchmark containers to registry pulls:

```sh
overlay_dir="$(mktemp -d)"
cp -R examples/observability "$overlay_dir/base"
cat > "$overlay_dir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - base
images:
  - name: iperf3-lib-observability
    newName: $REGISTRY_REPO
    newTag: "$IMAGE_TAG"
patches:
  - target:
      kind: Deployment
      name: benchmark
    patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: benchmark
      spec:
        template:
          spec:
            containers:
              - name: benchmark
                imagePullPolicy: IfNotPresent
              - name: iperf-server
                imagePullPolicy: IfNotPresent
EOF
export OBS_MANIFESTS="$overlay_dir"
```

For a private registry, add your namespace's `imagePullSecrets` to the overlay's
pod spec. Use a new tag for each rebuild, or pin the pushed digest.
The tracked base files stay unchanged. See
[Kustomize image and patch customization](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/#customizing)
and [Kubernetes image pull policies](https://kubernetes.io/docs/concepts/containers/images/#image-pull-policy).

## Deploy

For kind or Colima's local image path, select the base manifests:

```sh
export OBS_MANIFESTS=examples/observability
```

For a registry, retain the overlay path set above. Review and apply it:

```sh
kubectl kustomize "$OBS_MANIFESTS"
kubectl --context "$KUBE_CONTEXT" apply -k "$OBS_MANIFESTS"
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" rollout status deployment/benchmark --timeout=120s
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" rollout status deployment/prometheus --timeout=120s
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" rollout status deployment/grafana --timeout=120s
```

The deployments select Linux nodes and use ephemeral storage. Declared limits
total 1.7 CPU cores and 1,984 MiB of memory, in addition to cluster overhead. The monitoring images are
pinned by digest in `stack.yaml`. The benchmark container starts idle.

## Open the dashboard

Run these commands in three terminals, setting the same `KUBE_CONTEXT` and
`OBS_NAMESPACE` in each:

```sh
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" port-forward --address 127.0.0.1 service/grafana 13000:3000
```

```sh
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" port-forward --address 127.0.0.1 service/prometheus 19090:9090
```

```sh
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" port-forward --address 127.0.0.1 service/benchmark 19100:9100
```

Open the [Grafana dashboard](http://127.0.0.1:13000/d/iperf3-lib-observability).
Its Prometheus datasource and panels are provisioned automatically.

## Run benchmarks and verify the metrics

```sh
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" exec deployment/benchmark -c benchmark -- \
  python /app/examples/observability/benchmark.py run --scenario all
uv run --frozen python examples/observability/verify.py --context "$KUBE_CONTEXT" \
  --output .vault/observability-validation.json
```

The batch takes about 50 seconds and includes these two-second successful tests:

| Profile | Native request |
| --- | --- |
| `tcp-forward` | Two TCP streams from client to server |
| `tcp-reverse` | Two TCP streams from server to client |
| `tcp-bidirectional` | Two TCP streams in each direction simultaneously |
| `udp` | One UDP stream with 1,200-byte datagrams |
| `transition` | Two TCP streams, then a deliberate failure on unused loopback port 5202 |

Successful tests request 4,000,000 bits/s per stream. Runs are serialized;
six-second pauses let the two-second scrape schedule capture the changes.
You can also pass an individual profile to `--scenario`, or `failure` after
`transition` to inspect the success-to-failure behavior separately.

The verifier compares saved native JSON with Prometheus and Grafana API samples:
directions, sender/receiver observations, units, scrape freshness, and textfile
parsing must match. Missing, duplicate, unexpected, or incorrect samples fail.
The failed profile must retain its last-success time and publish no current
throughput; historical successful samples remain visible in the plots.

Without `--context`, the verifier resolves the current kubeconfig context once.
It prints and records that context and the dedicated namespace. Its default
HTTP endpoints are the three forwards above; `--prometheus-url`, `--grafana-url`,
and `--node-exporter-url` override them. Forward all three from the same context
used to read the native receipts.

Results older than 15 minutes fail verification; rerun the batch to refresh
them, or set `--max-run-age` deliberately. The optional JSON output includes
native results and queried vectors. `.vault/` is ignored by Git.
Check the rendered panels too: loopback throughput demonstrates the pipeline,
not an external network's capacity.

## Update and clean up

After source changes, rebuild and deliver the image again. For registry
deployments, update the overlay to a new tag and apply it. For local images,
reload the image into the nodes and restart the benchmark deployment:

```sh
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" rollout restart deployment/benchmark
kubectl --context "$KUBE_CONTEXT" -n "$OBS_NAMESPACE" rollout status deployment/benchmark --timeout=120s
```

Restarting clears that pod's metric files. Reconnect affected port forwards and
run another batch.

To remove the example, stop the forwards with Ctrl+C and delete its dedicated
namespace:

```sh
kubectl --context "$KUBE_CONTEXT" delete namespace "$OBS_NAMESPACE"
```

This removes the example's workloads and stored measurements.
