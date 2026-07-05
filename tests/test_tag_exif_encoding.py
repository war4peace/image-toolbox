"""
Regression tests for item 3: non-English EXIF descriptions must survive.

Before the fix, write_exif stored the description as
`long_description.encode("ascii", "replace")`, so with --language:RO (or any
non-ASCII language) every diacritic became "?": "Pisică pe acoperiș" was
persisted as "Pisic? pe acoperi?". The fix writes UTF-8 into ImageDescription
and mirrors the text into XPTitle (UTF-16LE, the Windows-native Unicode field).

Also pins the undo-snapshot fix: piexif returns the XP* tags as int TUPLES, which
base64.b64encode can't handle — so a snapshot reaching an XP field used to abort
mid-loop and silently drop the fields after it. With two XP tags now written
(XPTitle + XPComment), a snapshot must capture every tracked field.

Needs Pillow + piexif; skips cleanly where they're absent.
"""

import pytest

pytest.importorskip("PIL.Image")
pytest.importorskip("piexif")

from PIL import Image                     # noqa: E402
import piexif                             # noqa: E402
import tag_and_rename as tr               # noqa: E402

# Romanian: "A cat on the roof" — the flagship non-ASCII case from the report.
ROMANIAN = "Pisică pe acoperiș"


def _jpeg(tmp_path, name="photo.jpg"):
    p = str(tmp_path / name)
    Image.new("RGB", (32, 24), (200, 120, 60)).save(p, "JPEG")
    return p


def _read_xp(exif_0th, tag):
    raw = exif_0th.get(tag)
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)):     # piexif returns XP tags as int tuples
        raw = bytes(raw)
    return raw.decode("utf-16-le", "ignore").rstrip("\x00")


def test_description_preserves_diacritics(tmp_path):
    p = _jpeg(tmp_path)
    assert tr.write_exif(p, ROMANIAN, "photo.jpg") is True

    exif = piexif.load(p)
    desc = exif["0th"][piexif.ImageIFD.ImageDescription].decode("utf-8")
    assert desc == ROMANIAN            # UTF-8, diacritics intact
    assert "?" not in desc             # the specific old failure mode

    # XPTitle (40091): the Windows "Title" field, same accented text.
    assert _read_xp(exif["0th"], 40091) == ROMANIAN


def test_xpcomment_still_holds_original_name(tmp_path):
    p = _jpeg(tmp_path)
    tr.write_exif(p, ROMANIAN, "IMG_1234.jpg")
    exif = piexif.load(p)
    assert _read_xp(exif["0th"], 40092) == "IMG_1234.jpg"


def test_snapshot_captures_every_tracked_field(tmp_path):
    # The tuple-abort fix: with XPTitle + XPComment (both int tuples on reload),
    # the snapshot must still capture ImageDescription and UserComment that follow.
    p = _jpeg(tmp_path)
    tr.write_exif(p, ROMANIAN, "photo.jpg")
    tr.write_processed_marker(p)

    snap = tr._snapshot_exif(p)
    for field in ("ImageDescription", "XPTitle", "XPComment", "UserComment"):
        assert snap[field] is not None, f"{field} was dropped from the snapshot"


def test_undo_restores_original_state(tmp_path):
    # A fresh JPEG has none of the tracked fields; after tagging, undo (restore to
    # the all-None original snapshot) must strip every field we added.
    p = _jpeg(tmp_path)
    original = tr._snapshot_exif(p)
    assert all(v is None for v in original.values())

    tr.write_exif(p, ROMANIAN, "photo.jpg")
    tr.write_processed_marker(p)
    assert tr.is_already_processed(p) is True

    ok, changed = tr._restore_exif_fields(p, original)
    assert ok and changed
    assert tr.is_already_processed(p) is False       # marker removed
    after = piexif.load(p)
    assert piexif.ImageIFD.ImageDescription not in after["0th"]
    assert 40091 not in after["0th"]                 # XPTitle removed
