"""Client wrapper and helpers for running iperf3 client tests."""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from ._event_capture import BoundedDocumentCapture
from .config import ClientConfig, Protocol, config_to_dict, requires_worker
from .events import NativeEvent
from .exceptions import IperfError, IperfLibraryError, UnsupportedFeatureError
from .ffi.api import ffi, lib
from .intent import RateIntent, resolve_rate
from .result import Diagnostic, ExecutionMetadata, Result, VerifiedSetting, result_from_iperf_json

if TYPE_CHECKING:
    from ._cancellation import _ExecutionControl
    from ._event_stream import EventStream
    from .events import LiveEvent

TCP_PROTOCOL_ID = 1
UDP_PROTOCOL_ID = 2
SCTP_PROTOCOL_ID = 12

DEFAULT_UDP_BLOCK_SIZE = 0  # select the control connection's MSS, with libiperf fallback
DEFAULT_SCTP_BLOCK_SIZE = 64 * 1024

PROTOCOL_IDS = {
    Protocol.TCP: TCP_PROTOCOL_ID,
    Protocol.UDP: UDP_PROTOCOL_ID,
    Protocol.SCTP: SCTP_PROTOCOL_ID,
}


def _check(ret: int) -> None:
    """Raise IperfError if the return code is negative."""
    if ret < 0:
        err = lib.iperf_strerror(lib.i_errno)
        msg = ffi.string(err).decode() if err != ffi.NULL else "unknown libiperf error"
        raise IperfError(msg)


def _set_str(setter: Callable, t, s: str) -> None:
    """Call a CFFI string setter with a Python string argument."""
    setter(t, ffi.new("char[]", s.encode()))


def _try_set(sym: str) -> Callable | None:
    """Return a lib function if it exists, else None."""
    try:
        return getattr(lib, sym)
    except AttributeError:
        return None


def _maybe_set(setter_name: str, t, value: int) -> bool:
    """Try to set a value using a setter if available; return True if set."""
    fn = _try_set(setter_name)
    if fn:
        fn(t, int(value))
        return True
    return False


def _install_json_callback(t) -> tuple[BoundedDocumentCapture, object | None]:
    """Install libiperf's JSON callback when both the library and FFI support it.

    Minimal Python test doubles commonly omit ``ffi.callback``. In that case,
    retain the legacy getter-based behavior so unit tests do not need to emulate
    native callback machinery.
    """
    setter = _try_set("iperf_set_test_json_callback")
    callback_factory = getattr(ffi, "callback", None)
    capture = BoundedDocumentCapture(ffi)
    if setter is None or callback_factory is None:
        return capture, None

    callback = callback_factory("void(iperf_test *, char *)", capture.capture)
    setter(t, callback)
    return capture, callback


