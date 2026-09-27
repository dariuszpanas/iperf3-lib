"""Typed native configuration, argument ownership and public getter tests."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from iperf3_lib import native_options as native
from iperf3_lib.exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError


class FakeNative:
    """Model parser mutations and setters without loading the native library."""

    def __init__(self, version="iperf 3.21"):
        """Initialize deterministic native defaults and call observations."""
        self.i_errno = 0
        self.version = version
        self.calls = []
        self.arguments = []
        self.references = []
        self.parser_result = 0
        self.unavailable = set()
        self.values: dict[str, Any] = {
            name: None if kind == "str" else 0 for name, (_, kind) in native._GETTERS.items()
        }
        self.values.update(
            protocol=1, duration=10, parallel=1, blksize=131072, interval_seconds=1.0
        )
        self.fields = {
            **{symbol: name for name, symbol in native._INTEGER_SETTERS.items()},
            **{symbol: name for name, symbol in native._BOOLEAN_SETTERS.items()},
            **{symbol: name for name, symbol in native._STRING_SETTERS.items()},
            "iperf_set_test_rate": "rate",
        }

    def string(self, value):
        """Keep a native string alive for a simulated getter."""
        pointer = native.ffi.new("char[]", value.encode())
        self.references.append(pointer)
        return pointer

    def iperf_get_iperf_version(self):
        """Return the configured producer version."""
        return self.string(self.version)

    def iperf_strerror(self, code):
        """Return an error without depending on a native error table."""
        return self.string(f"error {code}")

    def iperf_parse_arguments(self, test, argc, argv):
        """Record writable arguments and model the native SCTP fallthrough."""
        assert self.i_errno == 0
        assert argv[argc] == native.ffi.NULL
        self.arguments = [native.ffi.string(argv[index]).decode() for index in range(argc)]
        self.calls.append(("parse", test))
        if "--client" in self.arguments:
            self.values["server"] = self.arguments[self.arguments.index("--client") + 1]
        if "--udp" in self.arguments:
            self.values.update(protocol=2, rate=1048576, blksize=0)
        if "--sctp" in self.arguments:
            self.values.update(protocol=12, blksize=65536)
        if "--nstreams" in self.arguments:
            self.values["rate"] = int(self.arguments[self.arguments.index("--nstreams") + 1])
        for flag, name in (("--time", "duration"), ("--length", "blksize")):
            if flag in self.arguments:
                self.values[name] = int(self.arguments[self.arguments.index(flag) + 1])
        for flag, name in (
            ("--reverse", "reverse"),
            ("--bidir", "bidirectional"),
            ("--json-stream", "json_stream"),
        ):
            self.values[name] = flag in self.arguments
        if "--interval" in self.arguments:
            self.values["interval_seconds"] = float(
                self.arguments[self.arguments.index("--interval") + 1]
            )
        if self.parser_result < 0:
            self.i_errno = 23
        return self.parser_result

    def __getattr__(self, name):
        """Expose only the simulated public symbols needed by the helper."""
        if name in self.unavailable:
            raise AttributeError(name)
        if name == "iperf_has_zerocopy":
            return lambda: True
        for key, (symbol, kind) in native._GETTERS.items():
            if symbol == name:

                def getter(_test, key=key, kind=kind):
                    value = self.values[key]
                    return (
                        self.string(value)
                        if kind == "str" and value is not None
                        else native.ffi.NULL
                        if kind == "str"
                        else value
                    )

                return getter
        if name.startswith("iperf_set_"):

            def setter(_test, value):
                if not isinstance(value, int):
                    value = native.ffi.string(value).decode()
                self.calls.append((name, value))
                if name in self.fields:
                    self.values[self.fields[name]] = value

            return setter
        raise AttributeError(name)


@pytest.fixture
def library(monkeypatch):
    """Replace the lazy native proxy with a recording implementation."""
    instance = FakeNative()
    monkeypatch.setattr(native, "lib", instance)
    return instance


def test_precise_unsigned_values_use_public_setters(library):
    """The parser never rounds a uint64 bitrate or termination count."""
    maximum = 2**64 - 1
    setup = native.configure_native(
        object(),
        "client",
        {
            "server": "localhost",
            "rate": maximum,
            "duration": None,
            "bytes_to_send": maximum,
            "burst_packets": 5,
        },
    )
    assert "--bitrate" not in library.arguments
    assert "--bytes" not in library.arguments
    assert setup.evidence["rate"]["value"] == maximum
    assert setup.evidence["bytes_to_send"]["value"] == maximum
    assert setup.evidence["duration"]["value"] == 0
    assert library.calls.count(("iperf_set_test_rate", maximum)) == 1
    assert library.calls.count(("iperf_set_test_burst", 5)) == 1


@pytest.mark.parametrize("rate", [None, 0, 7_654_321])
def test_sctp_parser_fallthrough_restores_rate_once(library, rate):
    """Native nstreams side effects cannot silently replace the intended rate."""
    setup = native.configure_native(
        object(),
        "client",
        {
            "server": "localhost",
            "protocol": "sctp",
            "sctp_streams": 3,
            "rate": rate,
            "sctp_bind_addresses": ("127.0.0.1", "127.0.0.2"),
        },
    )
    intended = 0 if rate is None else rate
    assert setup.evidence["rate"]["value"] == intended
    assert library.calls.count(("iperf_set_test_rate", intended)) == 1
    assert library.arguments.count("--xbind") == 2


def test_direction_and_block_size_precede_parser_derivation(library):
    """Receive timeout and GSRO observe the configured mode and datagram size."""
    setup = native.configure_native(
        object(),
        "client",
        {
            "server": "localhost",
            "protocol": "udp",
            "reverse": True,
            "blksize": 1024,
            "receive_timeout_ms": 500,
            "gsro": True,
        },
    )
    assert library.arguments.index("--reverse") < library.arguments.index("--rcv-timeout")
    assert "--gsro" in library.arguments
    assert setup.evidence["blksize"]["value"] == 1024
    assert not any(call[0] == "iperf_set_test_reverse" for call in library.calls)


def test_public_string_and_boolean_controls_have_getter_receipts(library):
    """Only observed public getter values are promoted into evidence."""
    setup = native.configure_native(
        object(),
        "client",
        {
            "server": "localhost",
            "bind_address": "127.0.0.1",
            "bind_device": "lo",
            "client_port": 5202,
            "socket_buffer_bytes": 8192,
            "congestion_control": "cubic",
            "no_delay": True,
            "mss": 1200,
            "connect_timeout_ms": 250,
            "pacing_timer_us": 300,
            "get_server_output": True,
            "extra_data": "sample",
            "zerocopy": True,
            "repeating_payload": True,
            "address_family": "ipv4",
            "fq_rate_bps": 2_000_000,
        },
    )
    for name in (
        "bind_address",
        "bind_device",
        "client_port",
        "socket_buffer_bytes",
        "congestion_control",
        "no_delay",
        "mss",
        "connect_timeout_ms",
        "pacing_timer_us",
        "get_server_output",
        "extra_data",
        "zerocopy",
        "repeating_payload",
    ):
        assert setup.evidence[name]["getter"].startswith("iperf_get_")
    assert setup.evidence["bind_device"]["value"] == "lo"
    assert setup.evidence["no_delay"]["value"] is True
    assert "fq_rate_bps" not in setup.evidence
    assert "address_family" not in setup.evidence
    assert "--version4" in library.arguments


def test_parser_only_controls_and_borrowed_path_are_retained(library):
    """Generated argv contains only typed options and owns borrowed filename data."""
    setup = native.configure_native(
        object(),
        "client",
        {
            "server": "localhost",
            "payload_file": "payload.bin",
            "affinity": 2,
            "server_affinity": 3,
            "control_keepalive": (30, 2, 3),
            "title": "--help",
            "json_stream": True,
            "send_timeout_ms": 1000,
        },
    )
    assert "--cntl-ka=30/2/3" in library.arguments
    assert "2,3" in library.arguments
    assert library.arguments[library.arguments.index("--title") + 1] == "--help"
    argv = setup.references[-1]
    assert native.ffi.string(argv[library.arguments.index("--file") + 1]) == b"payload.bin"
    assert "references=" not in repr(setup)


@pytest.mark.parametrize("version", ["iperf 3.19.1", "unknown"])
def test_version_gated_flag_never_reaches_parser(library, version):
    """Known unavailable or unidentifiable producer versions reject new flags."""
    library.version = version
    with pytest.raises(UnsupportedFeatureError, match="3.21"):
        native.configure_native(
            object(), "client", {"server": "localhost", "protocol": "udp", "gsro": True}
        )
    assert library.calls == []


def test_native_parser_failure_is_raised_before_setters(library):
    """Returned native errors preserve the first error before further native calls."""
    library.parser_result = -1
    with pytest.raises(IperfError, match="error 23"):
        native.configure_native(object(), "client", {"server": "localhost"})
    assert [call[0] for call in library.calls] == ["parse"]


def test_missing_public_symbol_is_explicit(library):
    """An absent optional setter produces a support error rather than a fallback."""
    library.unavailable.add("iperf_set_test_bind_dev")
    with pytest.raises(UnsupportedFeatureError, match="bind_dev"):
        native.configure_native(object(), "client", {"server": "localhost", "bind_device": "lo"})


def test_invalid_or_unknown_options_are_rejected_before_native(library):
    """The worker admits config again instead of trusting serialized caller data."""
    with pytest.raises(TypeError):
        native.configure_native(object(), "client", {"server": "localhost", "raw_argv": ["--help"]})
    with pytest.raises(ValueError):
        native.configure_native(object(), "client", {"server": "localhost\0ignored"})
    with pytest.raises(ValueError, match="role"):
        native.configure_native(object(), "other", {})
    assert library.calls == []


def test_auth_is_not_passed_through_cli_or_evidence(library, monkeypatch):
    """Explicit credentials use setters and never enable the parser's stdin path."""
    monkeypatch.setattr(native, "_validated_key", lambda *_args, **_kwargs: b"validated public PEM")
    setup = native.configure_native(
        object(),
        "client",
        {"server": "localhost", "username": "tester", "rsa_public_key_path": "public.pem"},
        password="private-password",
    )
    assert "--username" not in library.arguments
    assert "--rsa-public-key-path" not in library.arguments
    assert "private-password" not in str(setup.evidence)
    assert "private-password" not in repr(setup)
    assert ("iperf_set_test_client_password", "private-password") in library.calls


