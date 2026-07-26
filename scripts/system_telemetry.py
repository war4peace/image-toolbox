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

# Columns queried from nvidia-smi, in order. Keep this list and pod/worker.py's
# copy (_sample_gpu) IDENTICAL — the app plots local and pod telemetry with one
# code path, so both sides must produce the same sample keys. Adding a column is
# a one-line change here: the parse is per-field, so a card that reports "[N/A]"
# for a field (common for power.limit / clocks / on laptops) yields None for just
# that field instead of poisoning the whole sample.
_GPU_QUERY_FIELDS = [
    ("gpu_used_mb",       "memory.used"),
    ("gpu_total_mb",      "memory.total"),
    ("gpu_temp_c",        "temperature.gpu"),
    ("gpu_util_pct",      "utilization.gpu"),
    ("gpu_power_w",       "power.draw"),
    ("gpu_power_limit_w", "power.limit"),
    ("gpu_clock_mhz",     "clocks.gr"),
]


def _parse_gpu_field(raw):
    """One nvidia-smi cell -> int, or None for '[N/A]' / blank / unparseable."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("[") or raw.lower() in ("n/a", "na"):
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def sample_gpu(timeout=5):
    """
    Query the first NVIDIA GPU via ``nvidia-smi``.

    Returns a dict with the keys in ``_GPU_QUERY_FIELDS`` (VRAM used/total MB, GPU
    temperature C, core utilisation %, power draw + limit W, core clock MHz), each
    an int or ``None`` where the card doesn't report it. Returns ``None`` outright
    only if ``nvidia-smi`` is missing / fails / prints nothing parseable.
    """
    query = "--query-gpu=" + ",".join(f for _, f in _GPU_QUERY_FIELDS)
    try:
        proc = subprocess.run(
            ["nvidia-smi", query, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 2:            # need at least VRAM used/total to be worth it
        return None
    result = {}
    for i, (key, _field) in enumerate(_GPU_QUERY_FIELDS):
        result[key] = _parse_gpu_field(parts[i]) if i < len(parts) else None
    return result


# ── telemetry history (in-app usage graphs, #9) ──────────────────────────────
# Per-RUN, in-memory buffers that feed the matplotlib graph window. GUI-free and
# stdlib-only so this unit-tests without a display (the window lives in
# gui/telemetry_graph.py). See docs/telemetry-design.md section 4.

# Range-button spans (seconds). A button is enabled only once the run's runtime
# has reached its span (docs 4.4).
HISTORY_SPANS = [("1h", 3600), ("3h", 10800), ("6h", 21600),
                 ("12h", 43200), ("24h", 86400)]

# A gap between adjacent samples wider than this many times the run's median
# sample interval breaks the plotted line (an honest gap, never an interpolation
# across a stall / a pod that briefly vanished).
HISTORY_GAP_FACTOR = 3.0


class TelemetryHistory:
    """One run's telemetry, in memory, for the graph window (#9).

    One instance per source ("local" or a pod). It records **only between
    ``start()`` and ``seal()``**: ``append`` outside that window is ignored, so
    the continuous idle sampler (which keeps the readout row live) never leaks
    into a graph. ``start()`` on an existing instance discards the previous run
    (reset-on-next-run). A sealed run is frozen but still fully queryable, so the
    window stays interactive for review. Nothing here persists: it dies with the
    process.
    """

    def __init__(self):
        self._start_ts = None
        self._sealed = False
        self._samples = []            # list of (ts, sample_dict)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, ts):
        """Open a fresh run at wall-clock ``ts`` (discards any previous run)."""
        self._start_ts = ts
        self._sealed = False
        self._samples = []

    def seal(self):
        """End the run: freeze the data (still queryable), stop accepting appends."""
        self._sealed = True

    def append(self, ts, sample):
        """Record one sample. No-op unless the run is live (started, not sealed)."""
        if self._start_ts is None or self._sealed:
            return
        if self._samples and ts < self._samples[-1][0]:
            ts = self._samples[-1][0]         # clamp the odd out-of-order sample
        self._samples.append((ts, dict(sample)))

    # ── queries ──────────────────────────────────────────────────────────────
    @property
    def is_live(self):
        return self._start_ts is not None and not self._sealed

    @property
    def is_sealed(self):
        return self._sealed

    @property
    def start_ts(self):
        return self._start_ts

    def __len__(self):
        return len(self._samples)

    def latest(self):
        """The most recent sample dict, or None if nothing recorded yet."""
        return self._samples[-1][1] if self._samples else None

    def anchor_ts(self):
        """The timeline origin for the graph: the FIRST recorded sample, not the
        moment the run was started. A batch's start includes a long
        eligibility/scan phase that does no GPU work and emits no samples, so
        anchoring to run-start would leave the graph blank for minutes; anchoring
        to the first sample makes the plot begin when the first image/video is
        actually processed. Falls back to start_ts before any sample exists."""
        if self._samples:
            return self._samples[0][0]
        return self._start_ts

    def runtime(self):
        """Elapsed run time in seconds (first sample to last), 0 if <2 samples.
        Measured from the first SAMPLE (see anchor_ts), so the range buttons
        enable on how much data exists, not on time spent pre-scanning."""
        if len(self._samples) < 2:
            return 0.0
        return max(0.0, self._samples[-1][0] - self._samples[0][0])

    def bounds(self):
        """(t0, t1) wall-clock span for the whole-run view: first sample to last
        (no leading dead space from the pre-scan phase)."""
        if not self._samples:
            base = self._start_ts or 0.0
            return base, base
        return self._samples[0][0], self._samples[-1][0]

    def enabled_spans(self):
        """Which range buttons are live now (runtime >= span), as (label, secs)."""
        rt = self.runtime()
        return [(label, s) for (label, s) in HISTORY_SPANS if rt >= s]

    def _median_interval(self):
        if len(self._samples) < 2:
            return None
        import statistics
        deltas = [self._samples[i][0] - self._samples[i - 1][0]
                  for i in range(1, len(self._samples))]
        deltas = [d for d in deltas if d > 0]
        return statistics.median(deltas) if deltas else None

    def series(self, accessor):
        """(times, values) float lists for plotting.

        ``accessor`` is a sample-field name or a ``callable(sample) -> number |
        None``. A None value, or a time gap wider than ``HISTORY_GAP_FACTOR`` x
        the run's median interval, inserts a ``NaN`` so matplotlib breaks the
        line there instead of drawing across the gap.
        """
        fn = accessor if callable(accessor) else (lambda s, k=accessor: s.get(k))
        med = self._median_interval()
        times, values = [], []
        prev_t = None
        for ts, s in self._samples:
            if prev_t is not None and med and (ts - prev_t) > HISTORY_GAP_FACTOR * med:
                times.append((prev_t + ts) / 2.0)   # a break between the two points
                values.append(float("nan"))
            v = fn(s)
            times.append(float(ts))
            values.append(float("nan") if v is None else float(v))
            prev_t = ts
        return times, values


def list_gpus(timeout=5):
    """
    Every local NVIDIA GPU as a list of ``{"index", "name", "memory_gb"}`` dicts,
    in nvidia-smi order (so ``index`` is the CUDA device number). Empty list if
    ``nvidia-smi`` is missing / fails / prints nothing parseable.

    ``sample_gpu`` deliberately reads only the FIRST card (the telemetry row shows
    one); this is the enumeration the GUI's "Run on -> Local GPU" picker needs.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    gpus = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        idx  = _parse_gpu_field(parts[0])
        name = parts[1]
        vram = _parse_gpu_field(parts[2])
        if idx is None or not name:
            continue
        gpus.append({"index": idx, "name": name,
                     "memory_gb": round((vram or 0) / 1024.0)})
    return gpus


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
