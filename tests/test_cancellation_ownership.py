"""Retained worker ownership when neither cleanup nor a reaper can start."""

import gc
import threading
import weakref

import pytest

from iperf3_lib import _cancellation
from iperf3_lib.exceptions import IperfCleanupError


def test_failed_reaper_start_retains_ownership_after_exception_is_discarded(monkeypatch):
    """Library ownership survives caller GC and ends only after confirmed reaping."""
    exited = threading.Event()
    admission = threading.Lock()
    admission.acquire()
    starts = []

    class Process:
        """Fail shutdown until the test supplies an eventual native exit."""

        def poll(self):
            return 0 if exited.is_set() else None

        def terminate(self):
            raise OSError("termination unavailable")

        def kill(self):
            raise OSError("kill unavailable")

        def wait(self, timeout):
            assert exited.is_set(), "unexpected blocking reap"
            return 0

    def fail_start(thread):
        starts.append(thread._target.__name__)
        raise RuntimeError("thread startup unavailable")

    def discard_failed_operation():
        control = _cancellation._ExecutionControl()
        process = Process()
        references = weakref.ref(control), weakref.ref(process)
        try:
            control.register(process)
        except RuntimeError as exc:
            control.abort(exc)
        control.when_reaped(admission.release)
        with pytest.raises(IperfCleanupError, match="ownership is retained") as captured:
            control.close()
        assert "Background reaper could not start" in captured.value.__notes__
        return references

    with monkeypatch.context() as patcher:
        patcher.setattr(threading.Thread, "start", fail_start)
        control_ref, process_ref = discard_failed_operation()

    try:
        gc.collect()
        retained_control = control_ref()
        assert retained_control is not None
        assert process_ref() is not None
        assert retained_control in _cancellation._RETAINED_CONTROLS
        assert not retained_control.cleanup_confirmed
        assert admission.locked()
        assert starts == ["_supervise", "_reap_later"]
    finally:
        exited.set()
        if control_ref() is not None:
            control_ref()._reap_later()

    assert retained_control.cleanup_confirmed
    assert retained_control not in _cancellation._RETAINED_CONTROLS
    assert not admission.locked()