def test_password_is_required_and_not_prompted(library):
    """Auth admission requires an explicit credential rather than the environment."""
    with pytest.raises(ValueError, match="explicit password"):
        native.configure_native(
            object(),
            "client",
            {"server": "localhost", "username": "tester", "rsa_public_key_path": "public.pem"},
        )
    with pytest.raises(ValueError, match="requires client"):
        native.configure_native(object(), "client", {"server": "localhost"}, password="unused")


def test_server_policies_and_metadata_use_admitted_paths(library):
    """Server policy arguments retain units and avoid native CLI process controls."""
    setup = native.configure_native(
        object(),
        "server",
        {
            "bind_address": "127.0.0.1",
            "bind_device": "lo",
            "address_family": "ipv4",
            "interval_seconds": 0.5,
            "idle_timeout_seconds": 1,
            "receive_timeout_ms": 250,
            "send_timeout_ms": 0,
            "bitrate_limit_bps": 2_000_000,
            "bitrate_limit_interval_seconds": 2.5,
            "max_duration_seconds": 3,
            "extra_data": "server-marker",
            "control_keepalive": [30, 2, 3],
        },
    )
    assert "--server" in library.arguments and "--one-off" not in library.arguments
    assert "--extra-data" not in library.arguments
    assert "2000000/2.5" in library.arguments
    assert "--cntl-ka=30/2/3" in library.arguments
    assert setup.evidence["extra_data"]["value"] == "server-marker"
    assert setup.evidence["interval_seconds"]["value"] == 0.5


