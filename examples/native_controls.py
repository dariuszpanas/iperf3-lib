"""Run a bounded client with explicit local binding and aggregate rate intent.

Start a compatible server first, then run, for example:
    python examples/native_controls.py 127.0.0.1 --bind-address 127.0.0.1 --events
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal, cast

from iperf3_lib import Client, ClientConfig, Protocol
from iperf3_lib.artifacts import artifact_from_result, dumps_artifact
from iperf3_lib.events import NativeEvent
from iperf3_lib.intent import RateIntent, parse_rate


def show_event(event: NativeEvent) -> None:
    """Keep event handling short; final measurements come from the Result."""
    print(f"event {event.sequence}: {event.kind}")


def main() -> int:
    """Execute one explicitly requested benchmark; importing generates no traffic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", help="destination hostname or address")
    parser.add_argument("--port", type=int, default=5201)
    parser.add_argument("--bind-address", help="address assigned to the local client")
    parser.add_argument("--bind-device", help="local device name; native support required")
    parser.add_argument("--family", choices=("auto", "ipv4", "ipv6"), default="auto")
    parser.add_argument("--protocol", choices=("tcp", "udp", "sctp"), default="tcp")
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--rate", default="10 Mbit/s", help="aggregate per-direction SI rate")
    parser.add_argument("--timeout", type=float, default=10, help="worker deadline in seconds")
    parser.add_argument("--events", action="store_true", help="print bounded live-event notices")
    parser.add_argument("--output", type=Path, help="optional result-artifact destination")
    args = parser.parse_args()

    config = ClientConfig(
        server=args.server,
        port=args.port,
        protocol=Protocol(args.protocol),
        duration=args.duration,
        parallel=args.parallel,
        bind_address=args.bind_address,
        bind_device=args.bind_device,
        address_family=cast("Literal['auto', 'ipv4', 'ipv6']", args.family),
        connect_timeout_ms=2_000,
        interval_seconds=0.5,
    )
    intent = RateIntent(aggregate_bps_per_direction=parse_rate(args.rate))
    result = Client(config, rate_intent=intent).run(
        timeout=args.timeout,
        on_event=show_event if args.events else None,
    )
    print(f"completed={result.ok} role={result.reporting_role} error={result.error}")
    print("event delivery:", result.extensions.get("iperf3_lib.event_delivery"))
    print("native setting receipts:", result.extensions.get("iperf3_lib.native_configuration"))
    if args.output is not None:
        args.output.write_text(
            dumps_artifact(artifact_from_result(result), indent=2), encoding="utf-8"
        )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
