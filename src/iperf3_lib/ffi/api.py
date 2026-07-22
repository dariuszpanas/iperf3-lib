"""FFI bindings and dynamic loading for libiperf."""

from __future__ import annotations

import os
import threading
from typing import Any, Final

from cffi import FFI

from ..exceptions import IperfLibraryError
from .symbols import CDEF

POSSIBLE_NAMES: Final[tuple[str, ...]] = (
    "libiperf.so",
    "libiperf.so.0",  # Linux common soname
    "libiperf.dylib",  # macOS
    "iperf3.dll",
    "libiperf.dll",  # Windows (if available)
)

ffi = FFI()
ffi.cdef(CDEF)


def _dlopen():
    # Prefer a clearer env var key for package consumers
    path = os.getenv("IPERF3_LIB")
    if path:
        return ffi.dlopen(path)
    last_err: Exception | None = None
    for name in POSSIBLE_NAMES:
        try:
            return ffi.dlopen(name)
        except OSError as e:  # noqa: PERF203 - ok in a tiny loop
            last_err = e
    raise OSError(f"Could not load libiperf. Tried {POSSIBLE_NAMES}. Last error: {last_err}")


class _LazyLibrary:
    """Load libiperf on first symbol access instead of during package import."""

    def __init__(self) -> None:
        """Initialize an unloaded, thread-safe library proxy."""
        self._loaded: Any | None = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        """Return the loaded library, translating loader errors for consumers."""
        if self._loaded is not None:
            return self._loaded

        with self._lock:
            if self._loaded is None:
                try:
                    self._loaded = _dlopen()
                except OSError as exc:
                    raise IperfLibraryError(str(exc)) from exc
        return self._loaded

    def __getattr__(self, name: str) -> Any:
        """Resolve a symbol from libiperf, loading the library if necessary."""
        return getattr(self._load(), name)


lib = _LazyLibrary()
