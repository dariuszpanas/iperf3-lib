"""Detached native JSON events delivered outside the native execution thread."""

from dataclasses import dataclass

from .result import JSONValue


@dataclass(frozen=True)
class NativeEvent:
    """One copied JSON event; sequence gaps identify bounded-delivery drops.

    Data is detached from native memory and the eventual Result. Callbacks run
    in the thread calling Client.run or Server.run_once, never a C callback.
    """

    kind: str
    data: JSONValue
    sequence: int
    received_at_seconds: float
