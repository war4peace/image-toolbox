"""
Item 12: tag_and_rename is the one tool that mutates user files in place (rename +
EXIF + rotation + undo), yet had no dedicated test until the 0.4.5 work. Items
2/3/6 and the filename-growth fix added EXIF-format, encoding, lossless-save,
cache and original-name tests; this file fills the remaining gaps the review named:
the pure string logic (resolve_language / has_camera_default_name / _auto_condense
/ _sanitize_condensed), build_new_path collision handling, and the undo round-trip
that backs the "every change is recorded" promise.

The pure-string and build_new_path tests are stdlib-only. The undo round-trip
needs Pillow + piexif (it tags a real JPEG) and skips without them.
"""

import os
import re

import pytest

import tag_and_rename as tr


# ── resolve_language ─────────────────────────────────────────────────────────

def test_resolve_language_iso_code_case_insensitive():
    assert tr.resolve_language("RO") == "Romanian"
    assert tr.resolve_language("ro") == "Romanian"
    assert tr.resolve_language("  fr ") == "French"


def test_resolve_language_full_name_normalised():
    assert tr.resolve_language("romanian") == "Romanian"
    assert tr.resolve_language("FRENCH") == "French"


def test_resolve_language_unknown_is_title_cased():
    assert tr.resolve_language("klingon") == "Klingon"


# ── has_camera_default_name (suffix stripping + boundary) ────────────────────

def test_has_camera_default_name_strips_suffixes(monkeypatch):
    # Deterministic pattern set so the test is independent of config.json.
    monkeypatch.setattr(tr, "_compiled_patterns",
                        [re.compile(r"^IMG_\d+", re.IGNORECASE)])
    assert tr.has_camera_default_name("IMG_1234.jpg")
    assert tr.has_camera_default_name("IMG_1234_upscaled.jpg")   # _upscaled stripped
    assert tr.has_camera_default_name("IMG_1234(0).jpg")         # (N) duplicate suffix
    assert tr.has_camera_default_name("IMG_1234_upscaled(2).jpg")
    assert not tr.has_camera_default_name("Family_Reunion.jpg")  # human name
    assert not tr.has_camera_default_name("MyIMG_1234.jpg")      # must anchor at start


# ── _auto_condense ───────────────────────────────────────────────────────────

