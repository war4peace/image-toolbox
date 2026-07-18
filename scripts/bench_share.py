"""Benchmark sharing (future-features #8): serialise the per-card video benchmark
SUMMARY to a CSV users can contribute to a crowdsourced corpus, and read such a CSV
back.

The shared shape is the summary table the Benchmark GPU window shows: one row per
(target x torch.compile mode x tiling regime) for a card, NOT the raw per-batch
probes. The summary carries everything the sizer/estimator need (the ceiling batch,
the chosen batch, s/frame and peak VRAM) while staying human-readable and git-diffable
as text. See docs/future-features.md #8.

Distribution is zero-infrastructure: the curated master CSV lives in the GitHub repo
(pulled anonymously on the Download side), and a user contributes by exporting this CSV
and attaching it to a pre-filled GitHub issue (see gui.common.contribute_benchmark).
The maintainer curates submissions with `--merge` (dedupe + sanity-check into the
master), then commits the diffable CSV.

Pure standard library. Fail-safe by design: a malformed row is SKIPPED (never raised),
so one bad line can't poison an import; a write error is the caller's to surface.
"""

import csv
import os

# Bump when a column's MEANING changes. A pure append of a new column does NOT need a
# bump: the sentinel + the name-keyed reader tolerate unknown/extra and missing columns.
SCHEMA_VERSION = 1
SENTINEL = f"# imgtbx-bench v{SCHEMA_VERSION}"

# Column order IS the CSV header. Append-only across versions.
#   gpu_id       - the card key (nvidia-smi name local, RunPod id remote)
#   run_on       - local | remote: where the GPU ran (a clean pod vs a shared desktop card)
#   model        - model family tag (7b / 3b / 3b_fp16)
#   compile      - torch.compile ON | OFF (local signal; a pod owns its own compile)
#   tile         - VAE tiling regime ('off' | 'd1024' | 'e1024_d512' ...): a tiled ceiling
#                  is NOT interchangeable with an untiled one, so it must disambiguate a row
#   target       - human label (1080p / 4K / 1280x960); out_w/out_h are authoritative
#   out_w,out_h  - the real output dimensions (the box-fit key the sizer matches on)
#   max_batch    - the measured ceiling (largest 'ok' batch)
#   used_batch   - the throughput-optimal batch AUTO actually runs (<= ceiling)
#   overlap      - segment overlap for used_batch
#   spf          - seconds per frame at used_batch
#   peak_vram    - peak allocated VRAM (GB) at used_batch
#   free_vram    - FREE VRAM (GB) at probe time (contention context)
#   price_usd_hr - live RunPod $/h snapshot at benchmark time (blank for local); advisory
#   source       - local | imported: data provenance (drives import local-precedence)
#   date         - benchmark date (YYYY-MM-DD)
COLUMNS = [
    "gpu_id", "run_on", "model", "compile", "tile", "target",
    "out_w", "out_h", "max_batch", "used_batch", "overlap",
    "spf", "peak_vram", "free_vram", "price_usd_hr", "source", "date",
]

# Columns that must be present and non-blank for a row to be usable on import.
_REQUIRED = ("gpu_id", "model", "out_w", "out_h", "max_batch")


def _fmt(v):
    """CSV cell text for a value: '' for None, trimmed float, str otherwise."""
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def write_csv(path, rows):
    """Write `rows` (dicts keyed by COLUMNS; unknown keys ignored, missing blank) to
    `path`: the version sentinel first line, then the header, then the rows. Returns the
    number of rows written. Raises OSError only on a genuine file error (the caller
    surfaces it)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(SENTINEL + "\n")
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        n = 0
        for r in rows:
            w.writerow({c: _fmt(r.get(c)) for c in COLUMNS})
            n += 1
    return n


def to_text(rows):
    """The same CSV as `write_csv` but as a string, for inlining into a GitHub issue
    body. Never raises (pure in-memory)."""
    import io
    buf = io.StringIO()
    buf.write(SENTINEL + "\n")
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: _fmt(r.get(c)) for c in COLUMNS})
    return buf.getvalue()


def read_csv(path):
    """Parse a bench-share CSV into a list of dicts. Tolerant: the sentinel line and any
    other leading '#' comment lines are skipped; unknown columns are ignored and missing
    ones default blank; a row missing a required field is skipped. Returns [] on any file
    error (fail-safe), so a bad download/attachment can never raise into the caller."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return _parse(fh)
    except (OSError, csv.Error):
        return []


