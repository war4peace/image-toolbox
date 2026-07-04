"""
debug_log.py
------------
A tiny, fail-safe diagnostic trail for the app's many `except Exception: pass`
handlers.

The "never crash, never touch originals" philosophy is right for this app, so
the fail-safe handlers stay. The only problem they had was silence: a swallowed
failure (a cache that never persists, a dead MQTT publish, a lineage row never
recorded, a pod that failed to tear down) left no trail, so the bug was
invisible until its downstream symptom appeared. This module gives those
handlers somewhere to whisper without ever changing the never-crash behaviour.

Usage from inside a fail-safe handler:

    from debug_log import debug_log
    try:
        ...
    except Exception as exc:
        debug_log(f"EligibilityCache.save: {exc}")          # one-liner
        # or, to capture the full traceback for a persistence/money path:
        debug_log("pod teardown failed", exc=exc, tb=True)

It appends a timestamped line to ``logs/debug.log`` (same folder as the run and
crash logs). It is itself fail-safe: any error inside it (unwritable disk, etc.)
is swallowed, so routing a handler through it can never reintroduce a crash. The
file is size-capped (rolls to ``debug.log.1`` past ``_MAX_BYTES``) so a long-
running install can't grow it without bound. Stdlib only, to keep the app
dependency-light. Works from the GUI and from every subprocess runner (each
process appends to the same file; a short source tag distinguishes them).
"""

import os
import sys
import datetime
import traceback

# Roll the log over past this size so it can't grow without bound across many
# runs. One previous generation is kept (debug.log.1); older lines are dropped.
_MAX_BYTES = 2 * 1024 * 1024

# Short tag identifying which process wrote a line (the GUI vs. one of the
# subprocess runners), derived once from the entry-point script name.
try:
    _SOURCE = os.path.splitext(os.path.basename(sys.argv[0]))[0] or "?"
except Exception:
    _SOURCE = "?"


def _logs_dir():
    # This module lives in scripts/; the logs/ folder is at the app root, one
    # level up (matches crash_logger._logs_dir).
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(app_root, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def _roll_if_big(path):
    """Best-effort size cap: once debug.log passes _MAX_BYTES, move it to
    debug.log.1 (replacing any previous one) so the active file starts fresh."""
    try:
        if os.path.getsize(path) < _MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass


def debug_log(msg, exc=None, tb=False):
    """Append one timestamped line to logs/debug.log. Never raises.

    ``exc``: an exception instance appended as ": <ClassName>: <exc>".
    ``tb``:  if True, append the current exception's traceback on following
             lines (call from inside the ``except`` block whose error you want).
    """
    try:
        text = str(msg)
        if exc is not None:
            text = f"{text}: {type(exc).__name__}: {exc}"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{_SOURCE}] {text}"
        if tb:
            trace = traceback.format_exc()
            # format_exc() returns "NoneType: None\n" when no exception is active.
            if trace and not trace.startswith("NoneType: None"):
                line = line + "\n" + trace.rstrip("\n")
        path = os.path.join(_logs_dir(), "debug.log")
        _roll_if_big(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # A diagnostic helper must never itself crash a fail-safe handler.
        pass
