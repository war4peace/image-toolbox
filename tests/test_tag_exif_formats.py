"""
Regression tests for item 2: Tag & Rename must never write JPEG bytes into a
non-JPEG file (data corruption) or crash on an RGBA PNG.

Before the fix, every EXIF write went through `_save_with_exif` which did
`img.save(path, "jpeg", ...)` unconditionally, while the tool accepts
.png/.webp/.tiff — so a PNG got JPEG bytes under its .png extension, or the save
raised on an RGBA image. The upscaler legitimately produces PNG/TIFF, so this is
hit by the upscale -> tag workflow directly.

The fix embeds EXIF only for JPEG; other formats are left byte-for-byte untouched
(their description lives in the filename and their skip-on-rerun marker in the
cache). These tests need Pillow + piexif; they skip cleanly where those are
absent (the CI import-smoke environment has no Pillow).
"""

import os

import pytest

pytest.importorskip("PIL.Image")
pytest.importorskip("piexif")

from PIL import Image                     # noqa: E402
import tag_and_rename as tr               # noqa: E402


def _make(path, fmt, mode="RGB"):
    color = (200, 120, 60, 255)[: len(mode)]
    Image.new(mode, (32, 24), color).save(path, fmt)


# ── the core data-integrity guarantee ────────────────────────────────────────

def test_write_exif_leaves_png_byte_for_byte_untouched(tmp_path):
    p = str(tmp_path / "photo.png")
    _make(p, "PNG", "RGBA")            # RGBA is the case that used to RAISE
    before = open(p, "rb").read()

    written = tr.write_exif(p, "Pisica pe acoperis", "photo.png")

    assert written is False            # EXIF skipped for a non-JPEG
    after = open(p, "rb").read()
    assert after == before             # not re-encoded, not corrupted
    assert after[:8] == b"\x89PNG\r\n\x1a\n"   # still a real PNG


@pytest.mark.parametrize("fmt,ext,mode", [
    ("PNG", ".png", "RGBA"),
    ("PNG", ".png", "RGB"),
    ("WEBP", ".webp", "RGB"),
    ("TIFF", ".tif", "RGB"),
])
def test_non_jpeg_is_never_rewritten(tmp_path, fmt, ext, mode):
    p = str(tmp_path / ("img" + ext))
    _make(p, fmt, mode)
    before = open(p, "rb").read()
    assert tr.write_exif(p, "desc", "img" + ext) is False
    assert tr.write_processed_marker(p) is False
    assert open(p, "rb").read() == before


def test_save_with_exif_refuses_non_jpeg(tmp_path):
    # The hard guard: even a future caller that forgets the _exif_writable() gate
    # can't corrupt a PNG — the save refuses rather than writing JPEG bytes.
    p = str(tmp_path / "x.png")
    _make(p, "PNG")
    with pytest.raises(ValueError):
        tr._save_with_exif(p, {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}})


# ── JPEG still works exactly as before ────────────────────────────────────────

def test_jpeg_exif_is_written_and_round_trips(tmp_path):
    p = str(tmp_path / "photo.jpg")
    _make(p, "JPEG")

    assert tr.write_exif(p, "A cat on a roof", "photo.jpg") is True
    assert tr.write_processed_marker(p) is True

    assert open(p, "rb").read()[:3] == b"\xff\xd8\xff"   # still JPEG
    assert tr.is_already_processed(p) is True             # marker round-trips


# ── the skip-on-rerun signal for non-JPEG comes from the cache ────────────────

def test_non_jpeg_skip_uses_cache(tmp_path):
    p = str(tmp_path / "photo.png")
    _make(p, "PNG")
    root = str(tmp_path)

    # No EXIF marker on a PNG, so the EXIF-only check must NOT skip it.
    assert tr.is_already_processed(p) is False

    # A prior run recorded it "processed" in the cache -> re-run skips it.
    rel = os.path.relpath(p, root)
    cache = {"files": {rel: {"original_rel_path": rel,
                             "current_rel_path": rel,
                             "status": "processed"}}}
    assert tr.is_already_processed(p, cache, root) is True

    # A not-yet-finished status does not skip.
    cache["files"][rel]["status"] = "scanned"
    assert tr.is_already_processed(p, cache, root) is False


def test_cache_skip_follows_a_rename(tmp_path):
    # After tagging, the file is renamed; the cache tracks current_rel_path, so the
    # skip must find it under the NEW name on the next run.
    renamed = str(tmp_path / "photo_A_cat.png")
    _make(renamed, "PNG")
    root = str(tmp_path)
    cache = {"files": {"photo.png": {"original_rel_path": "photo.png",
                                     "current_rel_path": "photo_A_cat.png",
                                     "status": "processed"}}}
    assert tr.is_already_processed(renamed, cache, root) is True
