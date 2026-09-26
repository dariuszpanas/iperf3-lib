"""Explicit synthetic native-shaped receipts for deterministic sweep tests."""

from dataclasses import asdict
from types import SimpleNamespace

from iperf3_lib.config import ClientConfig
from iperf3_lib.intent import resolve_rate
from iperf3_lib.result import result_from_iperf_json
from iperf3_lib.sweeps import SweepAxis, prepare_sweep, run_sweep
from iperf3_lib.trials import PlanBudget, TrialPolicy


class Clock:
    """Deterministic operation and pause clock, independent of actual sleeping."""

    def __init__(self):
        """Start at zero monotonic elapsed time."""
        self.elapsed = 0.0
        self.pauses = []

    def sleep(self, seconds):
        """Advance an explicitly recorded pause."""
        self.pauses.append(seconds)
        self.elapsed += seconds

    def install(self, monkeypatch):
        """Install the public executor's shared clock seam."""
        import iperf3_lib.trials as trials

        monkeypatch.setattr(
            trials,
            "time",
            SimpleNamespace(
                time=lambda: 1000 + self.elapsed, monotonic=lambda: self.elapsed, sleep=self.sleep
            ),
        )


def measured(spec, *, count=100, seconds=1, failed=False, incomplete=False):
    """Generate labeled synthetic observations with actual canonical receipt pointers."""
    cfg = spec.resolved_config
    start = {
        "version": "iperf 3.21",
        "system_info": "Linux synthetic sweep fixture",
        "connecting_to": {"host": str(cfg.server), "port": cfg.port},
        "test_start": {
            "protocol": cfg.protocol.value.upper(),
            "duration": cfg.duration,
            "num_streams": cfg.parallel,
            "omit": cfg.omit,
            "reverse": int(cfg.reverse),
            "bidir": int(cfg.bidirectional),
            "blksize": cfg.blksize or 4096,
            "target_bitrate": cfg.rate or 0,
            "tos": cfg.tos or 0,
        },
    }
    raw = {"start": start, "end": {}}
    if failed:
        raw["error"] = "synthetic native failure"
    elif not incomplete:
        for suffix in ("", "_bidir_reverse") if cfg.bidirectional else ("",):
            for endpoint in ("sum_sent", "sum_received"):
                raw["end"][f"{endpoint}{suffix}"] = {
                    "bytes": count * (2 if endpoint == "sum_sent" else 1),
                    "seconds": seconds,
                    "bits_per_second": 99999999,
                }
    result = result_from_iperf_json(raw)
    result.extensions["example.synthetic"] = True
    result.extensions["iperf3_lib.rate_intent"] = {
        "schema_version": 1,
        "caller_config": {**asdict(spec.config), "protocol": spec.config.protocol.value},
        "intent": asdict(spec.rate_intent) if spec.rate_intent else None,
        "resolution": resolve_rate(spec.config, spec.rate_intent).to_dict(),
    }
    return result


def prepared(*, axes=None, policy=None, budget=None, base=None, **kwargs):
    """Admit a two-cell finite stream sweep unless a test supplies explicit alternatives."""
    return prepare_sweep(
        base or ClientConfig("127.0.0.1", duration=1, rate=4000000),
        axes if axes is not None else (SweepAxis("parallel", (1, 2)),),
        policy=policy or TrialPolicy(repetitions=2),
        budget=budget or PlanBudget(100, 100000000),
        **kwargs,
    )


def completed_sweep(**kwargs):
    """Execute synthetic receiver evidence through the shared real plan runner."""
    return run_sweep(prepared(**kwargs), executor=measured)
