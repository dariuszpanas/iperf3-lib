"""Check that the live-stack verifier compares native measurements and rejects bad evidence."""

import copy
import io
import json
import subprocess
from pathlib import Path

import pytest

from examples.observability import verify
from examples.observability.verify import (
    NAMESPACE,
    PROFILES,
    check_vector,
    expected_metrics,
    grafana_vector,
    read_receipt,
    resolve_context,
    series_key,
)


def test_explicit_context_does_not_read_or_change_kubeconfig(monkeypatch) -> None:
    """An explicit target bypasses current-context lookup without changing kubeconfig."""

    def unexpected(*args, **kwargs):
        raise AssertionError("explicit context must not invoke kubectl config")

    monkeypatch.setattr(verify.subprocess, "run", unexpected)
    assert resolve_context("work-cluster") == "work-cluster"


@pytest.mark.parametrize("value", ["", " ", "\n"])
def test_empty_explicit_context_is_rejected(value: str) -> None:
    """Empty overrides cannot silently fall back to a different cluster."""
    with pytest.raises(ValueError, match="no Kubernetes context"):
        resolve_context(value)


@pytest.mark.parametrize("selected", ["kind-observability\n", "colima\r\n", ""])
def test_current_context_lookup_is_bounded_and_requires_a_selection(monkeypatch, selected) -> None:
    """Resolve local kubeconfig once and reject an unset current context."""

    def run(command, **kwargs):
        assert command == ["kubectl", "config", "current-context"]
        assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 10}
        return subprocess.CompletedProcess(command, 0, stdout=selected)

    monkeypatch.setattr(verify.subprocess, "run", run)
    if selected:
        assert resolve_context(None) == selected.strip()
    else:
        with pytest.raises(ValueError, match="no Kubernetes context"):
            resolve_context(None)


def test_failed_current_context_lookup_does_not_fall_back(monkeypatch) -> None:
    """A kubeconfig error prevents receipt reads against an implicit target."""

    def run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(verify.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        resolve_context(None)


@pytest.mark.parametrize("context", ["kind-observability", "colima", "work-cluster"])
def test_receipt_read_uses_selected_context_and_dedicated_namespace(monkeypatch, context) -> None:
    """Each kubectl read names both the cluster context and the example namespace."""

    def run(command, **kwargs):
        assert command == [
            "kubectl",
            "--context",
            context,
            "-n",
            NAMESPACE,
            "exec",
            "deployment/benchmark",
            "-c",
            "benchmark",
            "--",
            "cat",
            "/metrics/evidence/failure-run.json",
        ]
        assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 30}
        return subprocess.CompletedProcess(command, 0, stdout='{"result": {"ok": false}}')

    monkeypatch.setattr(verify.subprocess, "run", run)
    assert read_receipt("failure", context=context) == {"result": {"ok": False}}


