"""
db.py — the video-table migration and the content-hash lineage round trip
(item 2). Both run against a throwaway cache.db in a tmp dir: the `db_conn`
fixture repoints db's module-level paths, resets the process-wide connection, and
stubs the one-shot legacy-JSON import so nothing on the real machine is touched.
"""

import sqlite3
import threading

import pytest

import db


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """A fresh cache.db under tmp_path, via the real get_conn (schema + migrations)."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ── schema / migration ──────────────────────────────────────────────────────

def test_fresh_db_has_the_normalised_video_shape(db_conn):
    cols = _columns(db_conn, "video_files")
    assert "target" not in cols          # the normalised shape moved target out
    assert "probe_version" in cols       # _ensure_video_columns / SCHEMA
    assert "video_outputs" in _tables(db_conn)
    assert "video_segments" in _tables(db_conn)


def test_phase4_video_files_are_migrated_on_open(tmp_path, monkeypatch):
    # Simulate an old DB carrying the phase-4 shape: video_files WITH a target
    # column and no video_outputs table.
    dbfile = tmp_path / "cache.db"
    old = sqlite3.connect(str(dbfile))
    old.executescript(
        "CREATE TABLE video_files (root_id INTEGER, rel_path TEXT, "
        "target TEXT, status TEXT);"
        "INSERT INTO video_files VALUES (1, 'a.mp4', '4K', 'done');")
    old.commit()
    old.close()

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)

    conn = db.get_conn()
    try:
        cols = _columns(conn, "video_files")
        assert "target" not in cols               # old table was dropped + recreated
        assert "video_outputs" in _tables(conn)   # the normalised sibling now exists
    finally:
        conn.close()
        monkeypatch.setattr(db, "_conn", None)


# ── segment-extractor clip_id (section 16.6) ─────────────────────────────────

def test_fresh_db_has_clip_columns(db_conn):
    ocols = _columns(db_conn, "video_outputs")
    for c in ("clip_id", "clip_start", "clip_end", "clip_label", "clip_frames"):
        assert c in ocols, ocols
    assert "clip_id" in _columns(db_conn, "video_segments")


def test_clip_jobs_coexist_with_whole_file_job(db_conn):
    root = db.get_video_root_id(db_conn, "/src", create=True)
    # whole-file job (clip_id defaults to 0)
    db.upsert_video_output(db_conn, root, "a.mp4", "1080p", status="queued", queue_order=0)
    assert db.next_clip_id(db_conn, root, "a.mp4") == 1
    c1 = db.next_clip_id(db_conn, root, "a.mp4")
    db.upsert_video_output(db_conn, root, "a.mp4", "1080p", clip_id=c1, status="queued",
                           queue_order=1, clip_start=151.0, clip_end=312.3,
                           clip_label="cake", clip_frames=4830)
    c2 = db.next_clip_id(db_conn, root, "a.mp4")
    assert c2 == 2
    db.upsert_video_output(db_conn, root, "a.mp4", "1080p", clip_id=c2, status="queued",
                           queue_order=2, clip_start=660.0, clip_end=800.0,
                           clip_label="speeches")
    # whole-file getters ignore clips; the clip getter sees both in timeline order
    assert len(db.get_video_outputs_for(db_conn, root, "a.mp4")) == 1
    assert len(db.get_video_outputs_all(db_conn, root)) == 1
    clips = db.get_video_clips(db_conn, root)
    assert [c["clip_label"] for c in clips] == ["cake", "speeches"]
    assert clips[0]["clip_id"] == c1 and clips[0]["clip_frames"] == 4830
    # the durable queue includes the whole-file job and both clips
    assert len(db.get_video_queue(db_conn, root)) == 3


def test_segments_and_delete_are_clip_scoped(db_conn):
    root = db.get_video_root_id(db_conn, "/src", create=True)
    db.upsert_video_output(db_conn, root, "a.mp4", "4K", status="queued")
    db.upsert_video_output(db_conn, root, "a.mp4", "4K", clip_id=1, status="queued",
                           clip_start=1.0, clip_end=9.0, clip_label="x")
    db.upsert_video_segment(db_conn, root, "a.mp4", "4K", 0, in_frames=100, status="pending")
    db.upsert_video_segment(db_conn, root, "a.mp4", "4K", 0, clip_id=1,
                            in_frames=50, status="pending")
    assert len(db.get_video_segments(db_conn, root, "a.mp4", "4K")) == 1
    assert len(db.get_video_segments(db_conn, root, "a.mp4", "4K", clip_id=1)) == 1
    # deleting the clip leaves the whole-file job and its segment intact
    db.delete_video_output(db_conn, root, "a.mp4", "4K", clip_id=1)
    assert db.get_video_clips(db_conn, root) == []
    assert len(db.get_video_segments(db_conn, root, "a.mp4", "4K", clip_id=1)) == 0
    assert db.get_video_output(db_conn, root, "a.mp4", "4K") is not None
    assert len(db.get_video_segments(db_conn, root, "a.mp4", "4K")) == 1


def test_preclip_db_migrates_preserving_rows(tmp_path, monkeypatch):
    # An existing (pre-clip) DB has the video tables WITHOUT clip_id in the PK. The
    # migration must rebuild both tables, preserve every row as a whole-file
    # (clip_id 0) job, and recreate the queue index — an in-flight queue survives.
    dbfile = tmp_path / "cache.db"
    old = sqlite3.connect(str(dbfile))
    old.executescript(
        "CREATE TABLE video_roots (id INTEGER PRIMARY KEY, source_root TEXT NOT NULL "
        "UNIQUE, output_root TEXT, saved_at TEXT);"
        "CREATE TABLE video_files (root_id INTEGER, rel_path TEXT, probe_version INTEGER,"
        " PRIMARY KEY (root_id, rel_path));"
        "CREATE TABLE video_outputs (root_id INTEGER NOT NULL, rel_path TEXT NOT NULL, "
        "target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', skip_reason TEXT, "
        "output_path TEXT, out_frames INTEGER, queue_order INTEGER, created_at TEXT, "
        "updated_at TEXT, PRIMARY KEY (root_id, rel_path, target));"
        "CREATE INDEX idx_video_outputs_queue ON video_outputs(root_id, queue_order);"
        "CREATE TABLE video_segments (root_id INTEGER NOT NULL, rel_path TEXT NOT NULL, "
        "target TEXT NOT NULL, seg_index INTEGER NOT NULL, in_frames INTEGER, "
        "out_frames INTEGER, status TEXT NOT NULL DEFAULT 'pending', seconds REAL, "
        "output_path TEXT, updated_at TEXT, PRIMARY KEY (root_id, rel_path, target, seg_index));"
        "CREATE INDEX idx_video_seg_parent ON video_segments(root_id, rel_path, target);"
        "INSERT INTO video_roots (id, source_root, output_root) VALUES (1, '/src', '/out');"
        "INSERT INTO video_outputs (root_id, rel_path, target, status, output_path, "
        "queue_order, created_at) VALUES (1, 'v.mp4', '4K', 'partial', '/out/v_4K.mp4', 7, 't');"
        "INSERT INTO video_segments (root_id, rel_path, target, seg_index, in_frames, status) "
        "VALUES (1, 'v.mp4', '4K', 0, 500, 'done');")
    old.commit()
    old.close()

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)

    conn = db.get_conn()
    try:
        assert "clip_id" in _columns(conn, "video_outputs")
        row = db.get_video_output(conn, 1, "v.mp4", "4K")
        assert row["clip_id"] == 0 and row["status"] == "partial" and row["queue_order"] == 7
        segs = db.get_video_segments(conn, 1, "v.mp4", "4K")
        assert len(segs) == 1 and segs[0]["in_frames"] == 500 and segs[0]["clip_id"] == 0
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='video_outputs'")}
        assert "idx_video_outputs_queue" in idx      # index recreated on the new table
        assert "_vo_old" not in _tables(conn) and "_vs_old" not in _tables(conn)
    finally:
        conn.close()
        monkeypatch.setattr(db, "_conn", None)


# ── lineage round trip ──────────────────────────────────────────────────────

def test_upscale_then_tag_lineage(db_conn):
    assert db.lineage_has_rows(db_conn) is False

    db.record_upscale_lineage(db_conn, "srcA", "upA",
                              src_path="a.jpg", upscaled_path="a_up.jpg")
    assert db.lineage_has_rows(db_conn) is True
    assert db.lineage_final_hash(db_conn, "srcA") == "upA"   # upscaled is final so far

    db.record_tag_lineage(db_conn, "upA", "tagA", tagged_path="a_tagged.jpg")
    assert db.lineage_final_hash(db_conn, "srcA") == "tagA"  # tagged wins


def test_reupscale_clears_the_stale_tag(db_conn):
    db.record_upscale_lineage(db_conn, "srcA", "upA")
    db.record_tag_lineage(db_conn, "upA", "tagA")
    # Re-upscaling the same source replaces the output and invalidates the tag
    # that applied to the now-gone upscaled file.
    db.record_upscale_lineage(db_conn, "srcA", "upA2")
    assert db.lineage_final_hash(db_conn, "srcA") == "upA2"


def test_tag_only_tree_creates_standalone_lineage(db_conn):
    # Tagging an input that is not a known upscaled output still records a row so
    # conciliation can match it by content (src == upscaled base).
    db.record_tag_lineage(db_conn, "srcX", "tagX")
    assert db.lineage_final_hash(db_conn, "srcX") == "tagX"


def test_empty_hashes_are_noops(db_conn):
    db.record_upscale_lineage(db_conn, "", "upA")
    db.record_upscale_lineage(db_conn, "srcA", "")
    db.record_tag_lineage(db_conn, None, "tagA")
    assert db.lineage_has_rows(db_conn) is False


def test_unknown_source_has_no_final_hash(db_conn):
    assert db.lineage_final_hash(db_conn, "nope") is None


# ── GUI-threading safety (item 8: the _LOCK serialising the shared conn) ──────

def _run_threads(target, count):
    """Start `count` threads on target(i), join them, return any exceptions."""
    errors = []

    def wrapped(i):
        try:
            target(i)
        except Exception as exc:            # capture so the assert can show it
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_writers_distinct_rows_all_land(db_conn):
    # Many worker threads writing distinct rows through @_locked helpers on the one
    # shared connection: every row must land and none may raise.
    root = db.get_video_root_id(db_conn, "/src", create=True)
    n = 40
    errors = _run_threads(
        lambda i: db.upsert_video_output(db_conn, root, f"clip{i}.mp4", "4K",
                                         status="queued", queue_order=i), n)
    assert not errors, errors
    assert len(db.get_video_outputs_all(db_conn, root)) == n


def test_concurrent_upserts_same_row_no_duplicate_insert(db_conn):
    # The real discriminator for the lock: _upsert does a SELECT-exists then an
    # INSERT/UPDATE in Python. Many threads first-inserting the SAME (rel_path,
    # target) would, unserialised, both see "not exists" and both plain-INSERT,
    # raising a duplicate-PK IntegrityError. Under _LOCK exactly one row results.
    root = db.get_video_root_id(db_conn, "/src", create=True)
    errors = _run_threads(
        lambda i: db.upsert_video_output(db_conn, root, "same.mp4", "4K",
                                         status=f"s{i}"), 40)
    assert not errors, errors
    assert len(db.get_video_outputs_for(db_conn, root, "same.mp4")) == 1


def test_lock_is_reentrant(db_conn):
    # A @_locked helper may be called while already holding _LOCK (helpers call
    # each other, e.g. upsert_video_* -> _upsert): RLock must not deadlock.
    root = db.get_video_root_id(db_conn, "/src", create=True)
    with db._LOCK:
        db.upsert_video_output(db_conn, root, "a.mp4", "4K", status="queued")
        assert db.get_video_output(db_conn, root, "a.mp4", "4K") is not None
