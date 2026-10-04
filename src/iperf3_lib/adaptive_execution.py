"""Sequential execution of finite batches from the pure adaptive UDP planner."""

from __future__ import annotations

import time
from collections.abc import Callable

from .adaptive import (
    AdaptiveBatchResult,
    AdaptiveResult,
    PreparedAdaptiveUDP,
    _validated_prepared_v1,
    next_adaptive_batch,
    summarize_adaptive_udp,
)
from .result import Result
from .trials import TrialSpec, run_plan


def run_adaptive_udp(
    prepared: PreparedAdaptiveUDP,
    *,
    executor: Callable[[TrialSpec], Result] | None = None,
) -> AdaptiveResult:
    """Run admitted batches with one recorded cooldown between successive calls.

    The pure planner reserves complete batches against whole-experiment limits.
    Each batch reuses ``run_plan`` and retains all artifacts, failures and unstarted
    trials. No failure is hidden by a retry. Confirmations are separate declared
    measurements whose earlier evidence remains part of the decision.

    Budgets bound admitted duration and target payload estimates, including
    warm-ups and omit periods. They cannot cap wire traffic or interrupt a
    blocking native call. Execution is sequential and retains the existing
    non-reentrant native contract across callers.
    """
    prepared = _validated_prepared_v1(prepared)
    if executor is not None and not callable(executor):
        raise ValueError("executor must be callable")
    history: list[AdaptiveBatchResult] = []
    while True:
        decision = next_adaptive_batch(prepared, tuple(history))
        if decision.batch is None:
            return summarize_adaptive_udp(prepared, tuple(history))
        pause = 0.0
        if history and prepared.policy.pause_seconds:
            started = time.monotonic()
            time.sleep(prepared.policy.pause_seconds)
            pause = time.monotonic() - started
        execution = run_plan(decision.batch.plan, executor=executor)
        history.append(AdaptiveBatchResult(decision.batch, execution, pause))
