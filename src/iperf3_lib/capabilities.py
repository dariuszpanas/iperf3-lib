"""Layered wrapper/native capability evidence without implicit native loading."""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .exceptions import IperfLibraryError
from .ffi import api
from .ffi.symbols import CDEF
from .result import Result

if TYPE_CHECKING:
    HAS_BIDIR: bool
    HAS_BIND_ADDRESS: bool
    HAS_JSON_CALLBACK: bool
    HAS_JSON_OUTPUT: bool
    HAS_JSON_STREAM: bool
    HAS_MPTCP: bool
    HAS_PROTOCOL_SELECTION: bool


@dataclass(frozen=True)
class LibraryCapability:
    """Outcome of inspecting the local library, independently of any benchmark."""

    state: Literal["unprobed", "available", "unavailable", "error"]
    version: str | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolCapability:
    """Distinguish wrapper ABI declarations from native symbol lookup evidence."""

    name: str
    declared: bool
    state: Literal["present", "absent", "unknown"] = "unknown"
    diagnostic: str | None = None


@dataclass(frozen=True)
class FeatureCapability:
    """Wrapper coverage and requirements, without claiming runtime success."""

    name: str
    wrapper: Literal["supported", "unsupported", "unimplemented"]
    symbols: tuple[SymbolCapability, ...]
    constraints: tuple[str, ...]
    runtime: Literal["not_run"] = "not_run"


@dataclass(frozen=True)
class ExecutionEvidence:
    """A supplied result's outcome, separate from the currently inspected library."""

    status: Literal["not_provided", "completed", "failed", "incomplete", "unknown"] = "not_provided"
    native_version: str | None = None
    protocol: str | None = None
    verified_settings: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CapabilityReport:
    """Separate static wrapper support, loaded symbols, qualification, and run evidence."""

    library: LibraryCapability
    features: tuple[FeatureCapability, ...]
    execution: ExecutionEvidence
    current_platform: str
    current_python: str
    tested_platforms: tuple[str, ...] = ("Linux",)
    tested_python_versions: tuple[str, ...] = ("3.12", "3.13", "3.14")
    tested_native_versions: tuple[str, ...] = ("3.19.1", "3.22")