def test_server_auth_uses_validated_private_key_and_default_skew(library, monkeypatch, tmp_path):
    """Server authentication bypasses parser prompts and records no key contents."""
    users = tmp_path / "users.csv"
    users.write_text("tester,hash", encoding="utf-8")
    monkeypatch.setattr(
        native, "_validated_key", lambda *_args, **_kwargs: b"validated private PEM"
    )
    setup = native.configure_native(
        object(),
        "server",
        {
            "rsa_private_key_path": "private.pem",
            "authorized_users_path": str(users),
            "use_pkcs1_padding": True,
        },
    )
    assert "--use-pkcs1-padding" in library.arguments
    assert "--rsa-private-key-path" not in library.arguments
    assert ("iperf_set_test_server_skew_threshold", 10) in library.calls
    assert "validated private PEM" not in str(setup.evidence)
    with pytest.raises(ValueError, match="only valid for client"):
        native.configure_native(object(), "server", {}, password="unused")


def test_server_missing_users_file_is_explicit(library, monkeypatch, tmp_path):
    """An unreadable authorized-users file cannot silently disable authentication."""
    monkeypatch.setattr(native, "_validated_key", lambda *_args, **_kwargs: b"validated PEM")
    with pytest.raises(ValueError, match="authorized-users"):
        native.configure_native(
            object(),
            "server",
            {
                "rsa_private_key_path": "private.pem",
                "authorized_users_path": str(tmp_path / "missing.csv"),
            },
        )


