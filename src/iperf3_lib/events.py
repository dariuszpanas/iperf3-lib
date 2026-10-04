"""Detached native JSON events delivered outside the native execution thread."""

from dataclasses import dataclass
from typing import Literal

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


@dataclass(frozen=True)
class Measurement:
    """A canonical native observation; missing measurements remain ``None``."""

    direction: str = "unknown"
    observation: Literal["sender", "receiver"] | None = None
    scope: Literal["aggregate", "stream", "unknown"] = "unknown"
    stream_id: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    duration_seconds: float | None = None
    bytes: int | None = None
    bits_per_second: float | None = None
    packets: int | None = None
    lost_packets: int | None = None
    lost_percent: float | None = None
    retransmits: int | None = None
    jitter_ms: float | None = None
    omitted: bool | None = None
    tcp: JSONValue = None


@dataclass(frozen=True)
class WorkerStatePayload:
    """Wrapper admission/readiness, without a native measurement claim."""

    state: Literal["starting", "ready"]
    role: Literal["client", "server"]


@dataclass(frozen=True)
class NativeStartPayload:
    """Copied native start metadata and canonical test provenance."""

    raw: JSONValue
    protocol: str | None
    method: str
    reporting_role: Literal["client", "server"]


@dataclass(frozen=True)
class IntervalPayload:
    """Native interval evidence projected using complete-result semantics."""

    raw: JSONValue
    measurements: tuple[Measurement, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeErrorPayload:
    """Native error evidence; this does not establish wrapper completion."""

    raw: JSONValue
    message: str | None


@dataclass(frozen=True)
class NativeEndPayload:
    """Native end measurements, distinct from the owned operation terminal."""

    raw: JSONValue
    measurements: tuple[Measurement, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeDocumentPayload:
    """A copied complete native document, not a wrapper success receipt."""

    raw: JSONValue
    protocol: str | None
    method: str
    measurements: tuple[Measurement, ...]
    intervals: tuple[Measurement, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerOutputPayload:
    """Advisory native server output retained without invented measurements."""

    native_kind: str
    raw: JSONValue


@dataclass(frozen=True)
class UnknownPayload:
    """Unrecognized native event evidence retained for the application."""

    native_kind: str
    raw: JSONValue


@dataclass(frozen=True)
class MalformedPayload:
    """A bounded diagnostic sample for a native payload that could not be parsed."""

    reason: str
    error_type: str | None = None
    sample: str | None = None


@dataclass(frozen=True)
class DeliveryGapPayload:
    """Loss at one explicitly named capture, transport, or consumer stage."""

    stage: str
    dropped: int


@dataclass(frozen=True)
class TerminalPayload:
    """Owned-operation outcome published only after the operation has settled."""

    outcome: Literal["completed", "error", "cancelled"]
    result_available: bool
    native_status: Literal["completed", "failed", "incomplete"] | None
    cleanup_confirmed: bool
    consumer_dropped: int = 0
    capture: JSONValue = None
    delivery: JSONValue = None
    error_type: str | None = None
    error_message: str | None = None


type LivePayload = (
    WorkerStatePayload
    | NativeStartPayload
    | IntervalPayload
    | NativeErrorPayload
    | NativeEndPayload
    | NativeDocumentPayload
    | ServerOutputPayload
    | UnknownPayload
    | MalformedPayload
    | DeliveryGapPayload
    | TerminalPayload
)

type LiveEventKind = Literal[
    "worker_state",
    "native_start",
    "interval",
    "native_error",
    "native_end",
    "native_document",
    "server_output",
    "unknown",
    "malformed",
    "delivery_gap",
    "terminal",
]


@dataclass(frozen=True)
class LiveEvent:
    """Typed runtime envelope with distinct delivery, capture, and arrival provenance.

    Envelopes and scalar measurements are frozen. Nested JSON evidence is detached
    but remains application-mutable; changing it cannot alter another event or Result.
    Native measurement timestamps occur only in payload measurements. This runtime
    model is separate from the legacy NativeEvent and persisted report schemas.
    """

    kind: LiveEventKind
    payload: LivePayload
    request_id: str | None = None
    worker_id: str | None = None
    run_index: int | None = None
    delivery_sequence: int = 0
    capture_sequence: int | None = None
    arrival_offset_seconds: float | None = None
    received_at_seconds: float | None = None