_FEATURES: tuple[
    tuple[
        str, Literal["supported", "unsupported", "unimplemented"], tuple[str, ...], tuple[str, ...]
    ],
    ...,
] = (
    (
        "protocol_selection",
        "supported",
        ("set_protocol", "iperf_get_test_protocol_id"),
        ("TCP, UDP and SCTP; SCTP additionally requires native build and kernel support.",),
    ),
    (
        "bidirectional",
        "supported",
        ("iperf_set_test_bidirectional",),
        ("Both directions share one per-stream rate setting.",),
    ),
    (
        "rate",
        "supported",
        ("iperf_set_test_rate",),
        ("Rate is a per-stream pacing target, not an achieved-throughput guarantee.",),
    ),
    (
        "json_output",
        "supported",
        ("iperf_set_test_json_output", "iperf_get_test_json_output_string"),
        ("Complete output is consumed after the blocking run.",),
    ),
    (
        "json_callback",
        "supported",
        ("iperf_set_test_json_callback",),
        ("Copied NativeEvent callbacks are available through Client and Server on_event.",),
    ),
    (
        "bind_address",
        "supported",
        ("iperf_set_test_bind_address", "iperf_get_test_bind_address"),
        (
            "ClientConfig.bind_address and ServerConfig.bind_address; legacy Server.bind_host alias.",
        ),
    ),
    (
        "mptcp",
        "supported",
        ("iperf_parse_arguments",),
        ("Configured in an isolated Python worker; requires an MPTCP-enabled build and kernel.",),
    ),
    (
        "json_stream",
        "supported",
        ("iperf_set_test_json_stream", "iperf_set_test_json_stream_full_output"),
        (
            "Bounded NativeEvent delivery; 3.21 and 3.22 retain full JSON, earlier versions label reconstruction from events.",
        ),
    ),
    (
        "pacing_timer",
        "supported",
        ("iperf_set_test_pacing_timer", "iperf_get_test_pacing_timer"),
        ("Getter verifies the configured microsecond timer, not scheduler timing accuracy.",),
    ),
    (
        "socket_buffer",
        "supported",
        ("iperf_set_test_socket_bufsize", "iperf_get_test_socket_bufsize"),
        ("Stored requested size differs from kernel-observed send/receive buffer sizes.",),
    ),
    (
        "congestion_control",
        "supported",
        ("iperf_set_test_congestion_control", "iperf_get_test_congestion_control"),
        ("TCP/Linux algorithms depend on the kernel; a getter returns stored request only.",),
    ),
    (
        "server_output",
        "supported",
        ("iperf_set_test_get_server_output", "iperf_get_test_get_server_output"),
        (
            "ClientConfig.get_server_output retains native server output; Server returns its own Result.",
        ),
    ),
    (
        "socket_pacing",
        "supported",
        ("iperf_parse_arguments",),
        (
            "ClientConfig.fq_rate_bps uses isolated native parsing; no public getter or kernel effect guarantee.",
        ),
    ),
    (
        "device_binding",
        "supported",
        ("iperf_set_test_bind_dev", "iperf_get_test_bind_dev"),
        ("Client and server; native build, OS and interface permissions apply.",),
    ),
    (
        "source_port",
        "supported",
        ("iperf_set_test_bind_port", "iperf_get_test_bind_port"),
        ("ClientConfig.client_port selects the native data connection source port.",),
    ),
    (
        "address_family",
        "supported",
        ("iperf_parse_arguments",),
        ("Explicit ipv4/ipv6 selection occurs inside an isolated native worker.",),
    ),
    (
        "transfer_counts",
        "supported",
        ("iperf_set_test_bytes", "iperf_set_test_blocks"),
        (
            "Use duration=None and exactly one count; elapsed time cannot be estimated from a count.",
        ),
    ),
    (
        "tcp_tuning",
        "supported",
        ("iperf_set_test_no_delay", "iperf_set_test_mss"),
        ("Protocol constraints and native/kernel limits apply.",),
    ),
    (
        "authentication",
        "supported",
        ("iperf_set_test_client_rsa_pubkey", "iperf_set_test_server_rsa_privkey"),
        (
            "Requires OpenSSL-enabled libiperf and RSA keys; passwords never enter saved configuration.",
        ),
    ),
    (
        "server_policies",
        "supported",
        ("iperf_parse_arguments",),
        (
            "ServerConfig policies use isolated parsing; max duration requires libiperf 3.21 or newer.",
        ),
    ),
    (
        "gsro",
        "supported",
        ("iperf_parse_arguments",),
        ("UDP-only, libiperf 3.21 or newer and native build/kernel support required.",),
    ),
    (
        "isolated_execution",
        "supported",
        ("iperf_parse_arguments",),
        (
            "Async methods, expanded controls and explicit timeout use a disposable Python process, without an iperf3 executable.",
        ),
    ),
    (
        "concurrent_execution",
        "unsupported",
        (),
        ("Blocking native calls and process-global error state are non-reentrant.",),
    ),
    (
        "hard_cancellation",
        "supported",
        ("iperf_parse_arguments",),
        (
            "Cancelling arun/aserve_once stops and reaps the owned worker before propagating cancellation; active callbacks must return. Direct calls and application-owned executor wrappers are not cancellable.",
        ),
    ),
)
_DECLARED = frozenset(re.findall(r"\b(\w+)\s*\(", CDEF))


def _library() -> LibraryCapability:
    try:
        getter = api.lib.iperf_get_iperf_version
    except (IperfLibraryError, OSError) as exc:
        return LibraryCapability("unavailable", diagnostics=(str(exc),))
    except AttributeError:
        return LibraryCapability("available", diagnostics=("Native version symbol is absent.",))
    except Exception as exc:
        return LibraryCapability("error", diagnostics=(f"{type(exc).__name__}: {exc}",))
    try:
        value = getter()
        if value == api.ffi.NULL:
            return LibraryCapability(
                "available", diagnostics=("Native version getter returned null.",)
            )
        version = api.ffi.string(value).decode("utf-8")
        if not version:
            return LibraryCapability(
                "available", diagnostics=("Native version getter returned empty text.",)
            )
        return LibraryCapability("available", version)
    except Exception as exc:
        return LibraryCapability(
            "error", diagnostics=(f"Native version probe failed: {type(exc).__name__}: {exc}",)
        )


