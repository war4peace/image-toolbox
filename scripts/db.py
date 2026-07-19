"""
db.py
-----
Single SQLite cache database for the toolbox, at db/cache.db.

Replaces the per-folder JSON cache files that used to live in scans/ (the
upscale eligibility cache) and trcache/ (the tag & rename cache). One database,
two pairs of cache tables plus the content-hash lineage:

    upscale_roots / upscale_files   – eligibility cache (batch_upscale.py)
    tag_roots     / tag_files       – tag & rename cache (tag_and_rename.py)
    lineage                         – source→upscaled→tagged links by content
                                      hash (batch_upscale.py, tag_and_rename.py;
                                      read by conciliate.py)
    file_hashes                     – memoised file content hashes, shared

The lineage table is the relationship that lets conciliation re-match a source
photo to its processed counterpart by content even after the user moves or
renames folders (each stage rewrites the bytes, so H0/H1/H2 are mutually
underivable and the link must be recorded at processing time; the hash only
re-identifies a file after its path changed). See record_upscale_lineage /
record_tag_lineage / lineage_final_hash below.

Logs are intentionally NOT stored here — they stay as human-readable text files
in logs/.

On first creation the existing JSON caches are imported once (see
import_legacy_json): a cache is imported only if its source folder still exists
on disk; stale ones are skipped. There is no ongoing migration — once cache.db
exists, the JSON files are ignored.

The database is opened once per process (get_conn) in WAL mode. The toolbox runs
its tools as separate, GPU-serialised subprocesses, so writes do not overlap in
practice; WAL keeps concurrent readers safe regardless.
"""

import os
import json
import sqlite3
import hashlib
import datetime
import threading
import functools

