"""Configure libiperf inside a dedicated, disposable Python worker.

The public native argument parser can terminate its process. This module must
only be called by the isolated worker, never as an in-process feature probe.
"""

from __future__ import annotations

import base64
import ctypes.util
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from cffi import FFI

from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .ffi.api import ffi, lib

type EvidenceValue = str | int | float | bool | None


class GetterReceipt(TypedDict):
    """An observed value and the public native function that returned it."""

    getter: str
    value: EvidenceValue


type GetterEvidence = dict[str, GetterReceipt]


@dataclass
class NativeSetup:
    """Keep borrowed native arguments alive until the owning test is freed."""

    references: tuple[Any, ...] = field(repr=False)
    evidence: GetterEvidence
    native_version: str


_INTEGER_SETTERS = {
    "port": "iperf_set_test_server_port",
    "parallel": "iperf_set_test_num_streams",
    "omit": "iperf_set_test_omit",
    "tos": "iperf_set_test_tos",
    "client_port": "iperf_set_test_bind_port",
    "socket_buffer_bytes": "iperf_set_test_socket_bufsize",
    "mss": "iperf_set_test_mss",
    "connect_timeout_ms": "iperf_set_test_connect_timeout",
    "bytes_to_send": "iperf_set_test_bytes",
    "blocks_to_send": "iperf_set_test_blocks",
    "burst_packets": "iperf_set_test_burst",
    "pacing_timer_us": "iperf_set_test_pacing_timer",
}
_BOOLEAN_SETTERS = {
    "no_delay": "iperf_set_test_no_delay",
    "zerocopy": "iperf_set_test_zerocopy",
    "udp_counters_64bit": "iperf_set_test_udp_counters_64bit",
    "dont_fragment": "iperf_set_dont_fragment",
    "repeating_payload": "iperf_set_test_repeating_payload",
    "get_server_output": "iperf_set_test_get_server_output",
}
_STRING_SETTERS = {
    "bind_address": "iperf_set_test_bind_address",
    "bind_device": "iperf_set_test_bind_dev",
    "congestion_control": "iperf_set_test_congestion_control",
    "extra_data": "iperf_set_test_extra_data",
}
_GETTERS = {
    "port": ("iperf_get_test_server_port", "int"),
    "server": ("iperf_get_test_server_hostname", "str"),
    "protocol": ("iperf_get_test_protocol_id", "protocol"),
    "duration": ("iperf_get_test_duration", "int"),
    "parallel": ("iperf_get_test_num_streams", "int"),
    "omit": ("iperf_get_test_omit", "int"),
    "blksize": ("iperf_get_test_blksize", "int"),
    "rate": ("iperf_get_test_rate", "int"),
    "tos": ("iperf_get_test_tos", "int"),
    "reverse": ("iperf_get_test_reverse", "bool"),
    "bidirectional": ("iperf_get_test_bidirectional", "bool"),
    "bind_address": ("iperf_get_test_bind_address", "str"),
    "bind_device": ("iperf_get_test_bind_dev", "str"),
    "client_port": ("iperf_get_test_bind_port", "int"),
    "socket_buffer_bytes": ("iperf_get_test_socket_bufsize", "int"),
    "congestion_control": ("iperf_get_test_congestion_control", "str"),
    "no_delay": ("iperf_get_test_no_delay", "bool"),
    "mss": ("iperf_get_test_mss", "int"),
    "connect_timeout_ms": ("iperf_get_test_connect_timeout", "int"),
    "bytes_to_send": ("iperf_get_test_bytes", "int"),
    "blocks_to_send": ("iperf_get_test_blocks", "int"),
    "interval_seconds": ("iperf_get_test_stats_interval", "float"),
    "pacing_timer_us": ("iperf_get_test_pacing_timer", "int"),
    "burst_packets": ("iperf_get_test_burst", "int"),
    "zerocopy": ("iperf_get_test_zerocopy", "bool"),
    "udp_counters_64bit": ("iperf_get_test_udp_counters_64bit", "bool"),
    "dont_fragment": ("iperf_get_dont_fragment", "bool"),
    "repeating_payload": ("iperf_get_test_repeating_payload", "bool"),
    "get_server_output": ("iperf_get_test_get_server_output", "bool"),
    # Both supported versions export this accessor; their header drops "test".
    "extra_data": ("iperf_get_test_extra_data", "str"),
    "json_stream": ("iperf_get_test_json_stream", "bool"),
}
_VALUE_ARGUMENTS = {
    "fq_rate_bps": "--fq-rate",
    "flow_label": "--flowlabel",
    "sctp_streams": "--nstreams",
    "payload_file": "--file",
    "title": "--title",
    "receive_timeout_ms": "--rcv-timeout",
    "send_timeout_ms": "--snd-timeout",
    "idle_timeout_seconds": "--idle-timeout",
    "max_duration_seconds": "--server-max-duration",
}
_CRYPTO_CDEF = """
typedef struct bio_st BIO;
typedef struct evp_pkey_st EVP_PKEY;
typedef int pem_password_cb(char *, int, int, void *);
BIO *BIO_new_mem_buf(const void *, int);
int BIO_free(BIO *);
EVP_PKEY *PEM_read_bio_PUBKEY(BIO *, EVP_PKEY **, pem_password_cb *, void *);
EVP_PKEY *PEM_read_bio_PrivateKey(BIO *, EVP_PKEY **, pem_password_cb *, void *);
int EVP_PKEY_get_base_id(const EVP_PKEY *);
int EVP_PKEY_base_id(const EVP_PKEY *);
void EVP_PKEY_free(EVP_PKEY *);
"""