@pytest.mark.parametrize("explicit", [False, True])
def test_verifier_retains_one_context_for_all_reads_and_output(
    monkeypatch, tmp_path, capsys, explicit
) -> None:
    """Changing ambient kubeconfig cannot redirect later receipt reads within a run."""
    receipts = _receipts()
    receipts["transition"]["result"]["ok"] = True
    failure = copy.deepcopy(receipts["transition"])
    failure["result"].update(ok=False, completed_at_seconds=120)
    failure["last_success_timestamp_seconds"] = 110
    current_receipts = receipts | {"transition": failure}
    expected = expected_metrics(current_receipts)
    vector = [
        {"metric": dict(labels) | {"__name__": name}, "value": [125, value]}
        for (name, labels), value in expected.items()
    ]
    lookup_count = 0
    reads = []

    def run(command, **kwargs):
        nonlocal lookup_count
        if command == ["kubectl", "config", "current-context"]:
            lookup_count += 1
            return subprocess.CompletedProcess(command, 0, stdout="work-cluster\n")
        assert command[:5] == ["kubectl", "--context", "work-cluster", "-n", NAMESPACE]
        scenario = Path(command[-1]).name.removesuffix("-run.json")
        reads.append(scenario)
        payload = failure if scenario == "failure" else receipts[scenario]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload))

    def query(_url, expression):
        if expression == verify.QUERY:
            return vector
        if expression.startswith("up{"):
            return [{"value": [125, 1]}]
        return [{"value": [125, 1]} for _ in PROFILES]

    output = tmp_path / "receipt.json"
    argv = ["verify.py", "--output", str(output)]
    if explicit:
        argv += ["--context", "work-cluster"]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(verify.subprocess, "run", run)
    monkeypatch.setattr(verify.time, "time", lambda: 125)
    monkeypatch.setattr(verify, "query", query)
    monkeypatch.setattr(verify, "query_grafana", lambda _url: vector)
    monkeypatch.setattr(
        verify, "urlopen", lambda *a, **kw: io.BytesIO(b"node_textfile_scrape_error 0\n")
    )
    monkeypatch.setattr(
        verify,
        "fetch_json",
        lambda _url: {
            "dashboard": {"uid": "iperf3-lib-observability"},
            "meta": {"url": "/d/iperf3-lib-observability"},
        },
    )

    assert verify.main() == 0
    assert lookup_count == (0 if explicit else 1)
    assert reads == [*PROFILES, "failure"]
    assert json.loads(output.read_text())["kubernetes"] == {
        "context": "work-cluster",
        "namespace": NAMESPACE,
    }
    assert "context 'work-cluster'" in capsys.readouterr().out


def _receipts() -> dict:
    receipts = {}
    for profile in PROFILES:
        sender = {"bits_per_second": 800, "retransmits": 3}
        receiver = {"bits_per_second": 720}
        if profile == "udp":
            sender = {"bits_per_second": 800}
            receiver |= {"lost_percent": 2.5, "jitter_ms": 3}
        end = {"sum_sent": sender, "sum_received": receiver}
        if profile == "tcp-bidirectional":
            end |= {
                "sum_sent_bidir_reverse": {"bits_per_second": 1600},
                "sum_received_bidir_reverse": {"bits_per_second": 1440},
            }
        receipts[profile] = {
            "last_success_timestamp_seconds": 100,
            "result": {
                "ok": profile != "transition",
                "completed_at_seconds": 110,
                "raw": {
                    "start": {
                        "test_start": {
                            "protocol": "UDP" if profile == "udp" else "TCP",
                            "duration": 2,
                            "num_streams": 1 if profile == "udp" else 2,
                            "reverse": profile == "tcp-reverse",
                            "bidir": profile == "tcp-bidirectional",
                            "target_bitrate": 4_000_000,
                        }
                    },
                    "end": end,
                },
            },
        }
    return receipts


def test_expected_metrics_derive_units_direction_and_observer_from_native_json() -> None:
    """Compare flow observations individually and apply the documented base-unit conversions."""
    expected = expected_metrics(_receipts())
    labels = {
        "profile": "udp",
        "target": "pod-loopback",
        "direction": "client_to_server",
        "observer": "receiver",
    }
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", labels)] == 90
    assert expected[series_key("iperf3_last_run_packet_loss_ratio", labels)] == 0.025
    assert expected[series_key("iperf3_last_run_jitter_seconds", labels)] == 0.003
    reverse = labels | {"profile": "tcp-reverse", "direction": "server_to_client"}
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", reverse)] == 90
    bidirectional = labels | {"profile": "tcp-bidirectional", "direction": "server_to_client"}
    assert expected[series_key("iperf3_last_run_throughput_bytes_per_second", bidirectional)] == 180
    assert not any(
        "transition" in dict(key[1]).values() and "throughput" in key[0] for key in expected
    )


def test_native_configuration_mismatch_is_rejected() -> None:
    """Require returned native configuration to match the exercised protocol and dimensions."""
    receipts = _receipts()
    receipts["udp"]["result"]["raw"]["start"]["test_start"]["protocol"] = "TCP"
    with pytest.raises(ValueError, match="native protocol"):
        expected_metrics(receipts)