def test_auto_condense_capitalises_and_joins(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    assert tr._auto_condense("a small red car") == "A_Small_Red_Car"


def test_auto_condense_caps_word_count(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 3)
    assert tr._auto_condense("one two three four five") == "One_Two_Three"


def test_auto_condense_ignores_punctuation(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    assert tr._auto_condense("cat, sitting on a mat!") == "Cat_Sitting_On_A_Mat"


# ── _sanitize_condensed ──────────────────────────────────────────────────────

def test_sanitize_strips_diacritics(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    # "Pisică pe fereastră" -> ascii-folded, spaces to underscores.
    assert tr._sanitize_condensed("Pisică pe fereastră") == "Pisica_pe_fereastra"


def test_sanitize_removes_illegal_filename_chars(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    assert tr._sanitize_condensed('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_sanitize_collapses_separators(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    assert tr._sanitize_condensed("  red --  car  ") == "red_car"


def test_sanitize_empty_falls_back(monkeypatch):
    # The fallback guards against an EMPTY result (which would make an unnamed
    # file), i.e. input that is only whitespace or only Windows-illegal chars.
    # Ordinary punctuation like '!' is not illegal, so it is kept as-is.
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 5)
    assert tr._sanitize_condensed("///") == "Unknown_Image"     # all illegal -> empty
    assert tr._sanitize_condensed("   ") == "Unknown_Image"     # all whitespace
    assert tr._sanitize_condensed("") == "Unknown_Image"
    assert tr._sanitize_condensed("!!!") == "!!!"               # '!' is legal, kept


def test_sanitize_caps_word_count(monkeypatch):
    monkeypatch.setattr(tr, "CONDENSED_MAX_WORDS", 2)
    assert tr._sanitize_condensed("one two three four") == "one_two"


# ── build_new_path (collision handling) ──────────────────────────────────────

def test_build_new_path_basic(tmp_path):
    src = os.path.join(str(tmp_path), "IMG_1.jpg")
    assert tr.build_new_path(src, "Red_Car") == os.path.join(str(tmp_path), "IMG_1_Red_Car.jpg")


def test_build_new_path_uses_base_stem(tmp_path):
    # Re-tagging an already-renamed file rebuilds from the ORIGINAL stem, so the
    # description replaces the old one rather than stacking on it.
    src = os.path.join(str(tmp_path), "001_Old_Desc.jpg")
    got = tr.build_new_path(src, "New_Desc", base_stem="001")
    assert got == os.path.join(str(tmp_path), "001_New_Desc.jpg")


def test_build_new_path_appends_counter_on_collision(tmp_path):
    open(os.path.join(str(tmp_path), "IMG_1_Cat.jpg"), "w").close()
    open(os.path.join(str(tmp_path), "IMG_1_Cat_2.jpg"), "w").close()
    src = os.path.join(str(tmp_path), "IMG_1.jpg")
    got = tr.build_new_path(src, "Cat")
    assert got == os.path.join(str(tmp_path), "IMG_1_Cat_3.jpg")   # skipped 1 and 2


def test_build_new_path_own_name_is_not_a_collision(tmp_path):
    # A file that already carries the exact target name is not a collision with
    # itself (re-tagging can produce the same name), so no counter is appended.
    existing = os.path.join(str(tmp_path), "001_Cat.jpg")
    open(existing, "w").close()
    got = tr.build_new_path(existing, "Cat", base_stem="001")
    assert got == existing


# ── undo round-trip (the "every change is recorded" promise) ─────────────────

def test_undo_round_trip_restores_name_and_exif(tmp_path):
    pytest.importorskip("PIL.Image")
    pytest.importorskip("piexif")
    from PIL import Image

    root = str(tmp_path)
    start = os.path.join(root, "IMG_0001.jpg")
    Image.new("RGB", (40, 30), (90, 120, 160)).save(start, "JPEG", quality=95)

    # Snapshot pristine state (all tracked fields absent on a bare JPEG).
    cache = {"files": {}, "_index": {}, "_dirty": set()}
    tr.ensure_cache_entry(cache, root, start)
    assert tr.is_already_processed(start) is False

    # Tag exactly as the runner does: write EXIF, rename from the original stem.
    original_name = tr.get_original_name(start, cache, root)
    tr.write_exif(start, "A calm blue square", original_name)
    new_path = tr.build_new_path(start, "A_Calm_Blue_Square",
                                 base_stem=os.path.splitext(original_name)[0])
    os.rename(start, new_path)
    tr.update_cache_entry(cache, root, start, new_path, "processed")

    assert os.path.basename(new_path) == "IMG_0001_A_Calm_Blue_Square.jpg"
    assert tr.is_already_processed(new_path) is True         # marker written

    # Undo both name and EXIF.
    _key, entry = tr._find_entry(cache, root, new_path)
    ok, _msg = tr._undo_entry(entry, root, undo_names=True, undo_exif=True)
    assert ok

    # Name restored, added EXIF (incl. the processed marker) stripped back to bare.
    assert os.path.exists(start)
    assert not os.path.exists(new_path)
    assert tr.is_already_processed(start) is False           # re-taggable again
    assert entry["status"] == "undone"
    assert entry["was_renamed"] is False


def test_undo_names_only_leaves_exif(tmp_path):
    pytest.importorskip("PIL.Image")
    pytest.importorskip("piexif")
    from PIL import Image

    root = str(tmp_path)
    start = os.path.join(root, "IMG_0002.jpg")
    Image.new("RGB", (40, 30), (10, 20, 30)).save(start, "JPEG", quality=95)

    cache = {"files": {}, "_index": {}, "_dirty": set()}
    tr.ensure_cache_entry(cache, root, start)
    tr.write_exif(start, "Something", "IMG_0002.jpg")
    new_path = tr.build_new_path(start, "Something", base_stem="IMG_0002")
    os.rename(start, new_path)
    tr.update_cache_entry(cache, root, start, new_path, "processed")

    _key, entry = tr._find_entry(cache, root, new_path)
    ok, _msg = tr._undo_entry(entry, root, undo_names=True, undo_exif=False)
    assert ok
    assert os.path.exists(start)
    # EXIF untouched by a names-only undo: the marker is still present.
    assert tr.is_already_processed(start) is True
