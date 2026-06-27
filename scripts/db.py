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
renames folders. See docs/content-hash-lineage.md.

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

-- Video Upscaler (#2) resume + queue state, mirroring the upscale eligibility
-- pattern but at two granularities (video, then segment) so a stopped run
-- resumes at the first unfinished SEGMENT, not the first unfinished video — the
-- installment model (docs/video-upscaler.md section 5). No GPU/torch here: the
-- runner is pure orchestration + local ffmpeg.
CREATE TABLE IF NOT EXISTS video_roots (
    id          INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL UNIQUE,
    output_root TEXT,
    saved_at    TEXT
);
CREATE TABLE IF NOT EXISTS video_files (
    root_id      INTEGER NOT NULL REFERENCES video_roots(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER,
    fps          REAL,
    frames       INTEGER,          -- COUNTED frames (not the unreliable header), see 14
    duration     REAL,
    target       TEXT,             -- 1080p | 1440p | 4K chosen for this video
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|splitting|streaming|done|failed|skipped
    skip_reason  TEXT,
    output_path  TEXT,
    out_frames   INTEGER,          -- frames in the reassembled output (drift check)
    updated_at   TEXT,
    PRIMARY KEY (root_id, rel_path)
);
CREATE TABLE IF NOT EXISTS video_segments (
    video_root_id INTEGER NOT NULL,
    video_rel     TEXT NOT NULL,            -- the parent video's rel_path
    seg_index     INTEGER NOT NULL,
    in_frames     INTEGER,                  -- source frames in this segment
    out_frames    INTEGER,                  -- upscaled frames returned by the worker
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed
    seconds       REAL,                     -- worker process time for this segment
    output_path   TEXT,                     -- the upscaled segment file (kept for reassembly)
    updated_at    TEXT,
    PRIMARY KEY (video_root_id, video_rel, seg_index),
    FOREIGN KEY (video_root_id) REFERENCES video_roots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_seg_parent
    ON video_segments(video_root_id, video_rel);

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
"""

_conn = None   # one connection per process


def get_conn():
    """Open (once per process) and return the shared cache.db connection."""
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(DB_DIR, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    if fresh:
        try:
            import_legacy_json(conn)
        except Exception as exc:
            print(f"  [db] Legacy cache import skipped due to error: {exc}")
    _conn = conn
    return conn


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


# ─────────────────────────────────────────────
#  GPU PERFORMANCE (remote cost estimator)
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  ROOT HELPERS
# ─────────────────────────────────────────────

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


def find_upscale_root(conn, source_root):
    """Find an upscale root by path (case-insensitive). Returns a Row or None."""
    target = _norm(source_root)
    for row in conn.execute("SELECT id, source_root, output_root FROM upscale_roots"):
        if _norm(row["source_root"]) == target:
            return row
    return None


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

def get_video_root_id(conn, source_root, output_root=None, create=True):
    """Return the id of a video root (creating/updating it when create=True)."""
    row = conn.execute("SELECT id FROM video_roots WHERE source_root = ?",
                       (source_root,)).fetchone()
    if row is not None:
        if create and output_root is not None:
            conn.execute("UPDATE video_roots SET output_root = ? WHERE id = ?",
                         (output_root, row["id"]))
        return row["id"]
    if not create:
        return None
    cur = conn.execute(
        "INSERT INTO video_roots (source_root, output_root, saved_at) VALUES (?, ?, ?)",
        (source_root, output_root, datetime.datetime.now().isoformat()))
    return cur.lastrowid


def upsert_video_file(conn, root_id, rel_path, **fields):
    """Insert or update a video_files row. `fields` may set any of: width, height,
    fps, frames, duration, target, status, skip_reason, output_path, out_frames.
    Always stamps updated_at. Commits."""
    fields["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cols = ["root_id", "rel_path"] + list(fields.keys())
    existing = conn.execute(
        "SELECT 1 FROM video_files WHERE root_id = ? AND rel_path = ?",
        (root_id, rel_path)).fetchone()
    if existing is None:
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO video_files ({', '.join(cols)}) VALUES ({placeholders})",
                     [root_id, rel_path, *fields.values()])
    else:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE video_files SET {sets} WHERE root_id = ? AND rel_path = ?",
                     [*fields.values(), root_id, rel_path])
    conn.commit()


def get_video_file(conn, root_id, rel_path):
    """Return the video_files Row for a source video, or None."""
    return conn.execute(
        "SELECT * FROM video_files WHERE root_id = ? AND rel_path = ?",
        (root_id, rel_path)).fetchone()


def upsert_video_segment(conn, root_id, video_rel, seg_index, **fields):
    """Insert or update one video_segments row (in_frames, out_frames, status,
    seconds, output_path). Stamps updated_at. Commits."""
    fields["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cols = ["video_root_id", "video_rel", "seg_index"] + list(fields.keys())
    existing = conn.execute(
        "SELECT 1 FROM video_segments WHERE video_root_id = ? AND video_rel = ? "
        "AND seg_index = ?", (root_id, video_rel, seg_index)).fetchone()
    if existing is None:
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO video_segments ({', '.join(cols)}) VALUES ({placeholders})",
                     [root_id, video_rel, seg_index, *fields.values()])
    else:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE video_segments SET {sets} WHERE video_root_id = ? AND "
            "video_rel = ? AND seg_index = ?",
            [*fields.values(), root_id, video_rel, seg_index])
    conn.commit()


def get_video_segments(conn, root_id, video_rel):
    """All segment rows for a video, ordered by index."""
    return conn.execute(
        "SELECT * FROM video_segments WHERE video_root_id = ? AND video_rel = ? "
        "ORDER BY seg_index", (root_id, video_rel)).fetchall()


def clear_video_segments(conn, root_id, video_rel):
    """Drop a video's segment rows (a fresh split supersedes the old plan)."""
    conn.execute(
        "DELETE FROM video_segments WHERE video_root_id = ? AND video_rel = ?",
        (root_id, video_rel))
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


def lineage_has_rows(conn):
    """True if any lineage has been recorded (lets callers skip hashing work)."""
    return conn.execute("SELECT 1 FROM lineage LIMIT 1").fetchone() is not None


# ─────────────────────────────────────────────
#  LEGACY JSON IMPORT  (one-shot, on db creation)
# ─────────────────────────────────────────────

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