def _validated_options(role: str, options: dict[str, Any]) -> dict[str, Any]:
    options = dict(options)
    # JSON transports tuples as arrays. Restore only the explicitly typed fields.
    for name in ("sctp_bind_addresses", "control_keepalive"):
        if isinstance(options.get(name), list):
            options[name] = tuple(options[name])
    if role == "client":
        from .config import ClientConfig

        return asdict(ClientConfig(**options))
    if role == "server":
        from .server_config import ServerConfig

        return asdict(ServerConfig(**options))
    raise ValueError("role must be 'client' or 'server'")


def _symbol(name: str) -> Any:
    try:
        return getattr(lib, name)
    except AttributeError as exc:
        raise UnsupportedFeatureError(f"loaded libiperf does not expose {name}") from exc


def _string(value: str, references: list[Any]) -> Any:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("native strings must be strings without NUL characters")
    pointer = ffi.new("char[]", value.encode("utf-8"))
    references.append(pointer)
    return pointer


def _native_version() -> tuple[str, tuple[int, ...] | None]:
    pointer = _symbol("iperf_get_iperf_version")()
    if pointer == ffi.NULL:
        raise IperfLibraryError("libiperf returned no version string")
    version = ffi.string(pointer).decode("utf-8", errors="replace")
    match = re.fullmatch(r"(?:iperf\s+)?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", version)
    numbers = tuple(int(part or 0) for part in match.groups()) if match else None
    return version, numbers


def _check_version(role: str, options: dict[str, Any], version: tuple[int, ...] | None) -> None:
    for name in ("gsro", "max_duration_seconds"):
        value = options.get(name)
        requested = value is not None and (name != "gsro" or value)
        if requested and (version is None or version < (3, 21, 0)):
            raise UnsupportedFeatureError(f"{name} requires libiperf 3.21 or newer")
    if role == "client" and options.get("use_pkcs1_padding"):
        if version is None or version >= (3, 21, 0):
            raise UnsupportedFeatureError(
                "libiperf 3.21 or newer accepts PKCS1 padding only in server mode"
            )
    for name in ("fq_rate_bps", "bitrate_limit_bps"):
        value = options.get(name)
        if value is not None and value > 2**53 - 1:
            raise ValueError(f"{name} exceeds the exact integer range of the native parser")
    if options.get("bitrate_limit_bps") and options.get("interval_seconds") == 0:
        raise ValueError("a server bitrate limit requires a positive reporting interval")


