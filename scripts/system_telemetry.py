"""
system_telemetry.py
--------------------
Lightweight, dependency-free system telemetry for the in-app status row and the
(optional) Home Assistant MQTT topics — Future Feature #3a.

Two read-only, best-effort sources, both Windows-friendly and stdlib-only so the
app stays dependency-light:

  * CPU utilisation — Windows ``GetSystemTimes`` via ``ctypes`` (no psutil).
    It is a *delta* measurement: each ``sample()`` reports the average CPU load
    over the interval since the previous call, which suits the slow (5–30 s)
    cadence the GUI samples at.
  * NVIDIA GPU VRAM + temperature — shelled out to ``nvidia-smi`` (already
    present on a CUDA box), with ``CREATE_NO_WINDOW`` so no console flashes.

Everything fails safe: a missing GPU, no ``nvidia-smi`` on PATH, or a non-Windows
host simply yields ``None`` for the affected fields. Calls block (the GPU query
spawns a process), so sample from a background thread — the GUI does.
"""

import sys
import ctypes
import subprocess
from ctypes import wintypes

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ── CPU (Windows GetSystemTimes) ─────────────────────────────────────────────

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class CpuSampler:
    """
    System-wide CPU usage from successive ``GetSystemTimes`` snapshots.

    Stateful: it remembers the previous reading so each ``sample()`` returns the
    busy-fraction over the elapsed interval. The first call has no baseline, so
    it primes with a brief internal sleep to still return a usable number.
    """

    def __init__(self):
        self._prev = None   # (idle, kernel, user) raw 100-ns counters

    def _read(self):
        if sys.platform != "win32":
            return None
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        try:
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        except Exception:
            return None
        if not ok:
            return None
        return (_filetime_to_int(idle),
                _filetime_to_int(kernel),
                _filetime_to_int(user))

    def _compute(self, prev, cur):
        d_idle   = cur[0] - prev[0]
        d_kernel = cur[1] - prev[1]   # kernel time on Windows *includes* idle
        d_user   = cur[2] - prev[2]
        total = d_kernel + d_user
        if total <= 0:
            return None
        busy = total - d_idle
        return max(0.0, min(100.0, busy * 100.0 / total))

    def sample(self, prime_seconds=0.5):
        """Return CPU usage as a percentage (0–100), or None if unavailable."""
        cur = self._read()
        if cur is None:
            return None
        if self._prev is None:
            # No baseline yet — take a short window so the first value is real.
            import time
            time.sleep(prime_seconds)
            prev = cur
            cur = self._read()
            if cur is None:
                self._prev = prev
                return None
        else:
            prev = self._prev
        self._prev = cur
        return self._compute(prev, cur)


# ── RAM (Windows GlobalMemoryStatusEx) ───────────────────────────────────────

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength",                ctypes.c_ulong),
        ("dwMemoryLoad",            ctypes.c_ulong),
        ("ullTotalPhys",            ctypes.c_ulonglong),
        ("ullAvailPhys",            ctypes.c_ulonglong),
        ("ullTotalPageFile",        ctypes.c_ulonglong),
        ("ullAvailPageFile",        ctypes.c_ulonglong),
        ("ullTotalVirtual",         ctypes.c_ulonglong),
        ("ullAvailVirtual",         ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def sample_ram():
    """Return physical RAM ``(used_mb, total_mb)`` as ints, or None if
    unavailable (non-Windows host or the call fails)."""
    if sys.platform != "win32":
        return None
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    except Exception:
        return None
    if not ok or stat.ullTotalPhys <= 0:
        return None
    used = stat.ullTotalPhys - stat.ullAvailPhys
    return used // (1024 * 1024), stat.ullTotalPhys // (1024 * 1024)


# ── GPU (nvidia-smi) ─────────────────────────────────────────────────────────

def sample_gpu(timeout=5):
    """
    Query the first NVIDIA GPU via ``nvidia-smi``.

    Returns ``(vram_used_mb, vram_total_mb, temp_c)`` as ints, or ``None`` if
    ``nvidia-smi`` is missing / fails / reports nothing parseable.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    line = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
    except ValueError:
        return None


def gpu_name(timeout=5):
    """
    The first NVIDIA GPU's model name via ``nvidia-smi`` (e.g. "NVIDIA GeForce
    RTX 4070"), or ``None`` if unavailable. Used to pre-fill bug reports; like
    the rest of this module it fails safe and never raises.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip().splitlines()[0].strip() or None