def test_server_new_version_flag_is_rejected_before_native(library):
    """A disabled-looking explicit new option still requires its public native version."""
    library.version = "3.19.1"
    with pytest.raises(UnsupportedFeatureError, match="3.21"):
        native.configure_native(object(), "server", {"max_duration_seconds": 0})
    assert library.calls == []


@pytest.mark.parametrize("role", ["client", "server"])
@pytest.mark.parametrize(
    ("version", "streaming", "full_output"),
    [
        ("3.19.1", True, False),
        ("3.21", True, True),
        ("3.21", False, False),
        ("unknown", True, False),
    ],
)
def test_streaming_retains_full_output_when_native_supports_it(
    library, role, version, streaming, full_output
):
    """Newer native streams also deliver their authoritative final JSON document."""
    library.version = version
    options = {"json_stream": streaming}
    if role == "client":
        options["server"] = "localhost"
    setup = native.configure_native(object(), role, options)
    calls = [call for call in library.calls if call[0] == "iperf_set_test_json_stream_full_output"]
    assert calls == ([("iperf_set_test_json_stream_full_output", 1)] if full_output else [])
    assert setup.evidence["json_stream"]["value"] is streaming


def test_post_run_receipts_observe_peer_changes(library):
    """Server receipts reflect native negotiation even when the request differs."""
    options = {"extra_data": "server-marker"}
    setup = native.configure_native(object(), "server", options)
    library.values["extra_data"] = "peer-marker"
    observed = native.observe_native_options(object(), options)
    assert observed["extra_data"]["value"] == "peer-marker"
    assert setup.evidence["extra_data"]["value"] == "server-marker"


def test_extra_data_uses_actual_exported_getter_name(library, monkeypatch):
    """Bind the real export independently of the upstream header's misspelled prototype."""
    from iperf3_lib.ffi.symbols import CDEF

    calls = []
    library.unavailable.add("iperf_get_extra_data")

    def getter(test):
        calls.append(test)
        return library.string("actual native value")

    monkeypatch.setattr(library, "iperf_get_test_extra_data", getter)
    test = object()
    setup = native.configure_native(test, "server", {"extra_data": "requested metadata"})
    assert setup.evidence["extra_data"] == {
        "getter": "iperf_get_test_extra_data",
        "value": "actual native value",
    }
    assert calls == [test]
    assert "iperf_get_test_extra_data(" in CDEF
    assert "iperf_get_extra_data(" not in CDEF


def test_client_pkcs1_version_mismatch_is_explicit(library):
    """The newer parser's server-only padding restriction is not bypassed."""
    with pytest.raises(UnsupportedFeatureError, match="server mode"):
        native.configure_native(
            object(),
            "client",
            {
                "server": "localhost",
                "username": "tester",
                "rsa_public_key_path": "public.pem",
                "use_pkcs1_padding": True,
            },
            password="password",
        )
    assert library.calls == []


def test_json_tuple_fields_are_restored_without_mutating_input(library):
    """Worker JSON arrays regain only their declared tuple field semantics."""
    options = {"server": "localhost", "protocol": "sctp", "sctp_bind_addresses": ["127.0.0.1"]}
    native.configure_native(object(), "client", options)
    assert options["sctp_bind_addresses"] == ["127.0.0.1"]
    assert "--xbind" in library.arguments


def test_zerocopy_support_cannot_be_silently_downgraded(library, monkeypatch):
    """The native boolean setter's fallback behavior is exposed as unsupported."""
    monkeypatch.setattr(library, "iperf_has_zerocopy", lambda: False)
    with pytest.raises(UnsupportedFeatureError, match="zerocopy"):
        native.configure_native(object(), "client", {"server": "localhost", "zerocopy": True})


def test_absent_native_version_is_a_library_error(library, monkeypatch):
    """A null version pointer cannot be decoded or used for feature admission."""
    monkeypatch.setattr(library, "iperf_get_iperf_version", lambda: native.ffi.NULL)
    with pytest.raises(IperfLibraryError, match="version"):
        native.configure_native(object(), "client", {"server": "localhost"})