def _symbol(name: str, library: LibraryCapability) -> SymbolCapability:
    declared = name in _DECLARED
    if not declared:
        return SymbolCapability(
            name,
            False,
            diagnostic="Not declared in this wrapper ABI; native presence was not probed.",
        )
    if library.state != "available":
        return SymbolCapability(name, True, diagnostic=f"Native library state is {library.state}.")
    try:
        getattr(api.lib, name)
    except AttributeError:
        return SymbolCapability(name, True, "absent")
    except Exception as exc:
        return SymbolCapability(
            name, True, diagnostic=f"Symbol probe failed: {type(exc).__name__}: {exc}"
        )
    return SymbolCapability(name, True, "present")


def get_capabilities(
    *, probe_native: bool = True, result: Result | None = None
) -> CapabilityReport:
    """Inspect symbols/version only; never allocate a test or generate traffic.

    ``probe_native=False`` performs no native loading. A supplied result records
    separate execution evidence, never upgrades a feature's static runtime state
    or implies the result came from the currently loaded library.
    """
    if not isinstance(probe_native, bool):
        raise TypeError("probe_native must be a boolean")
    if result is not None and not isinstance(result, Result):
        raise TypeError("result must be a Result or None")
    library = _library() if probe_native else LibraryCapability("unprobed")
    features = tuple(
        FeatureCapability(
            name, wrapper, tuple(_symbol(symbol, library) for symbol in symbols), constraints
        )
        for name, wrapper, symbols, constraints in _FEATURES
    )
    execution = ExecutionEvidence()
    if result is not None:
        metadata = result.execution
        execution = ExecutionEvidence(
            metadata.status if metadata is not None else "unknown",
            metadata.native_version if metadata is not None else None,
            result.protocol,
            tuple(
                sorted(
                    name
                    for name, setting in metadata.configuration.effective.items()
                    if setting.state == "verified"
                )
            )
            if metadata is not None
            else (),
            result.error,
        )
    return CapabilityReport(
        library, features, execution, platform.system(), platform.python_version()
    )


def has_symbol(name: str) -> bool:
    """Probe a legacy flag; false also covers library failure and undeclared names.

    Use get_capabilities for separate failure/availability evidence.
    """
    try:
        return hasattr(api.lib, name)
    except Exception:
        return False


_LEGACY = {
    "HAS_BIDIR": ("iperf_set_test_bidirectional",),
    "HAS_JSON_OUTPUT": ("iperf_set_test_json_output",),
    "HAS_JSON_CALLBACK": ("iperf_set_test_json_callback",),
    "HAS_PROTOCOL_SELECTION": ("set_protocol", "iperf_get_test_protocol_id"),
    "HAS_BIND_ADDRESS": ("iperf_set_test_bind_address",),
    "HAS_MPTCP": ("iperf_parse_arguments",),
    "HAS_JSON_STREAM": ("iperf_parse_arguments", "iperf_set_test_json_stream"),
}


def __getattr__(name: str) -> bool:
    """Resolve legacy flags only when accessed, leaving module import native-free."""
    if name in _LEGACY:
        return all(has_symbol(symbol) for symbol in _LEGACY[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CapabilityReport",
    "ExecutionEvidence",
    "FeatureCapability",
    "LibraryCapability",
    "SymbolCapability",
    "get_capabilities",
    "has_symbol",
    "HAS_BIDIR",
    "HAS_BIND_ADDRESS",
    "HAS_JSON_CALLBACK",
    "HAS_JSON_OUTPUT",
    "HAS_JSON_STREAM",
    "HAS_MPTCP",
    "HAS_PROTOCOL_SELECTION",
]
