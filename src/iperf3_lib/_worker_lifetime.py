"""Linux worker bootstrap, executable without importing the iperf3_lib package."""

from __future__ import annotations

import ctypes
import json
import os
import runpy
import signal
import sys

_PR_SET_PDEATHSIG = 1


def protect_parent(expected_pid: int) -> None:
    """Arm kernel termination before package imports and reject a departed parent.

    Linux associates the signal with the thread that spawned this process.
    The calling execution thread owns its worker until cleanup completes; an
    exit of that thread must therefore also end any remaining native work.
    """
    if sys.platform != "linux":
        raise NotImplementedError("Worker parent-death protection requires Linux")
    if type(expected_pid) is not int or expected_pid <= 0:
        raise ValueError("Expected parent PID must be a positive integer")
    library = ctypes.CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"Cannot arm worker parent-death protection: {os.strerror(error)}")
    # The parent may have died before prctl installed the signal. Checking
    # after installation closes that race without a threaded preexec_fn.
    if os.getppid() != expected_pid:
        raise RuntimeError("Worker parent exited before lifetime protection was established")


def main() -> None:
    """Fail closed through the worker protocol before importing any package code."""
    try:
        if len(sys.argv) != 2:
            raise ValueError("Worker bootstrap requires its expected parent PID")
        protect_parent(int(sys.argv[1]))
    except Exception as exc:
        print(json.dumps({"type": "error", "class": "IperfLibraryError", "message": str(exc)}))
        print('{"type":"done"}', flush=True)
        raise SystemExit(1) from exc
    runpy.run_module("iperf3_lib._worker", run_name="__main__")


if __name__ == "__main__":
    main()