# The curated community master lives IN the repo and is served raw + anonymously (no auth, no
# API), the same GitHub host updater.py already fetches from. `main` is the default branch.
COMMUNITY_URL = ("https://raw.githubusercontent.com/war4peace/image-toolbox/"
                 "main/docs/video-benchmarks.csv")


def fetch_community(url=None, dest=None, timeout=20):
    """Download the curated community benchmark CSV (anonymous GET over the certifi trust
    context, the path updater.py uses) and return its parsed rows. When `dest` is given the
    raw text is also cached there, so a fresh install keeps the corpus offline. The download
    is UNTRUSTED: `_parse` already skips malformed rows, and this returns [] on ANY error
    (network down, 404, decode failure), never raising into the GUI."""
    import io
    import urllib.request
    try:
        from net_ssl import ssl_context
        ctx = ssl_context()
    except Exception:                                    # noqa: BLE001 (fail-safe: default TLS)
        ctx = None
    try:
        req = urllib.request.Request(url or COMMUNITY_URL,
                                     headers={"User-Agent": "ImageToolbox-BenchShare"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text = resp.read().decode("utf-8-sig", "replace")
    except Exception:                                    # noqa: BLE001 (fail-safe)
        return []
    if dest:
        try:
            with open(dest, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError:
            pass
    return _parse(io.StringIO(text))


def _parse(fh):
    # Drop the sentinel + any leading comment lines before the header, so csv.DictReader
    # sees the real header first.
    lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    rows = []
    for raw in csv.DictReader(lines):
        row = {c: (raw.get(c) or "").strip() for c in COLUMNS}
        if any(not row[c] for c in _REQUIRED):
            continue
        rows.append(row)
    return rows


# ── maintainer-side curation (`--merge`) ─────────────────────────────────────
# The maintainer accepts submissions (GitHub issues with an attached/inlined CSV),
# feeds the CSVs to `bench_share.py --merge`, and commits the regenerated master
# CSV. This side is NOT shipped to users: it accumulates into a private working
# SQLite DB (kept out of git) and re-exports the curated, diffable master. It runs
# a moderation gate (`sanity_check`) so one over-optimistic ceiling can't poison
# every user's estimator, and dedupes newest-wins by benchmark date.

# The cell identity: two rows describe the SAME measurement iff all of these match.
# Broader than the DB's folded bench_key because compile/tile/run_on are separate
# CSV columns here; each genuinely distinguishes a physical measurement.
_MERGE_KEY = ("gpu_id", "run_on", "model", "compile", "tile", "out_w", "out_h")

# Physical-plausibility bounds for the moderation gate (deliberately generous: the
# maintainer still eyeballs the git diff, so these only catch the clearly impossible).
_MAX_BATCH = 4096       # the sweep caps ~3000; a ceiling past this is a parse/units error
_MAX_DIM = 8192         # output edge (8K leaves headroom over the 4K target)
_MIN_SPF = 0.001        # < 1 ms/frame is not real SeedVR2 throughput
_MAX_SPF = 3600.0       # > 1 h/frame is impossible
_MAX_VRAM_GB = 400.0    # absolute ceiling (B200 = 192 GB) for an unknown card

# Known-card VRAM capacity (GB) by a distinctive substring of the card name, longest/most
# specific first (A100 80GB must beat plain A100). Only used to reject a peak_vram above the
# card's physical VRAM; an unknown card falls back to the absolute ceiling above. Substring
# match is case-insensitive.
_CARD_VRAM = [
    ("a100 80", 80), ("a100-80", 80), ("a100 sxm", 80), ("a100", 40),
    ("h200", 141), ("h100", 80), ("b200", 192),
    ("pro 6000", 96), ("rtx 6000 ada", 48), ("a6000", 48), ("rtx a6000", 48),
    ("l40", 48), ("a40", 48), ("l4", 24),
    ("5090", 32), ("4090", 24), ("3090", 24),
    ("a5000", 24), ("4500 ada", 24), ("4000 ada", 20), ("2000 ada", 16),
    ("a4000", 16),
]


def _num(v):
    """Coerce a CSV cell (text, possibly blank) to float, or None if not numeric."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _card_vram(gpu_id):
    """Best-effort VRAM capacity (GB) for a card name, or None if unrecognised."""
    name = (gpu_id or "").lower()
    for sub, gb in _CARD_VRAM:
        if sub in name:
            return gb
    return None


def sanity_check(row):
    """The maintainer's moderation gate. Return None if the row is physically plausible,
    else a short human reason it was rejected. Numeric cells arrive as CSV text; blanks in
    optional columns are tolerated (only the hard-impossible is rejected)."""
    ow, oh = _num(row.get("out_w")), _num(row.get("out_h"))
    if not ow or not oh or ow < 1 or oh < 1 or ow > _MAX_DIM or oh > _MAX_DIM:
        return f"implausible output size {row.get('out_w')}x{row.get('out_h')}"
    mb = _num(row.get("max_batch"))
    if mb is None or mb < 1 or mb > _MAX_BATCH or mb != int(mb):
        return f"implausible max_batch {row.get('max_batch')!r}"
    ub = _num(row.get("used_batch"))
    if ub is not None and (ub < 1 or ub > mb):
        return f"used_batch {row.get('used_batch')!r} out of range 1..{int(mb)}"
    spf = _num(row.get("spf"))
    if spf is not None and (spf < _MIN_SPF or spf > _MAX_SPF):
        return f"implausible spf {row.get('spf')!r}"
    pv = _num(row.get("peak_vram"))
    if pv is not None:
        if pv < 0 or pv > _MAX_VRAM_GB:
            return f"implausible peak_vram {row.get('peak_vram')!r}"
        cap = _card_vram(row.get("gpu_id"))
        if cap and pv > cap * 1.05:      # 5% slack for reporting/rounding noise
            return f"peak_vram {pv:g} GB exceeds {row.get('gpu_id')} capacity ~{cap} GB"
    ov = _num(row.get("overlap"))
    if ov is not None and ov < 0:
        return f"negative overlap {row.get('overlap')!r}"
    return None


def _open_db(path):
    """Open (creating if needed) the maintainer's private working accumulator DB. Every
    column is TEXT affinity so the CSV round-trips losslessly; the merge key is the primary
    key so an upsert dedupes cells."""
    import sqlite3
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{c} TEXT" for c in COLUMNS)
    pk = ", ".join(_MERGE_KEY)
    conn.execute(f"CREATE TABLE IF NOT EXISTS bench ({cols}, PRIMARY KEY ({pk}))")
    return conn


def _upsert(conn, row):
    """Insert/replace one cell with NEWEST-date-wins semantics (a re-benchmark on newer
    drivers supersedes; ties let the later submission win). Returns True if it wrote."""
    where = " AND ".join(f"{k}=?" for k in _MERGE_KEY)
    prev = conn.execute(f"SELECT date FROM bench WHERE {where}",
                        tuple(row.get(k, "") for k in _MERGE_KEY)).fetchone()
    if prev and (prev[0] or "") > (row.get("date") or ""):
        return False                     # keep the newer existing row (ISO dates sort lexically)
    placeholders = ", ".join("?" for _ in COLUMNS)
    conn.execute(f"INSERT OR REPLACE INTO bench ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                 tuple(row.get(c, "") for c in COLUMNS))
    return True


def _sort_key(r):
    def _i(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    return (r.get("gpu_id", ""), r.get("run_on", ""), r.get("model", ""),
            r.get("compile", ""), r.get("tile", ""), _i(r.get("out_w")), _i(r.get("out_h")))


def export_master(conn, master_path):
    """Regenerate the curated master CSV from the whole accumulator DB, deterministically
    sorted so a merge produces a clean, reviewable git diff. Returns the row count."""
    rows = [dict(zip(COLUMNS, rec))
            for rec in conn.execute(f"SELECT {', '.join(COLUMNS)} FROM bench")]
    rows.sort(key=_sort_key)
    return write_csv(master_path, rows)


def merge_files(submitted, master_path, db_path):
    """Ingest each submitted CSV into the working DB (sanity-checked, deduped newest-wins),
    then re-export the curated master. Returns a report dict for `_print_report`."""
    conn = _open_db(db_path)
    report = {"files": [], "accepted": 0, "rejected": 0}
    try:
        for path in submitted:
            rows = read_csv(path)
            acc = rej = 0
            reasons = []
            for row in rows:
                why = sanity_check(row)
                if why:
                    rej += 1
                    reasons.append((row.get("gpu_id", "?"), why))
                    continue
                if _upsert(conn, row):
                    acc += 1
            report["files"].append({"path": path, "rows": len(rows),
                                    "accepted": acc, "rejected": rej, "reasons": reasons})
            report["accepted"] += acc
            report["rejected"] += rej
        conn.commit()
        report["master_rows"] = export_master(conn, master_path)
    finally:
        conn.close()
    return report


def _print_report(report, master_path, db_path):
    for f in report["files"]:
        print(f"  {f['path']}: {f['accepted']} accepted, {f['rejected']} rejected "
              f"(of {f['rows']} parsed)")
        for gpu, why in f["reasons"]:
            print(f"      rejected [{gpu}]: {why}")
    print(f"\nTotal: {report['accepted']} accepted, {report['rejected']} rejected.")
    print(f"Working DB: {db_path}")
    print(f"Master CSV: {master_path} ({report['master_rows']} rows)")


def _expand(patterns):
    """Expand shell globs OURSELVES so the tool behaves identically on every shell:
    PowerShell (the app's primary shell) does NOT expand `*.csv` for a native command, so
    the pattern would otherwise reach us verbatim and silently match nothing. A literal
    existing path passes straight through; a pattern that matches nothing is reported.
    Deduped, order-preserving."""
    import glob
    out, seen, missed = [], set(), []
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if not hits:
            missed.append(pat)
            continue
        for h in hits:
            if h not in seen:
                seen.add(h)
                out.append(h)
    return out, missed


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Benchmark-sharing maintainer tool: merge submitted CSVs into the "
                    "curated master (dedupe + sanity-check). Not shipped to users.")
    ap.add_argument("--merge", nargs="+", metavar="CSV", required=True,
                    help="submitted benchmark CSV(s) to ingest (globs like submissions/*.csv "
                         "are expanded by the tool, so they work in PowerShell too)")
    ap.add_argument("--db", default="benchmarks.db",
                    help="private working SQLite accumulator (default: benchmarks.db, gitignored)")
    ap.add_argument("--master", default=os.path.join("docs", "video-benchmarks.csv"),
                    help="curated master CSV to (re)generate (default: docs/video-benchmarks.csv)")
    args = ap.parse_args(argv)
    paths, missed = _expand(args.merge)
    for pat in missed:
        print(f"  (no files match {pat!r})")
    if not paths:
        print("No input files found. Nothing to merge.")
        return 1
    report = merge_files(paths, args.master, args.db)
    _print_report(report, args.master, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