class Client:
    """Client for running iperf3 tests using the provided configuration."""

    def __init__(
        self,
        cfg: ClientConfig,
        *,
        rate_intent: RateIntent | None = None,
        password: str | None = None,
    ):
        """Initialize the client with a configuration object."""
        self.cfg = cfg
        self.rate_intent = rate_intent
        if password is not None and (not isinstance(password, str) or "\x00" in password):
            raise TypeError("password must be a NUL-free string or None")
        self._password = password

    def run(
        self,
        *,
        timeout: float | None = None,
        on_event: Callable[[NativeEvent], None] | None = None,
    ) -> Result:
        """Run the iperf3 test synchronously and return the result."""
        return self._run(timeout=timeout, on_event=on_event)

    def _run(
        self,
        *,
        timeout: float | None = None,
        on_event: Callable[[NativeEvent], None] | None = None,
        _control: _ExecutionControl | None = None,
        _on_live_event: Callable[[LiveEvent], None] | None = None,
    ) -> Result:
        """Share admission and result metadata across direct and isolated execution."""
        cfg = replace(self.cfg)
        if self._password is not None and cfg.username is None:
            raise ValueError("password requires username and rsa_public_key_path")
        caller_config = config_to_dict(cfg)
        if on_event is not None or _on_live_event is not None:
            if on_event is not None and not callable(on_event):
                raise TypeError("on_event must be callable")
            cfg = replace(cfg, json_stream=True)
        admitted_intent = replace(self.rate_intent) if self.rate_intent is not None else None
        resolved = resolve_rate(cfg, admitted_intent)
        cfg = replace(cfg, rate=resolved.native_per_stream_bps)
        requested = config_to_dict(cfg)
        run_started_at = time.time()
        run_started_monotonic = time.monotonic()

        def finish(result: Result) -> Result:
            """Attach observed operation timing and the admitted configuration snapshot."""
            completed_at = time.time()
            elapsed = time.monotonic() - run_started_monotonic
            metadata = result.execution
            if metadata is None:
                metadata = ExecutionMetadata(status="completed" if result.ok else "failed")
                result.execution = metadata
            metadata.timing.started_at_seconds = run_started_at
            metadata.timing.completed_at_seconds = completed_at
            metadata.timing.elapsed_seconds = elapsed
            metadata.timing.requested_duration_seconds = cfg.duration
            metadata.configuration.requested = requested
            if (
                cfg.username is not None
                and not result.ok
                and metadata.native_version in {"3.19.1", "iperf 3.19.1"}
            ):
                result.diagnostics.append(
                    Diagnostic(
                        "libiperf 3.19.1 with OpenSSL 3 can reject valid authentication credentials due to a native encryption bug; authentication is qualified with libiperf 3.22.",
                        "warning",
                        code="execution.native_authentication_compatibility",
                    )
                )
            receipts = result.extensions.get("iperf3_lib.native_configuration", {})
            if isinstance(receipts, dict):
                for name, receipt in receipts.items():
                    if (
                        name in requested
                        and isinstance(receipt, dict)
                        and receipt.get("value") is not None
                    ):
                        existing = metadata.configuration.effective.get(name)
                        conflict = any(
                            diagnostic.code == "configuration.conflict"
                            and diagnostic.path == f"/execution/configuration/effective/{name}"
                            for diagnostic in result.diagnostics
                        )
                        if existing is None or (existing.state == "unavailable" and not conflict):
                            metadata.configuration.effective[name] = VerifiedSetting(
                                receipt["value"],
                                "verified",
                                [f"/extensions/iperf3_lib.native_configuration/{name}/value"],
                            )
            result.extensions["iperf3_lib.rate_intent"] = {
                "schema_version": 1,
                "caller_config": caller_config,
                "intent": asdict(admitted_intent) if admitted_intent is not None else None,
                "resolution": resolved.to_dict(),
            }
            for name in requested:
                metadata.configuration.effective.setdefault(name, VerifiedSetting())
            metadata.python_version = platform.python_version()
            metadata.platform = platform.platform()
            result.completed_at_seconds = completed_at
            if result.started_at_seconds is None:
                result.started_at_seconds = run_started_at
            if not result.raw:
                result.reporting_role = "client"
            for name, setting in metadata.configuration.effective.items():
                intent = requested.get(name)
                if setting.state == "verified" and intent is not None and setting.value != intent:
                    result.diagnostics.append(
                        Diagnostic(
                            f"Native setting {name} differs from the recorded request.",
                            "warning",
                            code="configuration.difference",
                            path=f"/execution/configuration/effective/{name}",
                            evidence_paths=[
                                f"/execution/configuration/requested/{name}",
                                *setting.evidence_paths,
                            ],
                        )
                    )
            return result

        if _control is not None or requires_worker(cfg) or timeout is not None:
            import os

            from ._execution import run_worker

            password = self._password
            if cfg.username is not None and password is None:
                password = os.environ.get("IPERF3_PASSWORD")
            if cfg.username is not None and password is None:
                raise ValueError("authenticated clients require password= or IPERF3_PASSWORD")
            execution_options = {"_control": _control} if _control is not None else {}
            if _on_live_event is not None:
                execution_options["_on_live_event"] = _on_live_event
            return finish(
                run_worker(
                    "client",
                    requested,
                    password=password,
                    timeout=timeout,
                    on_event=on_event,
                    **execution_options,
                )
            )

        t = lib.iperf_new_test()
        if t == ffi.NULL:
            raise IperfLibraryError("iperf_new_test failed")
        try:
            _check(lib.iperf_defaults(t))
            lib.iperf_set_test_role(t, b"c")
            _set_str(lib.iperf_set_test_server_hostname, t, str(cfg.server))
            lib.iperf_set_test_server_port(t, int(cfg.port))
            assert cfg.duration is not None
            lib.iperf_set_test_duration(t, int(cfg.duration))

            protocol_id = PROTOCOL_IDS[cfg.protocol]
            protocol_setter = _try_set("set_protocol")
            protocol_getter = _try_set("iperf_get_test_protocol_id")
            if protocol_setter is None or protocol_getter is None:
                raise UnsupportedFeatureError(
                    "This libiperf lacks the public protocol selection API"
                )
            _check(protocol_setter(t, protocol_id))
            if int(protocol_getter(t)) != protocol_id:
                raise IperfLibraryError(
                    f"libiperf did not apply requested protocol {cfg.protocol.value}"
                )

            if cfg.omit:
                lib.iperf_set_test_omit(t, int(cfg.omit))
            if cfg.parallel and cfg.parallel > 1:
                lib.iperf_set_test_num_streams(t, int(cfg.parallel))

            block_size = cfg.blksize
            if block_size is None and cfg.protocol is Protocol.UDP:
                block_size = DEFAULT_UDP_BLOCK_SIZE
            elif block_size is None and cfg.protocol is Protocol.SCTP:
                block_size = DEFAULT_SCTP_BLOCK_SIZE
            if block_size is not None:
                lib.iperf_set_test_blksize(t, int(block_size))

            if cfg.tos is not None:
                lib.iperf_set_test_tos(t, int(cfg.tos))

            # reverse / bidirectional
            if cfg.reverse:
                lib.iperf_set_test_reverse(t, 1)
            if cfg.bidirectional:
                if not _maybe_set("iperf_set_test_bidirectional", t, 1):
                    raise UnsupportedFeatureError("Bidirectional not supported by this libiperf")

            rate = cfg.rate
            if rate is not None:
                rate_setter = _try_set("iperf_set_test_rate")
                if rate_setter is None:
                    raise UnsupportedFeatureError("This libiperf lacks the public rate setter")
                rate_setter(t, int(rate))

            # ensure JSON output
            if not _maybe_set("iperf_set_test_json_output", t, 1):
                # some extremely old libs may lack JSON setter; we rely on JSON for parsing
                raise UnsupportedFeatureError("This libiperf lacks JSON output support")

            capture, callback = _install_json_callback(t)
            native_error = None
            try:
                _check(lib.iperf_run_client(t))
            except IperfError as exc:
                # Read process-global error text before any further native operation.
                native_error = str(exc)
            # Keep the cdata callback alive through iperf_run_client().
            _ = callback

            capture.finalize()
            native_error = native_error or capture.native_error
            json_bytes = capture.payload if capture.error is None else None
            if json_bytes is None:
                cjson = lib.iperf_get_test_json_output_string(t)
                json_bytes = capture.read(cjson)

            if json_bytes is not None:
                raw = capture.parse_document(json_bytes)
                result = result_from_iperf_json(raw, reporting_role="client")
                if capture.error is not None:
                    result.diagnostics.append(
                        Diagnostic(
                            f"{capture.error}; final JSON was recovered from the bounded getter.",
                            "warning",
                            code="execution.capture_recovered",
                        )
                    )
                if native_error is not None:
                    result.ok = False
                    result.error = native_error
                    if result.execution is not None:
                        result.execution.status = "failed"
                    result.diagnostics.append(
                        Diagnostic(native_error, "error", code="execution.native_error")
                    )
                return finish(result)
            return finish(
                Result(
                    ok=False,
                    error=native_error or capture.error or "No JSON returned by libiperf",
                    execution=ExecutionMetadata(status="failed" if native_error else "incomplete"),
                )
            )
        except IperfError as e:
            return finish(Result(ok=False, error=str(e)))
        finally:
            lib.iperf_free_test(t)

    def events(self, *, timeout: float | None = None) -> EventStream:
        """Create an owned live-event context with a complete result on completion.

        Configuration is detached on context entry. Exiting the context stops
        unfinished work and waits for cleanup, including after an early break.
        """
        from ._event_stream import EventStream

        def admit():
            client = Client(
                replace(self.cfg),
                rate_intent=replace(self.rate_intent) if self.rate_intent is not None else None,
                password=self._password,
            )
            return lambda control, sink: client._run(
                timeout=timeout, _control=control, _on_live_event=sink
            )

        return EventStream(admit)

    async def arun(
        self,
        *,
        timeout: float | None = None,
        on_event: Callable[[NativeEvent], None] | None = None,
    ) -> Result:
        """Run in an isolated worker, cleaning it up before propagating cancellation.

        A callback already running must return before this await finishes.
        Worker setup failures raise exceptions; completed native failures retain
        unsuccessful results. This method uses the built-in isolated execution
        path rather than delegating to an overridden ``run()`` method.
        """
        from ._cancellation import _run_async

        return await _run_async(
            lambda control: self._run(timeout=timeout, on_event=on_event, _control=control)
        )
