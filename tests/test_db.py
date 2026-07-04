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
