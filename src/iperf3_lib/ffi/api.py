"""FFI bindings and dynamic loading for libiperf."""

from __future__ import annotations

import os
from typing import Final

from cffi import FFI

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


lib = _dlopen()