def _arguments(role: str, options: dict[str, Any]) -> list[str]:
    args = ["iperf3-lib", "--json"]
    if role == "client":
        args.extend(("--client", str(options["server"])))
        protocol = options["protocol"]
        if protocol == "udp":
            args.append("--udp")
        elif protocol == "sctp":
            args.append("--sctp")
        duration = options["duration"]
        args.extend(("--time", str(0 if duration is None else duration)))
        if options.get("blksize") is not None:
            args.extend(("--length", str(options["blksize"])))
        if options.get("reverse"):
            args.append("--reverse")
        if options.get("bidirectional"):
            args.append("--bidir")
    else:
        args.append("--server")
    family = options.get("address_family", "auto")
    if family != "auto":
        args.append("--version4" if family == "ipv4" else "--version6")
    interval = options.get("interval_seconds")
    if interval is not None:
        args.extend(("--interval", str(interval)))
    for name, flag in _VALUE_ARGUMENTS.items():
        value = options.get(name)
        if value is not None:
            args.extend((flag, str(value)))
    for name, flag in (
        ("mptcp", "--mptcp"),
        ("skip_rx_copy", "--skip-rx-copy"),
        ("gsro", "--gsro"),
        ("json_stream", "--json-stream"),
        ("use_pkcs1_padding", "--use-pkcs1-padding"),
    ):
        if options.get(name):
            args.append(flag)
    for address in options.get("sctp_bind_addresses", ()):
        args.extend(("--xbind", address))
    affinity = options.get("affinity")
    if affinity is not None:
        value = str(affinity)
        if options.get("server_affinity") is not None:
            value += f",{options['server_affinity']}"
        args.extend(("--affinity", value))
    keepalive = options.get("control_keepalive")
    if keepalive is not None:
        args.append("--cntl-ka=" + "/".join(str(value) for value in keepalive))
    limit = options.get("bitrate_limit_bps")
    if limit is not None:
        value = str(limit)
        window = options.get("bitrate_limit_interval_seconds")
        if window is not None:
            value += f"/{window}"
        args.extend(("--server-bitrate-limit", value))
    return args


def _apply_setters(test: Any, options: dict[str, Any], references: list[Any]) -> None:
    for name, symbol in _INTEGER_SETTERS.items():
        value = options.get(name)
        if value is not None:
            _symbol(symbol)(test, value)
    for name, symbol in _BOOLEAN_SETTERS.items():
        if options.get(name):
            if name == "zerocopy" and not _symbol("iperf_has_zerocopy")():
                raise UnsupportedFeatureError("loaded libiperf does not support zerocopy")
            _symbol(symbol)(test, 1)
    for name, symbol in _STRING_SETTERS.items():
        value = options.get(name)
        if value is not None:
            _symbol(symbol)(test, _string(value, references))
    rate = options.get("rate")
    if rate is not None or options.get("sctp_streams") is not None:
        # Both supported parsers fall through from --nstreams into --bitrate.
        # Apply the admitted rate exactly once, including SCTP's default zero.
        _symbol("iperf_set_test_rate")(test, 0 if rate is None else rate)


def _crypto_library() -> tuple[Any, Any]:
    crypto_ffi = FFI()
    crypto_ffi.cdef(_CRYPTO_CDEF)
    candidates = (
        ctypes.util.find_library("crypto"),
        "libcrypto.so.3",
        "libcrypto.so.1.1",
        "libcrypto.dylib",
        "libcrypto-3-x64.dll",
    )
    for candidate in dict.fromkeys(candidates):
        if candidate:
            try:
                return crypto_ffi, crypto_ffi.dlopen(candidate)
            except OSError:
                pass
    raise UnsupportedFeatureError("RSA authentication requires a loadable OpenSSL crypto library")


def _validated_key(path: str, *, private: bool) -> bytes:
    try:
        key = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError("authentication key file could not be read") from exc
    if not key or b"ENCRYPTED" in key:
        raise ValueError("authentication requires a nonempty, unencrypted PEM RSA key")
    crypto_ffi, crypto = _crypto_library()
    buffer = crypto_ffi.new("char[]", key)
    bio = crypto.BIO_new_mem_buf(buffer, len(key))
    if bio == crypto_ffi.NULL:
        raise IperfLibraryError("OpenSSL could not allocate a key reader")

    @crypto_ffi.callback("int(char *, int, int, void *)")
    def no_password(_buffer: Any, _size: int, _writing: int, _context: Any) -> int:
        return 0

    parsed = crypto_ffi.NULL
    try:
        reader = crypto.PEM_read_bio_PrivateKey if private else crypto.PEM_read_bio_PUBKEY
        parsed = reader(bio, crypto_ffi.NULL, no_password, crypto_ffi.NULL)
        if parsed == crypto_ffi.NULL:
            raise ValueError("authentication key is not a valid unencrypted PEM key")
        try:
            key_type = crypto.EVP_PKEY_get_base_id(parsed)
        except AttributeError:
            try:
                key_type = crypto.EVP_PKEY_base_id(parsed)
            except AttributeError as exc:
                raise UnsupportedFeatureError(
                    "OpenSSL cannot report the authentication key type"
                ) from exc
        # EVP_PKEY_RSA == NID_rsaEncryption in OpenSSL's public headers.
        if key_type != 6:
            raise ValueError("authentication requires an RSA key")
    finally:
        if parsed != crypto_ffi.NULL:
            crypto.EVP_PKEY_free(parsed)
        crypto.BIO_free(bio)
    return key


