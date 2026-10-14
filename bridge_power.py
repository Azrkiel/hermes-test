"""Thread-scoped Windows system-sleep prevention, without policy changes."""

import ctypes
import sys
import threading
from contextlib import contextmanager
from ctypes import wintypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _load_execution_state():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.SetThreadExecutionState
    function.argtypes = [wintypes.DWORD]
    function.restype = wintypes.DWORD
    return function


@contextmanager
def keep_awake():
    """Prevent Windows idle system sleep for a same-thread synchronous scope.

    Yield supported/active booleans; non-Windows platforms yield both false.
    Acquisition or restoration refusal raises RuntimeError. The returned active
    flag becomes false only after successful restoration of the prior state.
    Enter and exit on the same thread (do not transfer the context to another
    thread); Windows also releases the request when that thread/process exits.

    This requests no display wakefulness, changes no global power policy, and
    requires no admin rights. It does not override explicit user/OS suspension.
    """
    if sys.platform != "win32":
        yield {"supported": False, "active": False}
        return

    set_execution_state = _load_execution_state()
    owner = threading.get_ident()
    previous = set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if not previous:
        raise RuntimeError("SetThreadExecutionState refused the keep-awake request")
    state = {"supported": True, "active": True}
    try:
        yield state
    finally:
        if threading.get_ident() != owner:
            raise RuntimeError("keep_awake must enter and exit on the same thread")
        if not set_execution_state(previous):
            raise RuntimeError("SetThreadExecutionState refused to restore the prior execution state")
        state["active"] = False