def test_unreadable_key_is_a_configuration_error(tmp_path):
    """Filesystem details do not obscure the configuration error category."""
    with pytest.raises(ValueError, match="could not be read"):
        native._validated_key(str(tmp_path / "missing.pem"), private=False)


def test_crypto_library_absence_is_explicit(monkeypatch):
    """OpenSSL probing stays local and returns a clear support failure."""

    def unavailable(_name):
        raise OSError("not installed")

    monkeypatch.setattr(native.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(
        native, "FFI", lambda: SimpleNamespace(cdef=lambda _source: None, dlopen=unavailable)
    )
    with pytest.raises(UnsupportedFeatureError, match="OpenSSL"):
        native._crypto_library()


@pytest.mark.parametrize("contents", [b"", b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nsecret"])
def test_encrypted_or_empty_key_never_reaches_openssl(tmp_path, monkeypatch, contents):
    """Encrypted-key prompts are prevented before allocating an OpenSSL reader."""
    path = tmp_path / "private.pem"
    path.write_bytes(contents)
    monkeypatch.setattr(native, "_crypto_library", lambda: pytest.fail("must not load OpenSSL"))
    with pytest.raises(ValueError, match="unencrypted"):
        native._validated_key(str(path), private=True)


@pytest.mark.parametrize("valid,rsa", [(False, True), (True, False), (True, True)])
def test_openssl_validation_has_no_password_callback_and_frees(tmp_path, monkeypatch, valid, rsa):
    """PEM parse failure and non-RSA keys are distinct from successful RSA input."""
    path = tmp_path / "key.pem"
    path.write_bytes(b"untrusted PEM")
    calls = []
    fake_ffi = SimpleNamespace(
        NULL=None,
        new=lambda _type, data: data,
        callback=lambda _signature: lambda callback: callback,
    )

    def read(_bio, _key, password_callback, _context):
        assert password_callback(None, 4096, 0, None) == 0
        return object() if valid else None

    crypto = SimpleNamespace(
        BIO_new_mem_buf=lambda *_args: object(),
        BIO_free=lambda _bio: calls.append("bio_free"),
        PEM_read_bio_PrivateKey=read,
        PEM_read_bio_PUBKEY=read,
        EVP_PKEY_get_base_id=lambda _key: 6 if rsa else 408,
        EVP_PKEY_free=lambda _key: calls.append("key_free"),
    )
    monkeypatch.setattr(native, "_crypto_library", lambda: (fake_ffi, crypto))
    if valid and rsa:
        assert native._validated_key(str(path), private=True) == b"untrusted PEM"
    else:
        with pytest.raises(ValueError, match="valid|RSA"):
            native._validated_key(str(path), private=True)
    assert calls == (["key_free", "bio_free"] if valid else ["bio_free"])


@pytest.mark.integration
def test_native_getters_in_disposable_python_process():
    """Real public getters prove configuration without a benchmark or CLI process."""
    script = """
import json
from iperf3_lib.ffi.api import ffi, lib
from iperf3_lib.native_options import configure_native
test = lib.iperf_new_test()
assert test != ffi.NULL
try:
    assert lib.iperf_defaults(test) == 0
    setup = configure_native(test, 'client', {
        'server': '127.0.0.1', 'duration': None, 'bytes_to_send': 2**64 - 1,
        'rate': 2**64 - 1, 'bind_address': '127.0.0.1', 'bind_device': 'lo',
        'client_port': 5202, 'no_delay': True, 'mss': 1200,
        'socket_buffer_bytes': 8192, 'connect_timeout_ms': 250,
        'pacing_timer_us': 500, 'burst_packets': 3, 'interval_seconds': 0.5,
        'get_server_output': True, 'extra_data': 'native-proof',
    })
    print(json.dumps(setup.evidence))
finally:
    lib.iperf_free_test(test)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, timeout=30, check=True
    )
    evidence = json.loads(completed.stdout)
    expected = {
        "bytes_to_send": 2**64 - 1,
        "rate": 2**64 - 1,
        "duration": 0,
        "bind_address": "127.0.0.1",
        "bind_device": "lo",
        "client_port": 5202,
        "no_delay": True,
        "mss": 1200,
        "socket_buffer_bytes": 8192,
        "connect_timeout_ms": 250,
        "pacing_timer_us": 500,
        "burst_packets": 3,
        "interval_seconds": 0.5,
        "get_server_output": True,
        "extra_data": "native-proof",
    }
    assert {key: evidence[key]["value"] for key in expected} == expected
