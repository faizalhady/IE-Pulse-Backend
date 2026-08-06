"""
keep_awake.py  (cycle_time)
───────────────────────────
Keep Windows awake for the duration of a long ingest so the machine can be
left unattended without the network connection being torn down by sleep.

Background: a long paginated pull (LAMRESEARCH ≈ 1,056 pages, KEYSIGHT more)
can run for hours. If the laptop goes to idle-sleep mid-run, the OS suspends
the network stack and any in-flight socket dies with
`ConnectionResetError(10054, 'An existing connection was forcibly closed')`.

This uses the Win32 `SetThreadExecutionState` API to tell Windows "a task is
running, don't idle-sleep the system or turn off the display" for as long as
the calling thread holds the flag. The flag is released automatically on exit
(including on crash / Ctrl+C), so normal power behaviour resumes afterward.

Caveats:
  - Idle-sleep is prevented. Sleep triggered by CLOSING THE LID is a separate
    hardware/policy action that this API does NOT override on most laptops —
    keep the lid open, or set the AC lid-close action to "Do nothing".
  - No-op on non-Windows platforms (returns a null context).
"""

import ctypes
import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Win32 EXECUTION_STATE flags
_ES_CONTINUOUS       = 0x80000000  # keep the state in effect until cleared
_ES_SYSTEM_REQUIRED  = 0x00000001  # forbid idle system-sleep
_ES_DISPLAY_REQUIRED = 0x00000002  # keep the display on (optional, harmless)


@contextmanager
def keep_system_awake(keep_display: bool = False):
    """Context manager: prevent Windows idle-sleep while the block runs.

    Usage:
        with keep_system_awake():
            long_running_ingest()
    """
    is_windows = hasattr(ctypes, "windll")
    if not is_windows:
        # Non-Windows: nothing to do.
        yield
        return

    flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
    if keep_display:
        flags |= _ES_DISPLAY_REQUIRED

    prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    if prev == 0:
        log.warning("Could not set keep-awake state; system may still sleep mid-run.")
    else:
        log.info("Keep-awake ON - system idle-sleep blocked for the duration of this run.")
        log.info("  (Lid-close sleep is NOT covered - keep the lid open or set lid action to 'Do nothing'.)")

    try:
        yield
    finally:
        # Clear the flag — restore normal power behaviour.
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        if prev != 0:
            log.info("Keep-awake OFF - normal power behaviour restored.")
