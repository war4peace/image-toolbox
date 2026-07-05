"""
Item 6: a successful tag must write its EXIF in ONE lossless save.

Before, a tagged JPEG was re-encoded twice per image (write_exif for the
description, then write_processed_marker for the skip marker) — two generations
of JPEG loss and double the I/O. Now write_exif folds the marker into a single
_save_with_exif call, and that save uses piexif.insert() which patches only the
APP1/EXIF segment and never re-encodes the pixels.

Guarantees checked here:
  * a single write_exif() call leaves the file marked as processed (the merge
    happened — no separate write_processed_marker is needed);
  * all four fields (ImageDescription, XPTitle, XPComment, UserComment marker)
    are present after that one call;
  * the pixels are byte-for-byte identical before and after, and stay identical
    across repeated re-tags (truly lossless, no accumulation).

Needs Pillow + piexif; skips without them.
"""

import hashlib
import os

import pytest

pytest.importorskip("PIL.Image")
pytest.importorskip("piexif")

from PIL import Image                     # noqa: E402
import piexif                             # noqa: E402
import tag_and_rename as tr               # noqa: E402


def _fresh_jpeg(path):
    # A bare Pillow JPEG (no APP1) mimics ComfyUI SaveImage output — the case the
    # lossless insert() path has to handle, not just files that already have EXIF.
    Image.new("RGB", (80, 60), (140, 100, 60)).save(path, "JPEG", quality=95)


def _pixels(path):
    with Image.open(path) as im:
        return hashlib.sha1(im.convert("RGB").tobytes()).hexdigest()


def _b(v):  # piexif returns XP/UserComment as bytes or an int tuple
    return bytes(v) if isinstance(v, (tuple, list)) else v


def test_single_write_marks_processed_and_carries_all_fields(tmp_path):
    p = os.path.join(str(tmp_path), "001.jpg")
    _fresh_jpeg(p)

    assert tr.is_already_processed(p) is False       # pristine
    assert tr.write_exif(p, "Pisică pe fereastră", "001.jpg") is True

    # The merge: ONE write_exif is enough to set the skip-on-rerun marker.
    assert tr.is_already_processed(p) is True

    rd = piexif.load(p)
    assert _b(rd["0th"][piexif.ImageIFD.ImageDescription]).decode("utf-8") == "Pisică pe fereastră"
    assert _b(rd["0th"][40091]).decode("utf-16-le") == "Pisică pe fereastră"   # XPTitle
    assert _b(rd["0th"][40092]).decode("utf-16-le") == "001.jpg"               # XPComment
    marker = _b(rd["Exif"][piexif.ExifIFD.UserComment])[8:].decode("ascii", "ignore")
    assert marker.startswith(tr.PROCESSED_MARKER)


def test_tagging_is_pixel_lossless(tmp_path):
    p = os.path.join(str(tmp_path), "photo.jpg")
    _fresh_jpeg(p)
    before = _pixels(p)

    tr.write_exif(p, "A cat by the window", "photo.jpg")
    assert _pixels(p) == before          # one tag: no pixel change at all


def test_repeated_retag_never_degrades(tmp_path):
    p = os.path.join(str(tmp_path), "photo.jpg")
    _fresh_jpeg(p)
    tr.write_exif(p, "first", "photo.jpg")
    baseline = _pixels(p)

    for i in range(5):
        tr.write_exif(p, f"description {i}", "photo.jpg")
        assert _pixels(p) == baseline    # re-tagging keeps the pixels identical