def _apply_auth(
    test: Any, role: str, options: dict[str, Any], password: str | None, references: list[Any]
) -> None:
    if role != "client" and password is not None:
        raise ValueError("password is only valid for client authentication")
    key_path = options.get("rsa_public_key_path" if role == "client" else "rsa_private_key_path")
    if key_path is None:
        if password is not None:
            raise ValueError("password requires client authentication configuration")
        return
    if role == "client" and password is None:
        raise ValueError("client authentication requires an explicit password")
    key = _validated_key(key_path, private=role == "server")
    key_symbol = (
        "iperf_set_test_client_rsa_pubkey"
        if role == "client"
        else "iperf_set_test_server_rsa_privkey"
    )
    _symbol(key_symbol)(test, _string(base64.b64encode(key).decode("ascii"), references))
    if role == "client":
        _symbol("iperf_set_test_client_username")(test, _string(options["username"], references))
        _symbol("iperf_set_test_client_password")(test, _string(password or "", references))
    else:
        users = options["authorized_users_path"]
        try:
            with Path(users).open("rb"):
                pass
        except OSError as exc:
            raise ValueError("authorized-users file could not be read") from exc
        _symbol("iperf_set_test_server_authorized_users")(test, _string(users, references))
        skew = options.get("time_skew_threshold_seconds")
        _symbol("iperf_set_test_server_skew_threshold")(test, 10 if skew is None else skew)


def _getter_evidence(test: Any, options: dict[str, Any]) -> GetterEvidence:
    evidence: GetterEvidence = {}
    for name, (symbol, kind) in _GETTERS.items():
        if name not in options:
            continue
        getter = _symbol(symbol)
        observed = getter(test)
        if kind == "str":
            value: EvidenceValue = (
                None
                if observed == ffi.NULL
                else ffi.string(observed).decode("utf-8", errors="replace")
            )
        elif kind == "bool":
            value = bool(observed)
        elif kind == "float":
            value = float(observed)
        elif kind == "protocol":
            value = {1: "tcp", 2: "udp", 12: "sctp"}.get(int(observed), "unknown")
        else:
            value = int(observed)
        evidence[name] = {"getter": symbol, "value": value}
    return evidence


def configure_native(
    test: Any, role: str, options: dict[str, Any], *, password: str | None = None
) -> NativeSetup:
    """Apply typed options to a fresh defaulted native test inside its worker.

    Args:
        test: An allocated test on which ``iperf_defaults`` already succeeded.
        role: ``client`` or ``server``.
        options: Complete or partial constructor fields for the selected config.
        password: Explicit client credential, never included in arguments/evidence.

    Returns:
        References that must remain alive through ``iperf_free_test`` and public
        getter observations. Parser acceptance is not reported as observation.

    Raises:
        ValueError: The typed configuration or supported combination is invalid.
        UnsupportedFeatureError: A required native version or API is unavailable.
        IperfError: The native parser returned an error.
    """
    admitted = _validated_options(role, options)
    version, numbers = _native_version()
    _check_version(role, admitted, numbers)
    references: list[Any] = []
    arguments = [_string(argument, references) for argument in _arguments(role, admitted)]
    argv = ffi.new("char *[]", [*arguments, ffi.NULL])
    references.append(argv)
    rc = _symbol("iperf_parse_arguments")(test, len(arguments), argv)
    if rc < 0:
        error = ffi.string(lib.iperf_strerror(lib.i_errno)).decode("utf-8", errors="replace")
        raise IperfError(f"native configuration failed: {error}")
    if admitted.get("json_stream") and numbers is not None and numbers >= (3, 21, 0):
        # Retain the authoritative final document as well as interval events.
        # Earlier native versions expose only streamed event envelopes.
        _symbol("iperf_set_test_json_stream_full_output")(test, 1)
    _apply_setters(test, admitted, references)
    _apply_auth(test, role, admitted, password, references)
    return NativeSetup(tuple(references), _getter_evidence(test, admitted), version)


def observe_native_options(test: Any, options: dict[str, Any]) -> GetterEvidence:
    """Read current public getter values after native parameter exchange/run.

    In particular, a client can replace a server's extra-data value during
    negotiation. Fresh observations must describe that actual value rather
    than retain only the server's pre-run configuration receipt.

    ``options`` must be the admitted fields retained by the owning worker.
    """
    return _getter_evidence(test, options)
