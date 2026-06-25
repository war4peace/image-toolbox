"""Author-benchmark lookup for the remote-pod cost estimator (0.3.9).

Answers one question for the GUI's "$ / 100 images" readout: given a task, a GPU
and a live hourly price, what did 100 images cost on that card in the author's
benchmark runs? The data source is ``docs/Benchmarks.csv`` (human-maintained);
the installer ships it to ``{app}/docs`` so it is present in installed copies too.

Pure standard library. Fail-safe by design: any missing file, parse error or
lookup miss returns None, which the GUI renders as "N/A".
"""

import csv
import os

# APP_ROOT = parent of scripts/, matching the rest of the app (paths anchored off
# __file__, never the cwd).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(APP_ROOT, "docs", "Benchmarks.csv")

# Task tags exactly as written in the CSV's "Task" column.
TASK_TAG              = "Tag & rename"
TASK_UPSCALE_RESIDENT = "Batch Upscaler (in-VRAM DiT+VAE)"   # big-VRAM cards
TASK_UPSCALE_OFFLOAD  = "Batch Upscaler (DiT in pod RAM)"    # smaller cards


def _normalize_gpu(name):
    """Lower-case and drop vendor noise so 'NVIDIA GeForce RTX 5090' and
    'RTX 5090' match, while keeping the model tokens that distinguish cards."""
    if not name:
        return ""
    s = name.lower()
    for junk in ("nvidia", "geforce"):
        s = s.replace(junk, " ")
    return " ".join(s.split())


def _gpu_matches(a, b):
    na, nb = _normalize_gpu(a), _normalize_gpu(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _parse_hms(text):
    """'HH:MM:SS' -> seconds; None for 'N/A', blank or anything unparseable."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def upscale_task(memory_gb, resident_threshold_gb=40):
    """Which upscale benchmark row applies to a card with `memory_gb` of VRAM: a
    big-VRAM card keeps DiT + VAE resident, a smaller one offloads DiT to pod RAM
    (the same 40 GB threshold the engine uses, see upscale_engine)."""
    try:
        big = float(memory_gb) >= float(resident_threshold_gb)
    except (TypeError, ValueError):
        big = False
    return TASK_UPSCALE_RESIDENT if big else TASK_UPSCALE_OFFLOAD


def seconds_per_100(task, gpu_name, csv_path=CSV_PATH):
    """Average wall-clock seconds per 100 images for `task` on `gpu_name`, across
    every CSV run that has a usable total runtime, or None if nothing matches."""
    try:
        # utf-8-sig: the CSV is saved with a BOM, which would otherwise glue
        # itself to the first column name ('﻿Task') and break the lookup.
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return None
    samples = []
    for row in rows:
        if (row.get("Task") or "").strip() != task:
            continue
        if not _gpu_matches(gpu_name, (row.get("GPU") or "").strip()):
            continue
        secs = _parse_hms(row.get("Total runtime"))
        if secs is None:
            continue
        try:
            count = int((row.get("Image Count") or "0").strip())
        except ValueError:
            continue
        if count <= 0:
            continue
        samples.append(secs / count * 100.0)
    if not samples:
        return None
    return sum(samples) / len(samples)


def cost_per_100(task, gpu_name, hourly_price, csv_path=CSV_PATH):
    """Estimated $ per 100 images = benchmark seconds-per-100 x live hourly rate.
    None when the GPU isn't benchmarked for this task, or the price is unknown."""
    if hourly_price is None:
        return None
    secs = seconds_per_100(task, gpu_name, csv_path=csv_path)
    if secs is None:
        return None
    return secs / 3600.0 * float(hourly_price)


def estimate_seconds_per_100(task, gpu_id, gpu_name, csv_path=CSV_PATH):
    """Best available seconds-per-100 estimate for the pre-run readout, with its
    source. Precedence: the user's own recorded runs on this exact GPU (once they
    have enough history, db.gpu_perf) -> the author's benchmark CSV -> nothing.

    Returns (seconds_per_100 | None, source) where source is
    'user' | 'author' | None. Reading the user history is best-effort: any DB
    issue silently falls through to the CSV."""
    if gpu_id:
        try:
            import db
            secs = db.get_gpu_perf(db.get_conn(), task, gpu_id)
            if secs is not None:
                return secs, "user"
        except Exception:                       # noqa: BLE001 (best-effort)
            pass
    secs = seconds_per_100(task, gpu_name, csv_path=csv_path)
    if secs is not None:
        return secs, "author"
    return None, None
