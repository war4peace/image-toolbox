"""
single_instance.py
-------------------
Windows single-instance guard for the GUI.

Two copies of the app running at once share the same SQLite cache (db/cache.db),
the per-run resume caches and the log/output folders — a recipe for double file
access, DB contention and confusing double runs. This enforces ONE instance with
a named mutex (`CreateMutexW`): the kernel releases it automatically when the
process dies, so there's no stale-lock problem the way a PID file has; it needs no
dependency (ctypes only) and works under pythonw (no console). A duplicate launch
brings the existing window to the front and shows a native message box.

Fail-safe by design: any unexpected error (or a non-Windows platform) returns
"go ahead" rather than blocking the app over the guard itself.
"""

import sys
import ctypes

# Per-user-session scope ("Local\\"), not system-wide ("Global\\") — one instance
# per logged-in session is what we want (separate RDP sessions stay independent).
_MUTEX_NAME = "Local\\ImageToolbox_SingleInstance_Mutex"
_ERROR_ALREADY_EXISTS = 183

# Held for the whole process lifetime; the mutex is released when the handle is
# closed at process exit. Module-level so it is never garbage-collected early.
_mutex_handle = None


def acquire(window_title=None, app_title="Image Toolbox"):
    """Return True if this is the only running instance (caller proceeds), or
    False if another instance already holds the lock (caller should exit).

    On a duplicate, brings the existing window (`window_title`, exact match) to the
    front and shows a native "already running" message box. Returns True on any
    error or off Windows — the guard must never be the reason the app won't start.
    """
    global _mutex_handle
    if not sys.platform.startswith("win"):
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        already = kernel32.GetLastError() == _ERROR_ALREADY_EXISTS
        if handle and not already:
            _mutex_handle = handle          # we own it — keep it alive
            return True
        # Another instance owns the mutex.
        if window_title:
            _focus_existing(window_title)
        _notify(app_title)
        return False
    except Exception:                       # noqa: BLE001 (never block startup)
        return True


def _focus_existing(title):
    """Best-effort: un-minimise and foreground the already-running window."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _notify(app_title):
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"{app_title} is already running.\n\n"
            "Only one copy can run at a time — switching to the existing window.",
            app_title,
            0x0 | 0x40,                     # MB_OK | MB_ICONINFORMATION
        )
    except Exception:
        pass
