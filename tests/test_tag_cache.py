"""
Tests for item 5: Tag & Rename's cache is now incremental (dirty-set) with an
O(1) lookup index, instead of a full DELETE + re-INSERT of every row after every
single image plus a linear _find_entry scan.

Two things must hold:
  * correctness — an incremental save must NOT drop the rows it didn't touch
    (the whole risk of removing the old DELETE-all), and the index must resolve a
    renamed file to its entry; and
  * the fast-path/fallback split — a cache built without the index (e.g. by hand)
    still resolves via a linear scan.

The cache logic is torch/PIL-free (EXIF snapshotting fails safe without piexif),
so these run everywhere. The DB round-trip uses a throwaway cache.db in a tmp dir.
"""

import os

import pytest

import db
import tag_and_rename as tr


@pytest.fixture
def tag_db(tmp_path, monkeypatch):
    """A fresh cache.db under tmp_path, via the real get_conn (schema included)."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


# ── _find_entry: O(1) index + linear fallback ────────────────────────────────

def test_find_entry_uses_index_for_original_and_renamed(tmp_path):
    root = str(tmp_path)
    cache = {"files": {}, "_index": {}, "_dirty": set()}
    cache["files"]["a.jpg"] = {"original_rel_path": "a.jpg", "current_rel_path": "a.jpg"}
    tr._index_set(cache, "a.jpg", "a.jpg")
    cache["files"]["b.jpg"] = {"original_rel_path": "b.jpg", "current_rel_path": "b_desc.jpg"}
    tr._index_set(cache, "b.jpg", "b_desc.jpg")

    assert tr._find_entry(cache, root, os.path.join(root, "a.jpg"))[0] == "a.jpg"
    # renamed file, looked up by its CURRENT name -> resolved via the index
    assert tr._find_entry(cache, root, os.path.join(root, "b_desc.jpg"))[0] == "b.jpg"
    # still resolvable by its ORIGINAL key (direct dict hit)
    assert tr._find_entry(cache, root, os.path.join(root, "b.jpg"))[0] == "b.jpg"
    # a genuinely absent file -> (None, None), in O(1)
    assert tr._find_entry(cache, root, os.path.join(root, "missing.jpg")) == (None, None)


def test_find_entry_without_index_falls_back_to_scan(tmp_path):
    root = str(tmp_path)
    cache = {"files": {"b.jpg": {"original_rel_path": "b.jpg",
                                 "current_rel_path": "b_desc.jpg"}}}
    # No "_index" key at all (a hand-built cache) — linear scan still finds it.
    assert tr._find_entry(cache, root, os.path.join(root, "b_desc.jpg"))[0] == "b.jpg"


# ── ensure/update maintain the dirty set + index ─────────────────────────────

def test_ensure_and_update_track_dirty_and_index(tmp_path):
    root = str(tmp_path)
    cache = {"files": {}, "_index": {}, "_dirty": set()}

    key = tr.ensure_cache_entry(cache, root, os.path.join(root, "photo.jpg"))
    assert key == "photo.jpg"
    assert "photo.jpg" in cache["_dirty"]
    assert cache["_index"][os.path.normcase("photo.jpg")] == "photo.jpg"

    cache["_dirty"].clear()
    tr.update_cache_entry(cache, root, os.path.join(root, "photo.jpg"),
                          os.path.join(root, "photo_cat.jpg"), "processed")

    entry = cache["files"]["photo.jpg"]
    assert entry["current_rel_path"] == "photo_cat.jpg"
    assert entry["status"] == "processed"
    assert "photo.jpg" in cache["_dirty"]
    # index moved: old current name gone, new current name present
    assert os.path.normcase("photo.jpg") not in cache["_index"]
    assert cache["_index"][os.path.normcase("photo_cat.jpg")] == "photo.jpg"
    assert tr._find_entry(cache, root, os.path.join(root, "photo_cat.jpg"))[0] == "photo.jpg"


# ── incremental save must not drop untouched rows (the core correctness risk) ─

def test_incremental_save_keeps_untouched_rows(tag_db, tmp_path):
    root = str(tmp_path)
    cache = tr.load_cache(root)
    tr.ensure_cache_entry(cache, root, os.path.join(root, "A.jpg"))
    tr.ensure_cache_entry(cache, root, os.path.join(root, "B.jpg"))
    tr.save_cache(cache, root)
    assert cache["_dirty"] == set()            # cleared after a successful save

    reload1 = tr.load_cache(root)
    assert set(reload1["files"]) == {"A.jpg", "B.jpg"}
    assert reload1["_index"]                   # index rebuilt on load

    # Touch ONLY A, save incrementally.
    tr.update_cache_entry(reload1, root, os.path.join(root, "A.jpg"),
                          os.path.join(root, "A_cat.jpg"), "processed")
    assert reload1["_dirty"] == {"A.jpg"}
    tr.save_cache(reload1, root)

    reload2 = tr.load_cache(root)
    assert set(reload2["files"]) == {"A.jpg", "B.jpg"}      # B was NOT dropped
    assert reload2["files"]["A.jpg"]["status"] == "processed"
    assert reload2["files"]["A.jpg"]["current_rel_path"] == "A_cat.jpg"
    assert reload2["files"]["B.jpg"]["status"] == "scanned"  # untouched


def test_clean_incremental_save_is_a_noop(tag_db, tmp_path):
    # A save with nothing dirty must not touch (let alone wipe) the stored rows.
    root = str(tmp_path)
    cache = tr.load_cache(root)
    tr.ensure_cache_entry(cache, root, os.path.join(root, "A.jpg"))
    tr.save_cache(cache, root)
    tr.save_cache(cache, root)                 # dirty is empty now
    assert "A.jpg" in tr.load_cache(root)["files"]


def test_full_rewrite_persists_even_without_dirty(tag_db, tmp_path):
    # Undo saves with full=True after mutating entries in place (nothing marked
    # dirty); everything in cache["files"] must still be written.
    root = str(tmp_path)
    cache = tr.load_cache(root)
    tr.ensure_cache_entry(cache, root, os.path.join(root, "A.jpg"))
    cache["_dirty"].clear()
    tr.save_cache(cache, root, full=True)
    assert "A.jpg" in tr.load_cache(root)["files"]
