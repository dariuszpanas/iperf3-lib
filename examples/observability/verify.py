"""Verify real native results through node_exporter, Prometheus, and Grafana APIs."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROFILES = ("tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp", "transition")
QUERY = '{__name__=~"iperf3_.*"}'


def fetch_json(url: str) -> dict:
    """Read a local service API with a finite network timeout."""
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def query(base_url: str, expression: str) -> list[dict]:
    """Return an instant Prometheus vector."""
    payload = fetch_json(f"{base_url}/api/v1/query?{urlencode({'query': expression})}")
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "vector":
        raise ValueError(f"query did not return a successful vector: {payload}")
    return payload["data"]["result"]


def grafana_vector(payload: dict) -> list[dict]:
    """Convert Grafana instant-query data frames into comparable sample vectors."""
    result = payload.get("results", {}).get("A", {})
    if result.get("error") or result.get("status", 200) != 200:
        raise ValueError(f"Grafana query failed: {result}")
    vector = []
    for frame in result.get("frames", []):
        values = frame["data"]["values"]
        for index, field in enumerate(frame["schema"]["fields"]):
            if field["type"] != "number":
                continue
            samples = values[index]
            if len(samples) != 1 or samples[0] is None:
                raise ValueError("Grafana instant query must return one finite sample per series")
            vector.append({"metric": field["labels"], "value": [0, samples[0]]})
    return vector


def query_grafana(base_url: str) -> list[dict]:
    """Exercise the same Grafana data-source query API used by dashboard panels."""
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": "iperf3-prometheus"},
                "expr": QUERY,
                "instant": True,
                "range": False,
                "format": "time_series",
                "intervalMs": 2000,
                "maxDataPoints": 1000,
            }
        ],
        "from": "now-1m",
        "to": "now",
    }
    request = Request(
        base_url + "/api/ds/query",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return grafana_vector(json.load(response))


def series_key(name: str, labels: dict[str, str]) -> tuple:
    """Identify a sample without Prometheus's job and instance transport labels."""
    return name, tuple(
        sorted(
            (key, value)
            for key, value in labels.items()
            if key not in {"job", "instance", "__name__"}
        )
    )


def expected_metrics(receipts: dict[str, dict]) -> dict[tuple, float]:
    """Derive expected measurements directly from each saved native JSON result."""
    expected = {}
    for profile in PROFILES:
        receipt = receipts[profile]
        result = receipt["result"]
        labels = {"profile": profile, "target": "pod-loopback"}
        expected[series_key("iperf3_last_run_success", labels)] = float(result["ok"])
        expected[series_key("iperf3_last_run_completed_timestamp_seconds", labels)] = result[
            "completed_at_seconds"
        ]
        expected[series_key("iperf3_last_success_timestamp_seconds", labels)] = receipt[
            "last_success_timestamp_seconds"
        ]
        if not result["ok"]:
            continue
        raw = result["raw"]
        test = raw["start"]["test_start"]
        expected_protocol = "UDP" if profile == "udp" else "TCP"
        if test["protocol"] != expected_protocol or test["duration"] != 2:
            raise ValueError(
                f"{profile}: native protocol or duration differs from requested values"
            )
        if test["num_streams"] != (1 if profile == "udp" else 2):
            raise ValueError(f"{profile}: native stream count differs from requested value")
        if test.get("target_bitrate", raw["start"].get("target_bitrate")) != 4_000_000:
            raise ValueError(f"{profile}: native target bitrate differs from requested value")
        reverse = test.get("reverse", 0)
        bidirectional = test.get("bidir", test.get("bidirectional", 0))
        if "bidir" in test and "bidirectional" in test and test["bidir"] != test["bidirectional"]:
            raise ValueError(f"{profile}: native bidirectional flags disagree")
        if reverse not in (0, 1) or reverse != (profile == "tcp-reverse"):
            raise ValueError(f"{profile}: native reverse flag differs from requested value")
        if bidirectional not in (0, 1) or bidirectional != (profile == "tcp-bidirectional"):
            raise ValueError(f"{profile}: native bidirectional flag differs from requested value")
        for suffix, direction in (
            ("", "server_to_client" if reverse else "client_to_server"),
            ("_bidir_reverse", "server_to_client"),
        ):
            if suffix and not bidirectional:
                continue
            for observer, key in (("sender", "sum_sent"), ("receiver", "sum_received")):
                stats = raw["end"].get(key + suffix)
                if stats is None:
                    continue
                observation = labels | {"direction": direction, "observer": observer}
                fields = (
                    ("bits_per_second", "throughput_bytes_per_second", 8),
                    ("retransmits", "retransmissions", 1),
                    ("lost_percent", "packet_loss_ratio", 100),
                    ("jitter_ms", "jitter_seconds", 1000),
                )
                for native, metric, divisor in fields:
                    if stats.get(native) is not None:
                        expected[series_key("iperf3_last_run_" + metric, observation)] = (
                            stats[native] / divisor
                        )
    return expected


