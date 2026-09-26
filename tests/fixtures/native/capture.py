"""Capture bounded native client and server JSON fixtures inside a Linux test image."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from iperf3_lib import Client, ClientConfig, Protocol, Result

SCENARIOS = (
    "tcp-forward",
    "tcp-reverse",
    "tcp-bidirectional",
    "udp",
    "udp-reverse",
    "udp-bidirectional",
    "sctp-forward",
    "sctp-reverse",
    "sctp-bidirectional",
    "tcp-warmup",
)


def preserve_raw(raw: dict[str, Any], *, reporting_role: str | None = None) -> Result:
    """Retain real native output independently of the normalization under test."""
    return Result(ok="error" not in raw, error=raw.get("error"), raw=raw)


def capture(output: Path, version: str, scenarios: list[str] | None = None) -> None:
    """Run bounded sequential loopback scenarios and preserve both native endpoints."""
    native_version = subprocess.run(
        ["iperf3", "--version"], check=True, capture_output=True, text=True, timeout=5
    ).stdout.splitlines()[0]
    if native_version.split()[1] != version:
        raise ValueError(f"expected libiperf {version}, found {native_version}")
    output.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios or SCENARIOS:
        server = subprocess.Popen(
            ["iperf3", "--server", "--one-off", "--json", "--bind", "127.0.0.1", "--port", "5201"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # A TCP readiness connection would consume the one-off server.
            time.sleep(0.25)
            config = ClientConfig(
                server="127.0.0.1",
                duration=1,
                parallel=2,
                rate=4_000_000,
                protocol=Protocol.UDP
                if scenario.startswith("udp")
                else Protocol.SCTP
                if scenario.startswith("sctp")
                else Protocol.TCP,
                reverse=scenario.endswith("-reverse"),
                bidirectional=scenario.endswith("-bidirectional"),
                blksize=1200 if scenario.startswith("udp") else None,
                omit=1 if scenario == "tcp-warmup" else 0,
            )
            # The CFFI call, protocol setters, and native JSON capture remain real.
            # Bypass only normalization so fixtures can expose unsupported values
            # and parser defects instead of being filtered by the parser under test.
            with patch("iperf3_lib.iperf_client.result_from_iperf_json", preserve_raw):
                result = Client(config).run()
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
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    args = parser.parse_args()
    capture(args.output, args.version, args.scenario)