# Fail-safe diagnostic trail for the swallowed-error handlers below. Guarded so
# an old install missing debug_log.py can't break the cache layer.
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# App root = parent of scripts/. The cache DB and the legacy scans/ & trcache/
# import folders live at the app root, not beside this module.
APP_ROOT   = os.path.dirname(SCRIPT_DIR)
DB_DIR     = os.path.join(APP_ROOT, "db")
DB_PATH    = os.path.join(DB_DIR, "cache.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS upscale_roots (
    id          INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL UNIQUE,
    output_root TEXT,
    saved_at    TEXT
);
CREATE TABLE IF NOT EXISTS upscale_files (
    root_id      INTEGER NOT NULL REFERENCES upscale_roots(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    mtime        REAL,
    size         INTEGER,
    eligible     INTEGER NOT NULL DEFAULT 1,
    already_done INTEGER NOT NULL DEFAULT 0,
    skip_reason  TEXT,
    PRIMARY KEY (root_id, rel_path)
);
CREATE TABLE IF NOT EXISTS tag_roots (
    id           INTEGER PRIMARY KEY,
    source_root  TEXT NOT NULL UNIQUE,
    created_at   TEXT,
    last_updated TEXT
);
CREATE TABLE IF NOT EXISTS tag_files (
    root_id           INTEGER NOT NULL REFERENCES tag_roots(id) ON DELETE CASCADE,
    original_rel_path TEXT NOT NULL,
    current_rel_path  TEXT,
    status            TEXT,
    entry_json        TEXT NOT NULL,
    PRIMARY KEY (root_id, original_rel_path)
);

-- Content-hash lineage: links a source photo to its upscaled output and, in
-- turn, to its tagged & renamed result. Each stage rewrites the bytes (the
-- upscaler is non-deterministic; tag&rename edits EXIF in place), so the three
-- hashes are unrelated and the chain MUST be recorded here as the files are
-- produced. The hashes then re-identify each file by content even after the
-- user moves or renames folders, which is what conciliation relies on.
CREATE TABLE IF NOT EXISTS lineage (
    id            INTEGER PRIMARY KEY,
    src_hash      TEXT,   -- H0: hash of the source photo
    upscaled_hash TEXT,   -- H1: hash of the upscaled output
    tagged_hash   TEXT,   -- H2: hash of the tagged & renamed result (NULL if untagged)
    src_path      TEXT,   -- last known absolute paths (informational only)
    upscaled_path TEXT,
    tagged_path   TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_lineage_src      ON lineage(src_hash);
CREATE INDEX IF NOT EXISTS idx_lineage_upscaled ON lineage(upscaled_hash);
CREATE INDEX IF NOT EXISTS idx_lineage_tagged   ON lineage(tagged_hash);

-- Memoised file content hashes, shared by every tool. Keyed by absolute path
-- and validated by (mtime, size) so an unchanged file is hashed only once.
CREATE TABLE IF NOT EXISTS file_hashes (
    path  TEXT PRIMARY KEY,
    mtime REAL,
    size  INTEGER,
    hash  TEXT
);

-- Video Upscaler (#2) — normalised into three tables (docs/video-upscaler.md
-- section 15.6): source PROPERTIES once (video_files), per-(source,target) JOB +
-- queue state (video_outputs), and per-(source,target,segment) resume state
-- (video_segments). The QUEUE is exactly the video_outputs rows not yet 'done',
-- ordered by queue_order, so the queue survives app restarts and a stopped run
-- resumes at the first unfinished SEGMENT (the installment model, section 5).
-- No GPU/torch here: the runner is pure orchestration + local ffmpeg.
CREATE TABLE IF NOT EXISTS video_roots (
    id          INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL UNIQUE,
    output_root TEXT,
    saved_at    TEXT
);
-- Source properties (one row per source video). The cheap fast-scan fields
-- (width/height/vcodec/acodec/fps/duration) are filled on folder scan and
-- validated by (mtime, size) so an unchanged re-scan is free; nb_frames (COUNTED,
-- the header lies — see 14) and src_hash (content lineage) are filled lazily at
-- Prepare/upscale time, only for files actually acted on (section 15.4).
CREATE TABLE IF NOT EXISTS video_files (
    root_id    INTEGER NOT NULL REFERENCES video_roots(id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,
    width      INTEGER,
    height     INTEGER,
    vcodec     TEXT,
    acodec     TEXT,
    fps        REAL,
    nb_frames  INTEGER,           -- COUNTED frames, filled at Prepare (NULL = not yet)
    duration   REAL,
    mtime      REAL,              -- (mtime, size) validate the fast-scan cache
    size       INTEGER,
    src_hash   TEXT,              -- content hash, filled lazily at upscale time
    probe_version INTEGER,        -- bumped when probe() semantics change (e.g. rotation
                                  -- -> display dims); a mismatch forces a re-probe
    updated_at TEXT,
    PRIMARY KEY (root_id, rel_path)
);
-- Per-(source, target, clip) job. THIS is the durable queue + resume state: the
-- queue is the rows with status not 'done', ordered by queue_order.
-- clip_id (section 16.6) is the segment-extractor discriminator: 0 = the WHOLE
-- file (all pre-clip behaviour), a positive id = a virtual time-range clip of the
-- source (clip_start/clip_end seconds, optional label, clip_frames counted at
-- Prepare). A source can carry several clips at one target, which is why clip_id
-- is part of the primary key.
CREATE TABLE IF NOT EXISTS video_outputs (
    root_id     INTEGER NOT NULL REFERENCES video_roots(id) ON DELETE CASCADE,
    rel_path    TEXT NOT NULL,
    target      TEXT NOT NULL,    -- 1080p | 1440p | 4K
    clip_id     INTEGER NOT NULL DEFAULT 0,  -- 0 = whole file; >0 = a virtual clip
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|splitting|streaming|partial|done|failed|skipped
    skip_reason TEXT,
    fail_count  INTEGER NOT NULL DEFAULT 0,  -- consecutive failed attempts; give-up drops the job (item 4)
    output_path TEXT,
    out_frames  INTEGER,          -- frames in the reassembled output (drift check)
    queue_order INTEGER,          -- user-orderable position in the queue
    clip_start  REAL,             -- clip range start (seconds); NULL for a whole-file job
    clip_end    REAL,             -- clip range end (seconds); NULL for a whole-file job
    clip_label  TEXT,             -- optional user label, used in the output filename
    clip_frames INTEGER,          -- COUNTED frames of the extracted range (filled at Prepare)
    created_at  TEXT,
    updated_at  TEXT,
    PRIMARY KEY (root_id, rel_path, target, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_video_outputs_queue
    ON video_outputs(root_id, queue_order);
CREATE TABLE IF NOT EXISTS video_segments (
    root_id    INTEGER NOT NULL,
    rel_path   TEXT NOT NULL,
    target     TEXT NOT NULL,
    clip_id    INTEGER NOT NULL DEFAULT 0,  -- matches the parent video_outputs job
    seg_index  INTEGER NOT NULL,
    in_frames  INTEGER,           -- source frames in this segment
    out_frames INTEGER,           -- upscaled frames returned by the worker
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed
    seconds    REAL,              -- worker process time for this segment
    output_path TEXT,             -- the upscaled segment file (kept for reassembly)
    updated_at TEXT,
    PRIMARY KEY (root_id, rel_path, target, clip_id, seg_index),
    FOREIGN KEY (root_id) REFERENCES video_roots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_seg_parent
    ON video_segments(root_id, rel_path, target, clip_id);

-- Per-user GPU timing for the remote-pod cost estimator (0.3.9). Cumulative
-- images + processing seconds per (task, gpu_id), accumulated from finished
-- remote runs, so a future run on the same card warm-starts its "$ / 100 images"
-- estimate from the user's OWN history instead of "N/A". TIME only, never cost
-- (cost is derived as time x the live hourly rate, so it stays correct as RunPod
-- prices move). `task` matches the benchmark labels (Tag & rename / the two
-- upscale variants); `gpu_id` is the exact RunPod GPU id.
CREATE TABLE IF NOT EXISTS gpu_perf (
    task    TEXT NOT NULL,
    gpu_id  TEXT NOT NULL,
    runs    INTEGER NOT NULL DEFAULT 0,
    images  INTEGER NOT NULL DEFAULT 0,
    seconds REAL    NOT NULL DEFAULT 0,
    updated TEXT,
    PRIMARY KEY (task, gpu_id)
);
CREATE TABLE IF NOT EXISTS video_batch_learn (
    gpu_id    TEXT    NOT NULL,       -- IMGTBX_GPU_OVERRIDE / model-qualified card key
    mp_key    INTEGER NOT NULL,       -- per-frame OUTPUT megapixels / MP_KEY_UNIT (fine grid,
                                      -- feature #7: fine enough that distinct real output sizes
                                      -- get distinct rows; the sizer reads NEAREST key >= run MP)
    batch     INTEGER NOT NULL,       -- converged safe 4n+1 batch for this card+output size
    updated_at TEXT,
    PRIMARY KEY (gpu_id, mp_key)
);
-- Per-card VRAM benchmark suite (feature #7, docs/local-video-upscaler.md 16/20).
-- One row per PROBE: a fixed 4n+1 batch tried at a given OUTPUT size on this card+model,
-- run in an isolated subprocess (test-till-it-breaks upward sweep). Persisted per probe so
-- a stopped benchmark RESUMES at the nearest untried batch, and so a report survives. The
-- final ceiling (max 'ok' batch per cell) is copied into video_batch_learn + gpu_perf, but
-- these raw rows stay as the audit trail / resume state.
CREATE TABLE IF NOT EXISTS video_bench (
    gpu_id        TEXT    NOT NULL,   -- nvidia-smi card name (the sizer/estimate key)
    model         TEXT    NOT NULL,   -- model family tag (7b / 3b / 3b_fp16)
    out_w         INTEGER NOT NULL,   -- benchmarked OUTPUT width
    out_h         INTEGER NOT NULL,   -- benchmarked OUTPUT height
    batch         INTEGER NOT NULL,   -- 4n+1 batch probed
    outcome       TEXT    NOT NULL,   -- 'ok' | 'oom' | 'thrash' | 'error'
    frames        INTEGER,            -- frames processed (outcome 'ok')
    seconds       REAL,               -- wall seconds for the probe (outcome 'ok')
    peak_alloc    REAL,               -- peak allocated VRAM, GB
    peak_reserved REAL,               -- peak reserved VRAM, GB
    free_vram     REAL,               -- FREE VRAM (GB) at probe start: distinguishes a
                                      -- contended failure (other apps holding VRAM) from a
                                      -- true ceiling, so a later clean run supersedes it
    updated_at    TEXT,
    PRIMARY KEY (gpu_id, model, out_w, out_h, batch)
);
"""

# The video_batch_learn key grid (per-frame OUTPUT megapixels -> integer key). FINE (0.05 MP)
# so distinct real output sizes -- e.g. a portrait 810x1080 (0.87 MP) vs a landscape 1280x960
# (1.23 MP) box-fit output -- get their OWN learned-batch row instead of colliding in one coarse
# bucket. The sizer looks up the nearest recorded key >= the run's MP (a ceiling measured at a
# LARGER output is a safe bound for a smaller one), so a fine grid costs no safety. Must match
# video_vram_sizer._MP_KEY_MP and batch_video_upscale._MP_KEY_MP.
MP_KEY_UNIT = 0.05

_conn = None   # one connection per process

# get_conn hands out ONE shared connection (check_same_thread=False) and the GUI
# touches it from short-lived scan/prepare worker threads (Video Upscaler tab).
# Nothing enforced that only one op runs at a time, so two helpers on different
# threads could interleave statements inside each other's implicit transaction on
# that single connection (a dirty read, or one thread's commit landing another
# thread's half-written rows). This reentrant lock serialises every DB helper so
# each read-modify-write runs atomically. Reentrant so a guarded helper may call
# another; every helper is short, so contention is negligible. It also guards the
# lazy connection init below so two threads can't both build the connection.
_LOCK = threading.RLock()


def _locked(fn):
    """Run a DB helper while holding _LOCK (see its comment)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


def get_conn():
    """Open (once per process) and return the shared cache.db connection."""
    if _conn is not None:
        return _conn
    with _LOCK:
        if _conn is not None:       # another thread built it while we waited
            return _conn
        return _open_conn()


def _open_conn():
    """Build, initialise and cache the shared connection. Caller holds _LOCK."""
    global _conn
    os.makedirs(DB_DIR, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    # check_same_thread=False: the GUI (Video Upscaler tab) touches this one shared
    # connection from short-lived worker threads (scan / prepare run off the UI
    # thread). Cross-thread use of the single connection is now made safe by _LOCK
    # (every helper is @_locked, so their read-modify-writes can't interleave); WAL
    # keeps the on-disk file safe for the separate subprocess runners too. This
    # avoids a per-thread connection (which the connection-caching callers, e.g.
    # EligibilityCache, would defeat anyway by holding one connection across threads).
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_video_tables(conn)
    _migrate_video_clip_columns(conn)
    _migrate_video_batch_learn(conn)
    conn.executescript(SCHEMA)
    _ensure_video_columns(conn)
    conn.commit()
    if fresh:
        try:
            import_legacy_json(conn)
        except Exception as exc:
            print(f"  [db] Legacy cache import skipped due to error: {exc}")
    _conn = conn
    return conn


def _migrate_video_tables(conn):
    """Phase 5 normalised the video_* cache into three tables (15.6). If a DB still
    carries the phase-4 shape (video_files with a 'target' column, no video_outputs),
    drop the three video tables so the new SCHEMA recreates them. No real data is
    lost: these are a regenerable resume cache, and only the experimental branch
    ever wrote them. Runs once — after the drop, video_files has no 'target' column
    so this is a no-op. Fail-safe."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_files)")]
        if cols and "target" in cols:
            conn.executescript(
                "DROP TABLE IF EXISTS video_segments;"
                "DROP TABLE IF EXISTS video_outputs;"
                "DROP TABLE IF EXISTS video_files;")
            conn.commit()
    except Exception as exc:
        debug_log("db._migrate_video_tables", exc=exc)


def _migrate_video_batch_learn(conn):
    """The learned-batch cache was re-keyed from a coarse 0.5-MP bucket (`mp_bucket` column) to
    a fine MP_KEY_UNIT grid (`mp_key`) for sizing precision (feature #7). The two can't be told
    apart by value, so if a DB still carries the old `mp_bucket` column, DROP the table and let
    SCHEMA recreate it with `mp_key`. No real data lost: video_batch_learn is a regenerable
    cache derived from video_bench + live runs (the raw ceilings survive in video_bench, keyed by
    exact output dims). Runs once (after the drop, the column is `mp_key`, so it's a no-op).
    Fail-safe."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_batch_learn)")]
        if cols and "mp_bucket" in cols:
            conn.executescript("DROP TABLE IF EXISTS video_batch_learn;")
            conn.commit()
    except Exception as exc:
        debug_log("db._migrate_video_batch_learn", exc=exc)


def _migrate_video_clip_columns(conn):
    """Add the segment-extractor `clip_id` discriminator (section 16.6) to
    video_outputs / video_segments. clip_id joins BOTH primary keys, and SQLite
    can't add a PK column in place, so rebuild the two tables when an existing DB
    still has the pre-clip shape. Data is preserved (every existing job/segment is
    a whole-file row, copied with clip_id = 0), so an in-flight queue survives.

    Runs BEFORE executescript(SCHEMA), so on a fresh DB (no video_outputs yet) it's
    a no-op and SCHEMA creates the new shape directly; on an already-migrated DB the
    clip_id column is present and it's a no-op too. Fail-safe (the cache is
    regenerable, so a rebuild failure is logged, not fatal)."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_outputs)")]
        if not cols or "clip_id" in cols:
            return                          # no table yet, or already migrated
        # Rename the old tables aside, drop their indexes (the names are reused by
        # SCHEMA below, and IF NOT EXISTS would otherwise skip the new indexes),
        # let SCHEMA recreate both tables + indexes in the new shape, copy every
        # row as a whole-file (clip_id = 0) job, then drop the old tables.
        conn.executescript(
            "ALTER TABLE video_outputs RENAME TO _vo_old;"
            "ALTER TABLE video_segments RENAME TO _vs_old;"
            "DROP INDEX IF EXISTS idx_video_outputs_queue;"
            "DROP INDEX IF EXISTS idx_video_seg_parent;")
        conn.executescript(SCHEMA)          # fresh new-shape tables + indexes
        conn.execute(
            "INSERT INTO video_outputs (root_id, rel_path, target, clip_id, status, "
            "skip_reason, output_path, out_frames, queue_order, created_at, updated_at) "
            "SELECT root_id, rel_path, target, 0, status, skip_reason, output_path, "
            "out_frames, queue_order, created_at, updated_at FROM _vo_old")
        conn.execute(
            "INSERT INTO video_segments (root_id, rel_path, target, clip_id, seg_index, "
            "in_frames, out_frames, status, seconds, output_path, updated_at) "
            "SELECT root_id, rel_path, target, 0, seg_index, in_frames, out_frames, "
            "status, seconds, output_path, updated_at FROM _vs_old")
        conn.executescript("DROP TABLE _vo_old; DROP TABLE _vs_old;")
        conn.commit()
    except Exception as exc:
        debug_log("db._migrate_video_clip_columns", exc=exc)


def _ensure_video_columns(conn):
    """Add columns introduced after a DB already had the video_* tables (CREATE IF NOT
    EXISTS won't alter an existing table). Fail-safe; runs every open, cheap."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_files)")]
        if cols and "probe_version" not in cols:
            conn.execute("ALTER TABLE video_files ADD COLUMN probe_version INTEGER")
    except Exception as exc:
        debug_log("db._ensure_video_columns(video_files)", exc=exc)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_outputs)")]
        if cols and "fail_count" not in cols:      # item 4: give up on a job that keeps failing
            conn.execute("ALTER TABLE video_outputs "
                         "ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
    except Exception as exc:
        debug_log("db._ensure_video_columns(video_outputs)", exc=exc)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(video_bench)")]
        if cols and "free_vram" not in cols:       # #7: free-VRAM tag for contended probes
            conn.execute("ALTER TABLE video_bench ADD COLUMN free_vram REAL")
        # Split peaks: peak_alloc/peak_reserved stay the WORST CASE over the probe (what the
        # ceiling is made of); these are the STEADY-STATE pass alone. A warmed probe fuses two
        # very different costs into one number -- the cold pass pays torch.compile, whose
        # inductor autotuning allocates at the compiled shape and varies run to run, while the
        # warm pass is what every segment after the first actually costs. Fused, we could not
        # tell a compile-bound ceiling from an inference-bound one, and the measured peak
        # stopped tracking output megapixels (0.69 MP -> 21.4 GB but 1.23 MP -> 18.4 GB).
        # NULL on an un-warmed probe / an older row: there was only ever one pass.
        for _c in ("peak_alloc_steady", "peak_reserved_steady"):
            if cols and _c not in cols:
                conn.execute(f"ALTER TABLE video_bench ADD COLUMN {_c} REAL")
        # #8 benchmark sharing: data provenance drives import local-precedence. Existing rows
        # were all measured on THIS machine, so 'local' is the correct default (an import can
        # never clobber them); a downloaded row is tagged 'imported' by import_bench_rows.
        if cols and "source" not in cols:
            conn.execute("ALTER TABLE video_bench "
                         "ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
    except Exception as exc:
        debug_log("db._ensure_video_columns(video_bench)", exc=exc)


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


# ─────────────────────────────────────────────
#  GPU PERFORMANCE (remote cost estimator)
# ─────────────────────────────────────────────

@_locked
def record_gpu_perf(conn, task, gpu_id, images, seconds, min_images=20):
    """Accumulate a finished remote run's GPU timing for (task, gpu_id). Stores
    cumulative images + processing seconds (a lifetime average); a run shorter
    than `min_images` is ignored as too noisy to trust. TIME only — never cost."""
    if not task or not gpu_id:
        return
    try:
        images  = int(images)
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    if images < min_images or seconds <= 0:
        return
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO gpu_perf (task, gpu_id, runs, images, seconds, updated) "
        "VALUES (?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(task, gpu_id) DO UPDATE SET "
        "runs = runs + 1, images = images + excluded.images, "
        "seconds = seconds + excluded.seconds, updated = excluded.updated",
        (task, gpu_id, images, seconds, now))
    conn.commit()


@_locked
def get_gpu_perf(conn, task, gpu_id, min_images=100):
    """The user's own seconds-per-100-images for (task, gpu_id), or None until
    there is enough recorded history (< `min_images` cumulative). Used to
    supersede the author benchmark once the user has run this GPU enough."""
    if not task or not gpu_id:
        return None
    row = conn.execute(
        "SELECT images, seconds FROM gpu_perf WHERE task = ? AND gpu_id = ?",
        (task, gpu_id)).fetchone()
    if not row:
        return None
    images, seconds = row["images"], row["seconds"]
    if not images or images < min_images or not seconds or seconds <= 0:
        return None
    return seconds / images * 100.0


@_locked
def get_video_mp_rate_any(conn, gpu_id, min_images=300):
    """Seconds-per-100-output-MP for this card pooled ACROSS EVERY video target (all
    `video-mp-<target>` rows summed). A cross-target fallback for the time estimate / live
    progress bar when the EXACT target has no history yet (e.g. the first 4x run after the card
    has only done 2x): SeedVR2 cost is ~per-output-MP, so a card's blended video-MP rate is a
    rough but honest estimate from the user's OWN data. None until there is enough pooled
    history. gpu_id namespaces are disjoint (local card name vs remote GPU id), so this never
    mixes a local rate into a remote estimate."""
    if not gpu_id:
        return None
    row = conn.execute(
        "SELECT SUM(images) AS images, SUM(seconds) AS seconds FROM gpu_perf "
        "WHERE gpu_id = ? AND task LIKE 'video-mp-%'", (gpu_id,)).fetchone()
    if not row:
        return None
    images, seconds = row["images"], row["seconds"]
    if not images or images < min_images or not seconds or seconds <= 0:
        return None
    return seconds / images * 100.0


# ─────────────────────────────────────────────
#  VIDEO BATCH LEARNING (auto-tune seed, item 9)
# ─────────────────────────────────────────────

def _row_fresh(row, max_age_days):
    """True if a video_batch_learn row is present and not stale (> max_age_days old)."""
    if not row or not row["batch"]:
        return False
    if max_age_days and row["updated_at"]:
        try:
            age = datetime.datetime.now() - datetime.datetime.strptime(
                row["updated_at"], "%Y-%m-%dT%H:%M:%S")
            if age.days > max_age_days:
                return False
        except (ValueError, TypeError):
            pass
    return True


@_locked
def get_learned_batch(conn, gpu_id, mp_key, max_age_days=90):
    """The converged safe batch learned for EXACTLY (gpu_id, output-MP key), or None when there
    is no entry or it is stale (> max_age_days old: drivers + worker code change, so an old value
    may no longer be optimal). Exact-key match (round-trip with put_learned_batch); the sizer's
    read is get_learned_batch_ge, which generalises to nearby sizes."""
    if not gpu_id or mp_key is None:
        return None
    row = conn.execute(
        "SELECT batch, updated_at FROM video_batch_learn WHERE gpu_id = ? AND mp_key = ?",
        (gpu_id, int(mp_key))).fetchone()
    return int(row["batch"]) if _row_fresh(row, max_age_days) else None


@_locked
def get_learned_batch_ge(conn, gpu_id, mp_key, max_age_days=90):
    """The learned safe batch for the SMALLEST recorded output-MP key >= `mp_key`, or None when
    nothing at-or-above is recorded (caller then uses the seed) / the nearest matches are stale.
    A ceiling measured at an output AT LEAST this large is a SAFE bound here: the VAE-decode
    ceiling falls monotonically as output-MP rises, so a batch that fit a bigger output also fits
    a smaller one. This is the sizer's lookup -- it lets each benchmarked size serve nearby
    smaller runs conservatively, so the fine key grid never needs an exact hit to be useful."""
    if not gpu_id or mp_key is None:
        return None
    rows = conn.execute(
        "SELECT batch, updated_at FROM video_batch_learn "
        "WHERE gpu_id = ? AND mp_key >= ? ORDER BY mp_key ASC",
        (gpu_id, int(mp_key))).fetchall()
    for row in rows:
        if _row_fresh(row, max_age_days):
            return int(row["batch"])
    return None


@_locked
def put_learned_batch(conn, gpu_id, mp_key, batch, updated_at=None):
    """Record the converged safe batch for (gpu_id, output-MP key). Overwrites any prior value
    (the newest measurement wins, so a driver/code change self-corrects). `updated_at` lets an
    IMPORT stamp the shared benchmark's own date (so staleness stays honest) instead of now.
    Commits."""
    if not gpu_id or mp_key is None or not batch or batch <= 0:
        return
    now = updated_at or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO video_batch_learn (gpu_id, mp_key, batch, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(gpu_id, mp_key) DO UPDATE SET "
        "batch = excluded.batch, updated_at = excluded.updated_at",
        (gpu_id, int(mp_key), int(batch), now))
    conn.commit()


# ─────────────────────────────────────────────
#  VIDEO BENCHMARK SUITE (per-card, feature #7 / docs 16 + 20)
# ─────────────────────────────────────────────

@_locked
def record_bench_probe(conn, gpu_id, model, out_w, out_h, batch, outcome,
                       frames=None, seconds=None, peak_alloc=None, peak_reserved=None,
                       free_vram=None, peak_alloc_steady=None, peak_reserved_steady=None):
    """Persist ONE benchmark probe (a fixed batch at an output size on this card+model).
    Upserts, so re-running a probe overwrites the prior outcome (the newest run wins).
    `free_vram` (GB free at probe start) tags a probe run under other-app VRAM contention,
    so resume can re-try a failure that happened with less headroom than is free now.

    `peak_alloc`/`peak_reserved` are the WORST CASE over the whole probe (the ceiling: a real
    run's first segment pays the cold+compile peak too). `peak_*_steady` are the warm pass
    alone -- what every later segment costs. Keeping both is what distinguishes a
    compile-bound ceiling from an inference-bound one; None when the probe ran a single pass.
    Best-effort: bad args are ignored. Commits so a crash/kill can't lose finished probes."""
    if not gpu_id or not model or not out_w or not out_h or not batch or not outcome:
        return
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    def _f(x):
        return float(x) if x is not None else None
    conn.execute(
        "INSERT INTO video_bench (gpu_id, model, out_w, out_h, batch, outcome, frames, "
        "seconds, peak_alloc, peak_reserved, free_vram, peak_alloc_steady, "
        "peak_reserved_steady, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(gpu_id, model, out_w, out_h, batch) DO UPDATE SET "
        "outcome=excluded.outcome, frames=excluded.frames, seconds=excluded.seconds, "
        "peak_alloc=excluded.peak_alloc, peak_reserved=excluded.peak_reserved, "
        "free_vram=excluded.free_vram, peak_alloc_steady=excluded.peak_alloc_steady, "
        "peak_reserved_steady=excluded.peak_reserved_steady, updated_at=excluded.updated_at, "
        "source=excluded.source",
        (gpu_id, model, int(out_w), int(out_h), int(batch), str(outcome),
         (int(frames) if frames is not None else None),
         _f(seconds), _f(peak_alloc), _f(peak_reserved), _f(free_vram),
         _f(peak_alloc_steady), _f(peak_reserved_steady), now, "local"))
    conn.commit()


@_locked
def get_bench_probes(conn, gpu_id, model, out_w=None, out_h=None):
    """All recorded probes for (gpu_id, model), optionally narrowed to one output cell,
    as a list of plain dicts ordered by output size then batch. Used to RESUME a stopped
    sweep (skip finished batches) and to render the report. Empty list on no history."""
    if not gpu_id or not model:
        return []
    sql = ("SELECT out_w, out_h, batch, outcome, frames, seconds, peak_alloc, "
           "peak_reserved, free_vram, updated_at, source "
           "FROM video_bench WHERE gpu_id=? AND model=?")
    params = [gpu_id, model]
    if out_w and out_h:
        sql += " AND out_w=? AND out_h=?"
        params += [int(out_w), int(out_h)]
    sql += " ORDER BY out_w*out_h, batch"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@_locked
def bench_gpu_ids(conn):
    """Every distinct card present in video_bench, regardless of current live stock. Drives
    the benchmark-share card picker so an out-of-stock remote card's past results can still
    be contributed. Ordered by name."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT gpu_id FROM video_bench ORDER BY gpu_id")]


@_locked
def bench_models(conn, gpu_id):
    """Every distinct regime-tagged `model` key present in video_bench for this card
    (e.g. '7b', '7b|c', '7b|td1024|c'). Used by the benchmark-share exporter to walk
    all of a card's measured regimes. Empty list on no history."""
    if not gpu_id:
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT model FROM video_bench WHERE gpu_id=?", (gpu_id,))]


def _to_float(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _mp_bucket(mp):
    """int output-MP key, matching video_vram_sizer.mp_bucket (MP_KEY_UNIT grid)."""
    return int(round(max(0.0, float(mp or 0)) / MP_KEY_UNIT))


def _import_probe(conn, gpu, model, ow, oh, batch, *, frames, seconds, peak_alloc, free_vram, ts):
    """Write ONE synthetic 'ok' probe (source='imported') for the importer. No commit / no lock:
    the caller (import_bench_rows) holds both. The cell is guaranteed free of a local row."""
    conn.execute(
        "INSERT INTO video_bench (gpu_id, model, out_w, out_h, batch, outcome, frames, "
        "seconds, peak_alloc, free_vram, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(gpu_id, model, out_w, out_h, batch) DO UPDATE SET "
        "outcome=excluded.outcome, frames=excluded.frames, seconds=excluded.seconds, "
        "peak_alloc=excluded.peak_alloc, free_vram=excluded.free_vram, "
        "updated_at=excluded.updated_at, source=excluded.source",
        (gpu, model, int(ow), int(oh), int(batch), "ok",
         (int(frames) if frames else None), _to_float(seconds),
         _to_float(peak_alloc), _to_float(free_vram), ts, "imported"))


def _local_bucket_taken(conn, gpu, model, mp_key):
    """True if this card+regime has a source='local' probe in the SAME MP bucket as `mp_key`.
    The learned-batch key is coarser (per MP bucket) than a video_bench cell, so this guards a
    locally-learned value from being overwritten by an imported cell that maps to its bucket."""
    for w, h in conn.execute(
            "SELECT out_w, out_h FROM video_bench "
            "WHERE gpu_id=? AND model=? AND source='local'", (gpu, model)):
        if _mp_bucket((w * h) / 1_000_000.0) == mp_key:
            return True
    return False


@_locked
def import_bench_rows(conn, rows):
    """Import curated community benchmark SUMMARY rows (#8, bench_share) into video_bench +
    video_batch_learn, with LOCAL PRECEDENCE: a cell (gpu_id, model, out_w, out_h) the user has
    already measured locally (source='local') is left completely untouched, so a download can
    never clobber measured ground truth. Imported rows are tagged source='imported' and stay
    ADVISORY (the sizer's OOM back-off self-corrects a slightly-wrong ceiling on the first run).

    Each `row` is a RESOLVED dict (the caller reconstructs the regime-tagged `model`, the
    sizer's `learn_key` and `mp_key` from the CSV's model+compile+tile): gpu_id, model, out_w,
    out_h, max_batch, used_batch, spf, peak_vram, free_vram, date, learn_key, mp_key. Two
    synthetic 'ok' probes are seeded per cell (the used batch, carrying the timing so the
    report + throughput-optimal reproduce the shared numbers, and the ceiling), plus the sizer's
    learned batch unless a local probe already owns that MP bucket. gpu_perf (the estimate rate)
    is deliberately NOT seeded: it is an accumulating store and the estimator already falls back
    to the author rate table for an unmeasured card. Returns (imported, skipped). Best-effort
    per row; commits once."""
    imported = skipped = 0
    for r in rows:
        try:
            gpu = (r.get("gpu_id") or "").strip()
            model = (r.get("model") or "").strip()
            ow = int(r["out_w"]); oh = int(r["out_h"])
            ceiling = int(r["max_batch"])
            used = int(r.get("used_batch") or ceiling)
        except (KeyError, TypeError, ValueError):
            continue
        if not gpu or not model or ow <= 0 or oh <= 0 or ceiling <= 0 or used <= 0:
            continue
        if conn.execute(
                "SELECT 1 FROM video_bench WHERE gpu_id=? AND model=? AND out_w=? AND out_h=? "
                "AND source='local' LIMIT 1", (gpu, model, ow, oh)).fetchone():
            skipped += 1                                  # local ground truth: never clobber
            continue
        # No local row owns this cell, so any existing rows here are imported: clear them so a
        # refreshed download that changed the numbers doesn't leave stale probes behind.
        conn.execute("DELETE FROM video_bench WHERE gpu_id=? AND model=? AND out_w=? AND out_h=? "
                     "AND source='imported'", (gpu, model, ow, oh))
        date = (r.get("date") or "")[:10]
        ts = (date + "T00:00:00") if date else \
            datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        spf = _to_float(r.get("spf"))
        peak = _to_float(r.get("peak_vram"))
        free = _to_float(r.get("free_vram"))
        _import_probe(conn, gpu, model, ow, oh, used, frames=used,
                      seconds=(spf * used if (spf and used) else None),
                      peak_alloc=peak, free_vram=free, ts=ts)
        if ceiling != used:
            _import_probe(conn, gpu, model, ow, oh, ceiling, frames=None, seconds=None,
                          peak_alloc=peak, free_vram=free, ts=ts)
        lk, mpk = r.get("learn_key"), r.get("mp_key")
        if lk and mpk is not None and not _local_bucket_taken(conn, gpu, model, int(mpk)):
            put_learned_batch(conn, lk, int(mpk), used, updated_at=ts)
        imported += 1
    conn.commit()
    return imported, skipped


@_locked
def max_feasible_output_mp(conn, gpu_id):
    """The largest OUTPUT megapixels PROVEN feasible for this card (feature #7 feasibility
    guard): the biggest 'ok' probe in video_bench plus the biggest learned key in
    video_batch_learn (a present key means a real run fit that output class). Returns None
    when there is no proof yet (the caller then uses the VRAM-tier seed). The learned
    key -> MP is a conservative lower edge (mp_key * MP_KEY_UNIT)."""
    if not gpu_id:
        return None
    best = 0.0
    try:
        for r in conn.execute(
                "SELECT out_w, out_h FROM video_bench WHERE gpu_id=? AND outcome='ok'",
                (gpu_id,)):
            if r["out_w"] and r["out_h"]:
                best = max(best, (r["out_w"] * r["out_h"]) / 1_000_000.0)
    except Exception as exc:                           # noqa: BLE001 (fail-safe)
        debug_log("db.max_feasible_output_mp(video_bench)", exc=exc)
    try:
        for r in conn.execute(
                "SELECT mp_key FROM video_batch_learn WHERE gpu_id = ? OR gpu_id LIKE ?",
                (gpu_id, gpu_id + "|%")):
            if r["mp_key"]:
                best = max(best, r["mp_key"] * MP_KEY_UNIT)
    except Exception as exc:                           # noqa: BLE001 (fail-safe)
        debug_log("db.max_feasible_output_mp(video_batch_learn)", exc=exc)
    return best or None


@_locked
def clear_bench(conn, gpu_id, model=None, cells=None):
    """Delete benchmark probes for a card (all models, or one model), optionally scoped
    to specific (out_w, out_h) targets. For a 'restart' from the GUI. Commits. The derived
    video_batch_learn / gpu_perf rows are left alone (a re-run overwrites them); only the
    raw probe/resume rows are cleared.

    `cells` exists because a card-wide wipe is almost never what a restart means. Ticking
    'Restart' to re-measure ONE target used to discard every target measured on that card:
    the 3090 carried 40 rows across 7 targets, each cell 10+ minutes of GPU time (and a
    remote cell is billed), and the loss was silent until the next sweep found nothing to
    resume. Callers that mean "only the targets I picked" MUST pass them.

    `cells=None` = unscoped (every target). `cells=[]` = an explicitly empty scope and
    clears NOTHING, so a caller that computes a target list can never fall through to the
    card-wide delete by handing over an empty one."""
    if not gpu_id:
        return
    sql = "DELETE FROM video_bench WHERE gpu_id=?"
    params = [gpu_id]
    if model:
        sql += " AND model=?"
        params.append(model)
    if cells is not None:
        pairs = [(int(w), int(h)) for w, h in cells]
        if not pairs:
            return
        sql += " AND (" + " OR ".join(["(out_w=? AND out_h=?)"] * len(pairs)) + ")"
        for w, h in pairs:
            params += [w, h]
    conn.execute(sql, params)
    conn.commit()


# ─────────────────────────────────────────────
#  ROOT HELPERS
# ─────────────────────────────────────────────

@_locked
def get_upscale_root_id(conn, source_root, output_root=None, create=True):
    """Return the id of an upscale root (creating/updating it when create=True)."""
    row = conn.execute("SELECT id FROM upscale_roots WHERE source_root = ?",
                       (source_root,)).fetchone()
    if row is not None:
        if create and output_root is not None:
            conn.execute("UPDATE upscale_roots SET output_root = ? WHERE id = ?",
                         (output_root, row["id"]))
        return row["id"]
    if not create:
        return None
    cur = conn.execute(
        "INSERT INTO upscale_roots (source_root, output_root, saved_at) VALUES (?, ?, ?)",
        (source_root, output_root, datetime.datetime.now().isoformat()))
    return cur.lastrowid


@_locked
def get_tag_root_id(conn, source_root, create=True, created_at=None):
    """Return the id of a tag root (creating it when create=True)."""
    row = conn.execute("SELECT id FROM tag_roots WHERE source_root = ?",
                       (source_root,)).fetchone()
    if row is not None:
        return row["id"]
    if not create:
        return None
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "INSERT INTO tag_roots (source_root, created_at, last_updated) VALUES (?, ?, ?)",
        (source_root, created_at or now, now))
    return cur.lastrowid


@_locked
def find_upscale_root(conn, source_root):
    """Find an upscale root by path (case-insensitive). Returns a Row or None.

    Scans the roots table in Python (SQLite can't case/normcase-fold Windows paths
    in SQL). Fine at real scale (roots number in the low tens); if that ever grew
    to hundreds, store `_norm(source_root)` in an indexed column and look it up
    directly (recommendations item 12). Same note applies to `find_tag_root`."""
    target = _norm(source_root)
    for row in conn.execute("SELECT id, source_root, output_root FROM upscale_roots"):
        if _norm(row["source_root"]) == target:
            return row
    return None


@_locked
def find_tag_root(conn, source_root):
    """Find a tag root by path (case-insensitive). Returns a Row or None."""
    target = _norm(source_root)
    for row in conn.execute("SELECT id, source_root FROM tag_roots"):
        if _norm(row["source_root"]) == target:
            return row
    return None


# ─────────────────────────────────────────────
#  VIDEO UPSCALER  (resume + queue, two granularities)
# ─────────────────────────────────────────────

@_locked
def get_video_root_id(conn, source_root, output_root=None, create=True):
    """Return the id of a video root (creating/updating it when create=True)."""
    row = conn.execute("SELECT id FROM video_roots WHERE source_root = ?",
                       (source_root,)).fetchone()
    if row is not None:
        if create and output_root is not None:
            conn.execute("UPDATE video_roots SET output_root = ? WHERE id = ?",
                         (output_root, row["id"]))
            conn.commit()       # don't leave a write txn open: the runner calls this
        return row["id"]        # then blocks for minutes in session.start(), which
    if not create:              # would hold the WAL write lock and stall the GUI
        return None
    cur = conn.execute(
        "INSERT INTO video_roots (source_root, output_root, saved_at) VALUES (?, ?, ?)",
        (source_root, output_root, datetime.datetime.now().isoformat()))
    conn.commit()
    return cur.lastrowid


@_locked
def _upsert(conn, table, key_cols, key_vals, fields):
    """Generic insert-or-update helper for the video_* tables. Stamps updated_at,
    commits. `key_cols`/`key_vals` are the primary-key columns/values; `fields` is
    the dict of other columns to set.

    SECURITY (recommendations item 12): `table`, `key_cols` and `fields` keys are
    interpolated straight into the SQL, so they MUST stay trusted, code-supplied
    literals — never a user string. Every call site passes hard-coded column names
    (see upsert_video_*), so there is no injection surface; keep it that way if you
    add a caller (bind VALUES as params, as here; never a column/table name)."""
    fields = dict(fields)
    fields["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    where = " AND ".join(f"{c} = ?" for c in key_cols)
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE {where}", key_vals).fetchone()
    if exists is None:
        cols = list(key_cols) + list(fields.keys())
        ph = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})",
                     [*key_vals, *fields.values()])
    else:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                     [*fields.values(), *key_vals])
    conn.commit()


# ── video_files (source properties) ──────────────────────────────────────────

@_locked
def upsert_video_file(conn, root_id, rel_path, **fields):
    """Insert/update source properties (width, height, vcodec, acodec, fps,
    nb_frames, duration, mtime, size, src_hash). Stamps updated_at, commits."""
    _upsert(conn, "video_files", ("root_id", "rel_path"), (root_id, rel_path), fields)


@_locked
def get_video_file(conn, root_id, rel_path):
    """Return the video_files Row for a source video, or None."""
    return conn.execute(
        "SELECT * FROM video_files WHERE root_id = ? AND rel_path = ?",
        (root_id, rel_path)).fetchone()


# Bump when probe() changes what dims/properties it reports, so a re-scan re-probes
# cached rows. 2 = rotation-aware display dimensions (portrait phone clips).
VIDEO_PROBE_VERSION = 2


@_locked
def video_file_is_fresh(conn, root_id, rel_path, mtime, size):
    """True if the cached fast-scan row matches the file's current (mtime, size) AND
    was probed by the current probe version, so a re-scan can skip re-probing it
    (section 15.4). A probe_version bump (e.g. rotation-aware dims) re-probes once."""
    row = conn.execute(
        "SELECT mtime, size, probe_version FROM video_files "
        "WHERE root_id = ? AND rel_path = ?",
        (root_id, rel_path)).fetchone()
    return bool(row and row["mtime"] == round(mtime, 3) and row["size"] == size
                and row["probe_version"] == VIDEO_PROBE_VERSION)


# ── video_outputs (the per-target job = the durable queue) ───────────────────

@_locked
def upsert_video_output(conn, root_id, rel_path, target, clip_id=0, **fields):
    """Insert/update one (source, target, clip) job row (status, skip_reason,
    output_path, out_frames, queue_order, and for a clip the clip_start/clip_end/
    clip_label/clip_frames). clip_id defaults to 0 (the whole-file job). Sets
    created_at on first insert."""
    if conn.execute(
            "SELECT 1 FROM video_outputs WHERE root_id = ? AND rel_path = ? "
            "AND target = ? AND clip_id = ?",
            (root_id, rel_path, target, clip_id)).fetchone() is None:
        fields.setdefault("created_at",
                          datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    _upsert(conn, "video_outputs", ("root_id", "rel_path", "target", "clip_id"),
            (root_id, rel_path, target, clip_id), fields)


@_locked
def get_video_output(conn, root_id, rel_path, target, clip_id=0):
    """Return the (source, target, clip) job Row, or None. clip_id defaults to 0
    (the whole-file job)."""
    return conn.execute(
        "SELECT * FROM video_outputs WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?",
        (root_id, rel_path, target, clip_id)).fetchone()


@_locked
def get_video_outputs_for(conn, root_id, rel_path, clip_id=0):
    """All target jobs for one source at the given clip_id (default 0 = the
    whole-file counterparts the scan list shows). Clips are queried separately via
    get_video_clips."""
    return conn.execute(
        "SELECT * FROM video_outputs WHERE root_id = ? AND rel_path = ? "
        "AND clip_id = ? ORDER BY target",
        (root_id, rel_path, clip_id)).fetchall()


@_locked
def get_video_outputs_all(conn, root_id, clip_id=0):
    """Every output job for a root in ONE query, so the GUI can refresh a large
    scan list without a query per file (which froze the UI thread). Defaults to the
    whole-file jobs (clip_id = 0); the scan list shows those, clips have their own
    manager (get_video_clips)."""
    return conn.execute(
        "SELECT rel_path, target, clip_id, status, output_path FROM video_outputs "
        "WHERE root_id = ? AND clip_id = ?", (root_id, clip_id)).fetchall()


@_locked
def get_video_clips(conn, root_id):
    """Every VIRTUAL clip job (clip_id > 0) for a root, for the Segments manager
    (section 16.5). Ordered by source then clip start so a source's scenes read in
    timeline order."""
    return conn.execute(
        "SELECT * FROM video_outputs WHERE root_id = ? AND clip_id > 0 "
        "ORDER BY rel_path, clip_start, target", (root_id,)).fetchall()


@_locked
def next_clip_id(conn, root_id, rel_path):
    """The next clip_id to assign for a source (max existing + 1, min 1 since 0 is
    reserved for the whole-file job). Clip ids are per (root, source)."""
    row = conn.execute(
        "SELECT MAX(clip_id) AS m FROM video_outputs WHERE root_id = ? AND rel_path = ?",
        (root_id, rel_path)).fetchone()
    return (row["m"] + 1) if row and row["m"] else 1


@_locked
def get_video_queue(conn, root_id):
    """The queue: every job not yet 'done'/'skipped', in queue order. This IS the
    durable, restart-surviving queue + resume set (section 15.1)."""
    return conn.execute(
        "SELECT * FROM video_outputs WHERE root_id = ? "
        "AND status NOT IN ('done', 'skipped') "
        "ORDER BY queue_order, rowid", (root_id,)).fetchall()


@_locked
def get_active_video_output_paths(conn):
    """Every non-terminal job's output_path across ALL roots. The staging-dir sweep
    (item 5) uses this to know which staging folders are still owned: a done/skipped
    job, or one with no output_path yet, owns no folder to protect. Cross-root because
    the staging base is shared, so a sweep for one root must not delete another's dirs."""
    return [r["output_path"] for r in conn.execute(
        "SELECT output_path FROM video_outputs "
        "WHERE status NOT IN ('done', 'skipped') AND output_path IS NOT NULL").fetchall()]


@_locked
def next_queue_order(conn, root_id):
    """The next queue_order to append at the end of the queue."""
    row = conn.execute(
        "SELECT MAX(queue_order) AS m FROM video_outputs WHERE root_id = ?",
        (root_id,)).fetchone()
    return (row["m"] + 1) if row and row["m"] is not None else 0


@_locked
def set_queue_order(conn, root_id, rel_path, target, order, clip_id=0):
    """Reorder one queued job. clip_id defaults to 0 (the whole-file job). Commits."""
    conn.execute(
        "UPDATE video_outputs SET queue_order = ? WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?", (order, root_id, rel_path, target, clip_id))
    conn.commit()


@_locked
def bump_video_fail_count(conn, root_id, rel_path, target, clip_id=0):
    """Increment and return a job's consecutive fail_count (0 -> 1 on the first
    failure). Atomic under the shared-connection lock. The runner uses the returned
    count to decide when a deterministically-failing job should be dropped from the
    queue (item 4). Returns 0 if the row is somehow gone."""
    conn.execute(
        "UPDATE video_outputs SET fail_count = COALESCE(fail_count, 0) + 1 "
        "WHERE root_id = ? AND rel_path = ? AND target = ? AND clip_id = ?",
        (root_id, rel_path, target, clip_id))
    conn.commit()
    row = conn.execute(
        "SELECT fail_count FROM video_outputs WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?",
        (root_id, rel_path, target, clip_id)).fetchone()
    return (row["fail_count"] if row else 0) or 0


@_locked
def reset_video_fail_count(conn, root_id, rel_path, target, clip_id=0):
    """Zero a job's fail_count (the GUI 'Retry' path: the user re-queues a job that
    gave up, so its attempt counter must start fresh). Commits."""
    conn.execute(
        "UPDATE video_outputs SET fail_count = 0 WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?", (root_id, rel_path, target, clip_id))
    conn.commit()


@_locked
def delete_video_output(conn, root_id, rel_path, target, clip_id=0):
    """Remove a job from the queue (and its segment rows). clip_id defaults to 0
    (the whole-file job). Commits."""
    conn.execute(
        "DELETE FROM video_segments WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?", (root_id, rel_path, target, clip_id))
    conn.execute(
        "DELETE FROM video_outputs WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?", (root_id, rel_path, target, clip_id))
    conn.commit()


# ── video_segments (per-target/clip resume) ──────────────────────────────────

@_locked
def upsert_video_segment(conn, root_id, rel_path, target, seg_index, clip_id=0, **fields):
    """Insert/update one segment row (in_frames, out_frames, status, seconds,
    output_path). clip_id defaults to 0 (the whole-file job). Stamps updated_at,
    commits."""
    _upsert(conn, "video_segments",
            ("root_id", "rel_path", "target", "clip_id", "seg_index"),
            (root_id, rel_path, target, clip_id, seg_index), fields)


@_locked
def get_video_segments(conn, root_id, rel_path, target, clip_id=0):
    """All segment rows for a (source, target, clip) job, ordered by index. clip_id
    defaults to 0 (the whole-file job)."""
    return conn.execute(
        "SELECT * FROM video_segments WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ? ORDER BY seg_index",
        (root_id, rel_path, target, clip_id)).fetchall()


@_locked
def clear_video_segments(conn, root_id, rel_path, target, clip_id=0):
    """Drop a job's segment rows (a fresh split supersedes the old plan). clip_id
    defaults to 0 (the whole-file job). Commits."""
    conn.execute(
        "DELETE FROM video_segments WHERE root_id = ? AND rel_path = ? "
        "AND target = ? AND clip_id = ?", (root_id, rel_path, target, clip_id))
    conn.commit()


# ─────────────────────────────────────────────
#  CONTENT HASHING  (file identity that survives moves/renames)
# ─────────────────────────────────────────────

def content_hash(path, _bufsize=1 << 20):
    """Return the blake2b-256 hex digest of a file's bytes, or None if it can't
    be read. blake2b is in the standard library and faster than SHA-256."""
    try:
        h = hashlib.blake2b(digest_size=32)
        with open(path, "rb", buffering=0) as f:
            for chunk in iter(lambda: f.read(_bufsize), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def hash_file_cached(conn, path):
    """
    Content hash of `path`, memoised in file_hashes by (path, mtime, size); the
    file is re-read only when that fingerprint changes. Does NOT commit — the
    caller commits (so a big rescan flushes once). Returns the hex digest or None.

    Deliberately NOT @_locked: it reads the whole file (a multi-GB video in the
    Video Upscaler path), and holding the global DB lock across that read would
    stall every other DB op. The only race is two threads hashing the same file at
    once, which is harmless — both write the identical digest (INSERT OR REPLACE),
    and SQLite's own mutex keeps each individual statement safe.
    """
    norm = _norm(path)
    try:
        st = os.stat(path)
        mtime, size = round(st.st_mtime, 3), st.st_size
    except OSError:
        return None
    row = conn.execute(
        "SELECT mtime, size, hash FROM file_hashes WHERE path = ?", (norm,)).fetchone()
    if row is not None and row["mtime"] == mtime and row["size"] == size and row["hash"]:
        return row["hash"]
    digest = content_hash(path)
    if digest is None:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO file_hashes (path, mtime, size, hash) VALUES (?, ?, ?, ?)",
        (norm, mtime, size, digest))
    return digest


# ─────────────────────────────────────────────
#  LINEAGE  (source → upscaled → tagged, by content hash)
# ─────────────────────────────────────────────

@_locked
def record_upscale_lineage(conn, src_hash, upscaled_hash, src_path=None, upscaled_path=None):
    """
    Link a source photo to its upscaled output. Keyed on src_hash: re-upscaling
    the same source updates the row (and clears any stale tagged_hash, since the
    previous tagging applied to the old, now-replaced upscaled file).
    """
    if not src_hash or not upscaled_hash:
        return
    now = datetime.datetime.now().isoformat()
    row = conn.execute("SELECT id FROM lineage WHERE src_hash = ?", (src_hash,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO lineage (src_hash, upscaled_hash, src_path, upscaled_path, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (src_hash, upscaled_hash, src_path, upscaled_path, now))
    else:
        conn.execute(
            "UPDATE lineage SET upscaled_hash = ?, src_path = ?, upscaled_path = ?, "
            "tagged_hash = NULL, tagged_path = NULL, updated_at = ? WHERE id = ?",
            (upscaled_hash, src_path, upscaled_path, now, row["id"]))
    conn.commit()


@_locked
def record_tag_lineage(conn, in_hash, tagged_hash, tagged_path=None):
    """
    Link a tagged & renamed file back to the upscaled file it was made from
    (matched by content hash == in_hash). If the input is not a known upscaled
    output (a tag-only tree), a standalone row is created with the input as both
    source and upscaled base so conciliation can still match it by content.
    """
    if not in_hash or not tagged_hash:
        return
    now = datetime.datetime.now().isoformat()
    row = conn.execute("SELECT id FROM lineage WHERE upscaled_hash = ?", (in_hash,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE lineage SET tagged_hash = ?, tagged_path = ?, updated_at = ? WHERE id = ?",
            (tagged_hash, tagged_path, now, row["id"]))
    else:
        conn.execute(
            "INSERT INTO lineage (src_hash, upscaled_hash, tagged_hash, tagged_path, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (in_hash, in_hash, tagged_hash, tagged_path, now))
    conn.commit()


@_locked
def lineage_final_hash(conn, src_hash):
    """
    Given a source photo's content hash, return the content hash of its final
    processed counterpart (tagged if tagged, otherwise upscaled), or None.
    """
    row = conn.execute(
        "SELECT upscaled_hash, tagged_hash FROM lineage WHERE src_hash = ?",
        (src_hash,)).fetchone()
    if row is None:
        return None
    return row["tagged_hash"] or row["upscaled_hash"]


@_locked
def lineage_has_rows(conn):
    """True if any lineage has been recorded (lets callers skip hashing work)."""
    return conn.execute("SELECT 1 FROM lineage LIMIT 1").fetchone() is not None


# ─────────────────────────────────────────────
#  LEGACY JSON IMPORT  (one-shot, on db creation)
# ─────────────────────────────────────────────

@_locked
def import_legacy_json(conn):
    """
    Import the old JSON caches into the freshly created database. A cache is
    imported only if its source folder still exists; stale caches are skipped.
    """
    up_roots = up_files = up_skipped = 0
    tg_roots = tg_files = tg_skipped = 0

    # ── Eligibility caches (scans/cache_*.json) ────────────────────────────────
    scans_dir = os.path.join(APP_ROOT, "scans")
    if os.path.isdir(scans_dir):
        for name in sorted(os.listdir(scans_dir)):
            if not (name.startswith("cache_") and name.endswith(".json")):
                continue
            data = _load_json(os.path.join(scans_dir, name))
            if data is None:
                continue
            src = data.get("source_root", "")
            if not src or not os.path.isdir(src):
                up_skipped += 1
                continue
            root_id = get_upscale_root_id(conn, src, data.get("output_root"))
            rows = []
            for rel, e in (data.get("entries") or {}).items():
                rows.append((root_id, rel,
                             e.get("mtime"), e.get("size"),
                             1 if e.get("eligible") else 0,
                             1 if e.get("already_done") else 0,
                             e.get("skip_reason")))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO upscale_files "
                    "(root_id, rel_path, mtime, size, eligible, already_done, skip_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            up_roots += 1
            up_files += len(rows)

    # ── Tag & rename caches (trcache/*.cache) ──────────────────────────────────
    tr_dir = os.path.join(APP_ROOT, "trcache")
    if os.path.isdir(tr_dir):
        for name in sorted(os.listdir(tr_dir)):
            if not name.endswith(".cache"):
                continue
            data = _load_json(os.path.join(tr_dir, name))
            if data is None:
                continue
            src = data.get("source_root", "")
            if not src or not os.path.isdir(src):
                tg_skipped += 1
                continue
            root_id = get_tag_root_id(conn, src, created_at=data.get("created_at"))
            rows = []
            for key, e in (data.get("files") or {}).items():
                rows.append((root_id,
                             e.get("original_rel_path", key),
                             e.get("current_rel_path"),
                             e.get("status"),
                             json.dumps(e, ensure_ascii=False)))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO tag_files "
                    "(root_id, original_rel_path, current_rel_path, status, entry_json) "
                    "VALUES (?, ?, ?, ?, ?)", rows)
            tg_roots += 1
            tg_files += len(rows)

    conn.commit()
    if up_roots or tg_roots or up_skipped or tg_skipped:
        print(f"  [db] Imported eligibility cache: {up_roots} folder(s), "
              f"{up_files} file(s) ({up_skipped} stale folder(s) skipped).")
        print(f"  [db] Imported tag/rename cache: {tg_roots} folder(s), "
              f"{tg_files} file(s) ({tg_skipped} stale folder(s) skipped).")


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None