@pytest.mark.parametrize("flag", ["reverse", "bidir", "bidirectional"])
def test_native_direction_mismatch_is_rejected(flag: str) -> None:
    """Reject ignored requested direction flags and conflicting native aliases."""
    receipts = _receipts()
    profile = "tcp-reverse" if flag == "reverse" else "tcp-bidirectional"
    receipts[profile]["result"]["raw"]["start"]["test_start"][flag] = 0
    with pytest.raises(ValueError, match="flag"):
        expected_metrics(receipts)


def test_native_bidirectional_flag_must_be_present() -> None:
    """An absent flag cannot qualify a requested bidirectional native run."""
    receipts = _receipts()
    del receipts["tcp-bidirectional"]["result"]["raw"]["start"]["test_start"]["bidir"]
    with pytest.raises(ValueError, match="bidirectional flag"):
        expected_metrics(receipts)


def test_grafana_frames_preserve_measurement_labels() -> None:
    """Extract exact sample labels and numbers from the dashboard query response format."""
    labels = {"__name__": "iperf3_last_run_success", "profile": "udp"}
    frame = {
        "schema": {
            "fields": [
                {"name": "Time", "type": "time"},
                {"name": "Value", "type": "number", "labels": labels},
            ]
        },
        "data": {"values": [[100], [1]]},
    }
    assert grafana_vector({"results": {"A": {"status": 200, "frames": [frame]}}}) == [
        {"metric": labels, "value": [0, 1]}
    ]
    frame["data"]["values"][1] = [None]
    with pytest.raises(ValueError, match="one finite sample"):
        grafana_vector({"results": {"A": {"frames": [frame]}}})


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "wrong", "nan"])
def test_api_vector_errors_fail(mutation: str) -> None:
    """Reject successful HTTP responses containing incomplete or incorrect measurements."""
    labels = {"profile": "udp", "target": "pod-loopback"}
    expected = {series_key("iperf3_last_run_success", labels): 1}
    vector = [
        {
            "metric": labels
            | {
                "__name__": "iperf3_last_run_success",
                "job": "iperf3-textfile",
                "instance": "benchmark:9100",
            },
            "value": [100, "1"],
        }
    ]
    if mutation == "missing":
        vector.clear()
    elif mutation == "extra":
        extra = copy.deepcopy(vector[0])
        extra["metric"]["profile"] = "unexpected"
        vector.append(extra)
    elif mutation == "duplicate":
        vector.append(copy.deepcopy(vector[0]))
    elif mutation == "wrong":
        vector[0]["value"][1] = "0"
    else:
        vector[0]["value"][1] = "NaN"
    with pytest.raises(ValueError):
        check_vector(expected, vector)


def test_dashboard_current_values_use_instant_queries() -> None:
    """Prevent last-not-null range reduction from displaying old samples as current status."""
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads(
        (root / "examples/observability/dashboard.json").read_text(encoding="utf-8")
    )
    for panel in dashboard["panels"]:
        if panel["type"] == "stat":
            assert all(target["instant"] and not target["range"] for target in panel["targets"])
        if panel["title"] == "UDP packet loss ratio":
            assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"
            assert panel["fieldConfig"]["defaults"]["max"] == 1


def test_example_deployments_select_linux_nodes() -> None:
    """Keep Linux-only images off Windows nodes in mixed Kubernetes clusters."""
    root = Path(__file__).resolve().parents[1] / "examples/observability"
    documents = (root / "stack.yaml").read_text(encoding="utf-8").split("\n---\n")
    deployments = [doc for doc in documents if "\nkind: Deployment\n" in doc]
    assert len(deployments) == 3
    for deployment in deployments:
        assert "\n      nodeSelector:\n        kubernetes.io/os: linux\n" in deployment
    namespace = (root / "namespace.yaml").read_text(encoding="utf-8")
    assert "pod-security.kubernetes.io/enforce: restricted\n" in namespace
    assert "pod-security.kubernetes.io/enforce-version: latest\n" in namespace
