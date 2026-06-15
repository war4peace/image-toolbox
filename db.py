"""
db.py
-----
Single SQLite cache database for the toolbox, at db/cache.db.

Replaces the per-folder JSON cache files that used to live in scans/ (the
upscale eligibility cache) and trcache/ (the tag & rename cache). One database,
two pairs of tables:

    upscale_roots / upscale_files   – eligibility cache (batch_upscale.py)
    tag_roots     / tag_files       – tag & rename cache (tag_and_rename.py)

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
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR     = os.path.join(SCRIPT_DIR, "db")
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
    scans_dir = os.path.join(SCRIPT_DIR, "scans")
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
    tr_dir = os.path.join(SCRIPT_DIR, "trcache")
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
