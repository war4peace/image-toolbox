"""
Regression test for the "growing filename" bug: sequential Rename passes must
OVERWRITE the previous description, rebuilding from the original filename, never
append to the renamed result.

    001.jpg
    -> 001_Child_At_Window.jpg          (pass 1)
    -> 001_Child_At_Window_New...jpg    (BUG: appended)
    -> 001_New_Description.jpg          (FIXED: rebuilt from the original)

The original filename is the source of truth in the DB (entry.original_rel_path).
The subtle failure was a cache MISS (reset DB, or the same folder reached via a
different root path such as a mapped drive vs UNC): ensure_cache_entry seeded the
entry from the CURRENT, already-renamed name, and get_original_name's cache-first
lookup then short-circuited the EXIF fallback that would have recovered the true
original. The fix recovers the original from the file's own EXIF XPComment when
the cache is lost.

Needs Pillow + piexif (to write/read the XPComment record); skips without them.
"""

import os

import pytest

pytest.importorskip("PIL.Image")
pytest.importorskip("piexif")

from PIL import Image                     # noqa: E402
import tag_and_rename as tr               # noqa: E402


def _fresh_jpeg(path):
    Image.new("RGB", (48, 32), (170, 130, 90)).save(path, "JPEG")


def _rename_pass(cache, root, path, condensed):
    """The exact sequence the runner's per-image loop performs for a rename."""
    original_name = tr.get_original_name(path, cache, root)
    tr.write_exif(path, condensed.replace("_", " "), original_name)
    new_path = tr.build_new_path(path, condensed,
                                 base_stem=os.path.splitext(original_name)[0])
    os.rename(path, new_path)
    tr.write_processed_marker(new_path)
    tr.update_cache_entry(cache, root, path, new_path, "processed")
    return new_path


def test_repeated_renames_overwrite_with_persistent_cache(tmp_path):
    root = str(tmp_path)
    start = os.path.join(root, "001.jpg")
    _fresh_jpeg(start)

    cache = {"files": {}, "_index": {}, "_dirty": set()}
    tr.ensure_cache_entry(cache, root, start)

    p = start
    for desc, expected in [("Child_At_Window", "001_Child_At_Window.jpg"),
                           ("Looking_Outside", "001_Looking_Outside.jpg"),
                           ("Kid_By_Glass",    "001_Kid_By_Glass.jpg")]:
        p = _rename_pass(cache, root, p, desc)
        assert os.path.basename(p) == expected     # rebuilt from "001", not grown


def test_rename_recovers_original_after_cache_loss(tmp_path):
    # The reported bug: after the cache is gone, a re-rename must still rebuild
    # from the true original recovered from EXIF XPComment.
    root = str(tmp_path)
    start = os.path.join(root, "001.jpg")
    _fresh_jpeg(start)

    cache = {"files": {}, "_index": {}, "_dirty": set()}
    tr.ensure_cache_entry(cache, root, start)
    p = _rename_pass(cache, root, start, "Child_At_Window")
    assert os.path.basename(p) == "001_Child_At_Window.jpg"

    # Cache LOST: brand-new empty cache, re-scan the renamed file.
    lost = {"files": {}, "_index": {}, "_dirty": set()}
    key = tr.ensure_cache_entry(lost, root, p)
    assert key == "001.jpg"                                   # original recovered
    assert lost["files"]["001.jpg"]["current_rel_path"] == "001_Child_At_Window.jpg"
    assert lost["files"]["001.jpg"]["was_renamed"] is True
    assert tr.get_original_name(p, lost, root) == "001.jpg"

    p2 = _rename_pass(lost, root, p, "New_Description")
    assert os.path.basename(p2) == "001_New_Description.jpg"  # overwrite, not append
    assert tr._recorded_original_name(p2) == "001.jpg"        # XPComment not poisoned


def test_recovery_does_not_clobber_an_existing_entry(tmp_path):
    # Two files whose XPComment both name "001.jpg" must not collapse onto one key.
    root = str(tmp_path)
    a = os.path.join(root, "001_First.jpg")
    b = os.path.join(root, "001_Second.jpg")
    _fresh_jpeg(a)
    _fresh_jpeg(b)
    tr.write_exif(a, "first", "001.jpg")
    tr.write_exif(b, "second", "001.jpg")

    cache = {"files": {}, "_index": {}, "_dirty": set()}
    ka = tr.ensure_cache_entry(cache, root, a)
    kb = tr.ensure_cache_entry(cache, root, b)
    assert ka == "001.jpg"          # first recovers the original key
    assert kb == "001_Second.jpg"   # second keeps its own name (no clobber)
    assert len(cache["files"]) == 2


def test_brand_new_untagged_file_is_its_own_original(tmp_path):
    root = str(tmp_path)
    f = os.path.join(root, "holiday.jpg")
    _fresh_jpeg(f)                              # no XPComment yet
    cache = {"files": {}, "_index": {}, "_dirty": set()}
    key = tr.ensure_cache_entry(cache, root, f)
    assert key == "holiday.jpg"
    assert cache["files"]["holiday.jpg"]["was_renamed"] is False
    assert tr.get_original_name(f, cache, root) == "holiday.jpg"
