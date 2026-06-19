"""
crash_logger.py
---------------
Last-resort crash diagnostics for the Image Toolbox GUI.

The app launches under ``pythonw.exe`` (no console window), so any unhandled
exception — including an import-time failure such as a module the installer
forgot to ship — kills the process with nothing left on screen but a
split-second flash and no diagnostic trail. This module closes that gap.

On an unhandled crash it, best-effort and fail-safe:

  * writes a crash log to ``logs/crash_<timestamp>.log`` (same folder/naming as
    the existing run logs) — the artifact a user can attach to a bug report;
  * shows a native message box (ctypes ``user32``, so it works even when tkinter
    itself is the thing that broke) pointing at the log file.

(A Windows Event Viewer entry was considered but dropped: writing to the
Application log requires the event source to be registered by an elevated
process, and the app runs non-elevated — so it would silently no-op for normal
users. The crash log file is the reliable artifact instead.)

It hooks three places exceptions can escape: ``sys.excepthook`` (main thread),
``threading.excepthook`` (background threads — telemetry, MQTT, stdin readers),
and ``tkinter.Tk.report_callback_exception`` (Tk callbacks, which otherwise
print to the now-absent stderr). Stdlib only, to keep the app dependency-light.

Arm it as early as possible — before the feature imports — so import-time
failures are caught too::

    import crash_logger
    crash_logger.install()
    ...
    crash_logger.set_version(APP_VERSION)   # once it is known
"""

import os
import sys
import platform
import traceback
import datetime

APP_NAME = "Image Toolbox"

_version = "unknown"
_installed = False
# Whether a fatal crash also pops a native message box. True for the GUI (so the
# crash is visible under pythonw); False for the headless subprocess runners,
# whose traceback already reaches the GUI log pane via stderr.
_notify = True
# Guard against a crash *inside* the handler (e.g. while writing the log) from
# re-entering and masking the original traceback.
_handling = False


def set_version(version):
    """Record the app version so it appears in crash reports."""
    global _version
    if version:
        _version = str(version)


def _logs_dir():
    # The log lives next to the app's other logs, beside this module.
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def _format_report(exc_type, exc, tb, thread_name=None):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"{APP_NAME} crash report",
        "=" * 60,
        f"Time         : {now}",
        f"App version  : {_version}",
        f"Python       : {sys.version.split()[0]} ({platform.architecture()[0]})",
        f"Executable   : {sys.executable}",
        f"Platform     : {platform.platform()}",
        f"Command line : {' '.join(sys.argv)}",
    ]
    if thread_name:
        lines.append(f"Thread       : {thread_name}")
    lines.append("=" * 60)
    lines.append("")
    lines.append("".join(traceback.format_exception(exc_type, exc, tb)))
    return "\n".join(lines)


def _write_log(report):
    """Write the report to a timestamped file; return its path (or None)."""
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_logs_dir(), f"crash_{ts}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path
    except Exception:
        return None


def _show_message_box(log_path):
    """Native error dialog so the crash is visible even if tkinter is dead."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        where = log_path if log_path else "the logs folder"
        text = (
            f"{APP_NAME} closed unexpectedly due to an error.\n\n"
            f"A crash log was saved to:\n{where}\n\n"
            "Please attach that file when reporting the problem."
        )
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(0, text, f"{APP_NAME} — crash", 0x10 | 0x10000)
    except Exception:
        pass


def _report(exc_type, exc, tb, thread_name=None, notify=True):
    global _handling
    if _handling:
        return
    _handling = True
    try:
        report = _format_report(exc_type, exc, tb, thread_name)
        path = _write_log(report)
        if notify:
            _show_message_box(path)
    except Exception:
        pass
    finally:
        _handling = False


def _excepthook(exc_type, exc, tb):
    # Let Ctrl-C / normal interpreter exit behave normally.
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _report(exc_type, exc, tb, notify=_notify)
    # Preserve default behaviour too (prints to stderr if one exists).
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


def _thread_excepthook(args):
    if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
        return
    name = getattr(args.thread, "name", None)
    # Log background-thread crashes, but don't pop a dialog per worker hiccup;
    # the app may still be usable and we want to avoid a wall of message boxes.
    _report(args.exc_type, args.exc_value, args.exc_traceback,
            thread_name=name, notify=False)


def _install_tk_hook():
    """Route Tk callback exceptions through the same reporter."""
    try:
        import tkinter
    except Exception:
        return

    def report_callback_exception(self, exc_type, exc, tb):
        _report(exc_type, exc, tb, thread_name="tk-callback", notify=_notify)

    try:
        tkinter.Tk.report_callback_exception = report_callback_exception
    except Exception:
        pass


def install(notify=True):
    """Arm the crash handlers. Idempotent; safe to call early.

    ``notify`` controls the native message box on a fatal crash: leave it True
    for the GUI, pass False for headless subprocess runners.
    """
    global _installed, _notify
    _notify = notify
    if _installed:
        return
    _installed = True

    sys.excepthook = _excepthook

    try:
        import threading
        threading.excepthook = _thread_excepthook
    except Exception:
        pass

    _install_tk_hook()