def check_vector(expected: dict[tuple, float], vector: list[dict]) -> None:
    """Fail on missing, extra, duplicate, nonfinite, or numerically incorrect samples."""
    observed = {}
    for series in vector:
        labels = series["metric"]
        key = series_key(labels["__name__"], labels)
        if key in observed:
            raise ValueError(f"duplicate sample: {key}")
        observed[key] = float(series["value"][1])
    if observed.keys() != expected.keys():
        raise ValueError(
            f"metric identities differ; missing={expected.keys() - observed.keys()}, extra={observed.keys() - expected.keys()}"
        )
    for key, value in expected.items():
        if not math.isfinite(observed[key]) or not math.isclose(
            observed[key], value, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError(
                f"sample differs from native result: {key}: {observed[key]} != {value}"
            )


def read_receipt(scenario: str) -> dict:
    """Read evidence only from this example's dedicated Docker Desktop namespace."""
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            "docker-desktop",
            "-n",
            "iperf3-lib-observability",
            "exec",
            "deployment/benchmark",
            "-c",
            "benchmark",
            "--",
            "cat",
            f"/metrics/evidence/{scenario}-run.json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def main() -> int:
    """Check the native receipts, current API vectors, freshness, and dashboard provisioning."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:13000")
    parser.add_argument("--node-exporter-url", default="http://127.0.0.1:19100")
    parser.add_argument("--max-run-age", type=float, default=900)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipts = {profile: read_receipt(profile) for profile in PROFILES}
    failure = read_receipt("failure")
    successful_transition = receipts["transition"]
    if not successful_transition["result"]["ok"] or failure["result"]["ok"]:
        raise ValueError("transition must contain a real successful run followed by a failed run")
    if (
        failure["last_success_timestamp_seconds"]
        != successful_transition["result"]["completed_at_seconds"]
    ):
        raise ValueError("failed run did not retain the previous successful completion timestamp")
    if failure["result"]["completed_at_seconds"] <= failure["last_success_timestamp_seconds"]:
        raise ValueError(
            "failed completion timestamp must follow its retained successful timestamp"
        )
    receipts["transition"] = failure
    now = time.time()
    if any(
        not 0 <= now - receipt["result"]["completed_at_seconds"] <= args.max_run_age
        for receipt in receipts.values()
    ):
        raise ValueError(
            "native benchmark evidence is stale or has a future completion timestamp; run the scenarios again"
        )
    with urlopen(args.node_exporter_url + "/metrics", timeout=10) as response:
        text = response.read().decode()
    if "node_textfile_scrape_error 0\n" not in text:
        raise ValueError("node_exporter reported a textfile parsing error")
    expected = expected_metrics(receipts)
    prometheus = query(args.prometheus_url, QUERY)
    check_vector(expected, prometheus)
    health = query(args.prometheus_url, 'up{job="iperf3-textfile"}')
    if len(health) != 1 or float(health[0]["value"][1]) != 1:
        raise ValueError("Prometheus scrape target is not healthy")
    freshness = query(args.prometheus_url, "time() - timestamp(iperf3_last_run_success)")
    if len(freshness) != len(PROFILES) or any(
        not 0 <= float(series["value"][1]) < 15 for series in freshness
    ):
        raise ValueError("Prometheus is not collecting current samples")
    grafana = query_grafana(args.grafana_url)
    check_vector(expected, grafana)
    dashboard = fetch_json(args.grafana_url + "/api/dashboards/uid/iperf3-lib-observability")
    if dashboard.get("dashboard", {}).get("uid") != "iperf3-lib-observability":
        raise ValueError("Grafana did not provision the expected dashboard")
    report = {
        "verified_at_seconds": now,
        "sample_count": len(expected),
        "profiles": list(PROFILES),
        "failure_retained_last_success": True,
        "failure_has_no_throughput": True,
        "native_receipts": receipts,
        "successful_transition": successful_transition,
        "prometheus_vector": prometheus,
        "grafana_vector": grafana,
        "dashboard_url": args.grafana_url + dashboard["meta"]["url"],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"Verified {len(expected)} native-derived samples across five profiles in both Prometheus and Grafana."
    )
    print(
        "Verified fresh scrapes, successful parsing, retained last success, and absent failed-run throughput."
    )
    print(report["dashboard_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
