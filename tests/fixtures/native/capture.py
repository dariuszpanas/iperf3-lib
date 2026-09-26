"""Capture bounded native client and server JSON fixtures inside a Linux test image."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from iperf3_lib import Client, ClientConfig, Protocol


def capture(output: Path, version: str) -> None:
    """Run four sequential one-second loopback scenarios and preserve both endpoints."""
    native_version = subprocess.run(
        ["iperf3", "--version"], check=True, capture_output=True, text=True, timeout=5
    ).stdout.splitlines()[0]
    if native_version.split()[1] != version:
        raise ValueError(f"expected libiperf {version}, found {native_version}")
    output.mkdir(parents=True, exist_ok=True)
    for scenario in ("tcp-forward", "tcp-reverse", "tcp-bidirectional", "udp"):
        server = subprocess.Popen(
            ["iperf3", "--server", "--one-off", "--json", "--bind", "127.0.0.1", "--port", "5201"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # A TCP readiness connection would consume the one-off server.
            time.sleep(0.25)
            result = Client(
                ClientConfig(
                    server="127.0.0.1",
                    duration=1,
                    parallel=2,
                    rate=4_000_000,
                    protocol=Protocol.UDP if scenario == "udp" else Protocol.TCP,
                    reverse=scenario == "tcp-reverse",
                    bidirectional=scenario == "tcp-bidirectional",
                    blksize=1200 if scenario == "udp" else None,
                )
            ).run()
            if not result.ok:
                raise RuntimeError(result.error)
            stdout, stderr = server.communicate(timeout=5)
            if server.returncode:
                raise RuntimeError(f"native server failed: {stderr}")
            server_raw = json.loads(stdout)
            for role, raw in (("client", result.raw), ("server", server_raw)):
                if raw.get("error"):
                    raise RuntimeError(raw["error"])
                destination = output / f"{scenario}-{role}.json"
                destination.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
                print(
                    f"{version} {scenario} {role}: {destination.stat().st_size} bytes", flush=True
                )
        finally:
            if server.poll() is None:
                server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    capture(args.output, args.version)
