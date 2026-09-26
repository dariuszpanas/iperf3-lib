"""Run bounded loopback benchmarks and preserve native results alongside textfile metrics."""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from pathlib import Path

from iperf3_lib import Client, ClientConfig, Protocol
from iperf3_lib.exporters.prometheus import write_textfile

SCENARIOS = ("tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp", "transition", "failure")


def run_scenario(scenario: str, directory: Path) -> dict:
    """Publish one native result, retaining a previous success timestamp on failure."""
    profile = "transition" if scenario == "failure" else scenario
    evidence = directory / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    state_path = evidence / f"{profile}.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    last_success = previous.get("last_success_timestamp_seconds")
    config = ClientConfig(
        server="127.0.0.1",
        port=5202 if scenario == "failure" else 5201,
        protocol=Protocol.UDP if scenario == "udp" else Protocol.TCP,
        duration=2,
        parallel=1 if scenario == "udp" else 2,
        rate=4_000_000,
        reverse=scenario == "tcp-reverse",
        bidirectional=scenario == "tcp-bidirectional",
        blksize=1200 if scenario == "udp" else None,
    )
    result = Client(config).run()
    if result.ok:
        last_success = result.completed_at_seconds
    labels = {"target": "pod-loopback", "profile": profile}
    write_textfile(
        directory / f"{profile}.prom",
        result,
        labels=labels,
        last_success_timestamp_seconds=last_success,
    )
    receipt = {
        "scenario": scenario,
        "labels": labels,
        "expected_success": scenario != "failure",
        "last_success_timestamp_seconds": last_success,
        "result": result.to_dict(),
    }
    serialized = json.dumps(receipt, indent=2, sort_keys=True)
    state_path.write_text(serialized + "\n", encoding="utf-8")
    # Preserve both sides of the transition even though its published file is replaced.
    (evidence / f"{scenario}-run.json").write_text(serialized + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "scenario": scenario,
                "ok": result.ok,
                "completed_at_seconds": result.completed_at_seconds,
                "last_success_timestamp_seconds": last_success,
                "error": result.error,
                "native_protocol": result.raw.get("start", {})
                .get("test_start", {})
                .get("protocol"),
                "flows": [flow.direction for flow in result.flows],
            }
        ),
        flush=True,
    )
    if result.ok != (scenario != "failure"):
        raise RuntimeError(f"{scenario}: native run did not have the expected success state")
    return receipt


def main() -> None:
    """Run one requested scenario or keep the container idle for explicit invocations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hold", "run"))
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--directory", type=Path, default=Path("/metrics"))
    args = parser.parse_args()
    if args.action == "hold":
        print(
            "Ready for explicitly requested benchmarks; no periodic traffic is generated.",
            flush=True,
        )
        while True:
            time.sleep(3600)
    args.directory.mkdir(parents=True, exist_ok=True)
    with (args.directory / ".benchmark.lock").open("a") as lock:
        # libiperf is not reentrant. Serialize even separate kubectl exec requests.
        fcntl.flock(lock, fcntl.LOCK_EX)
        scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
        for scenario in scenarios:
            run_scenario(scenario, args.directory)
            if args.scenario == "all":
                # Give the 2-second scrape schedule time to retain each state.
                time.sleep(6)


if __name__ == "__main__":
    main()
