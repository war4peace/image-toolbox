"""
Roadmap #13: carry the original's metadata into the upscaled copy.

Two halves, and both are tested here because they share one module:

  13a  the upscale engine writes the source's EXIF onto its output instead of a
       metadata-free image (`exif_copy.exif_for_upscaled`, used by
       `upscale_engine._save_image`).
  13b  Conciliation repairs an ALREADY-upscaled file from its original, at the
       one moment the app holds both (`exif_copy.backfill`).

The correctness traps these pin down, in order of how quietly they would break
things: Orientation must NOT be copied verbatim (the pipeline already applied it,
so a copied value rotates an upright photo a second time); the stale embedded
thumbnail must not survive; a TIFF source's structural tags must not be smuggled
into a JPEG; a backfill must never overwrite a field the processed file already
has (Tag & Rename's description is the one that would hurt); and nothing here may
raise, because 13a runs inside an upscale and 13b runs one statement before an
archive or a delete.

Needs Pillow; the JPEG backfill also needs piexif. Skips cleanly without them.
"""

import os

import pytest

pytest.importorskip("PIL")

from PIL import Image                                       # noqa: E402

import exif_copy as ec                                      # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

_MAKE, _MODEL, _ORIENT, _COPYRIGHT, _DESC = 271, 272, 274, 33432, 270
_DATE, _LENS, _PX_X, _PX_Y, _MAKERNOTE = 36867, 42036, 40962, 40963, 37500
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825


def _rich_exif(orientation=6, makernote=b"MK" * 20):
    """A camera-shaped EXIF block, built with piexif so the rationals and the GPS
    triples are encoded the way a real camera writes them."""
    piexif = pytest.importorskip("piexif")
    return piexif.dump({
        "0th": {_MAKE: b"Canon", _MODEL: b"EOS 5D", _ORIENT: orientation,
                305: b"cam-fw", _COPYRIGHT: b"(c) me"},
        "Exif": {_DATE: b"2005:01:02 03:04:05", _LENS: b"EF 50mm",
                 _PX_X: 40, _PX_Y: 30, _MAKERNOTE: makernote},
        "GPS": {1: b"N", 2: ((44, 1), (25, 1), (0, 1))},
        "1st": {}, "thumbnail": None})


def _source(path, fmt=None, **kw):
    blob = _rich_exif(**kw)
    img = Image.new("RGB", (40, 30), (1, 2, 3))
    img.save(str(path), fmt, exif=blob) if fmt else img.save(str(path), exif=blob)
    return str(path)


def _plain(path, fmt=None, size=(160, 120)):
    img = Image.new("RGB", size, (9, 9, 9))
    img.save(str(path), fmt) if fmt else img.save(str(path))
    return str(path)


def _tags(path):
    with Image.open(path) as im:
        ex = im.getexif()
        return dict(ex), dict(ex.get_ifd(_EXIF_IFD)), dict(ex.get_ifd(_GPS_IFD))


class _FakeFrames:
    """The [T, H, W, C] tensor `_save_image` expects, without importing torch.

    Deliberate: torch in sys.modules breaks test_import_smoke's "no module pulled
    in the GPU stack eagerly" check for every test that runs after this file, and
    the save path only ever calls tensor[0].clamp().numpy() anyway.
    """

    class _Frame:
        def __init__(self, arr):
            self._arr = arr

        def clamp(self, _lo, _hi):
            return self

        def numpy(self):
            return self._arr

    def __init__(self, w, h):
        import numpy as np
        self._frame = self._Frame(np.zeros((h, w, 3), dtype="float32"))

    def __getitem__(self, _i):
        return self._frame


def _write_with(dest, blob, fmt=None):
    img = Image.new("RGB", (160, 120))
    kw = ec.save_kwargs(dest, blob)
    img.save(dest, fmt, **kw) if fmt else img.save(dest, **kw)
    return dest


# ── 13a: the block written onto a new upscaled file ──────────────────────────

@pytest.mark.parametrize("name, fmt", [("s.jpg", "jpeg"), ("s.webp", "webp"),
                                       ("s.png", None), ("s.tif", None)])
def test_metadata_is_read_from_every_source_format(tmp_path, name, fmt):
    """One code path, not a per-format table: Pillow reads a metadata block out of
    all four containers, so all four sources yield the same fields."""
    src = _source(tmp_path / name, fmt)
    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest, size=(160, 120)), "jpeg")

    zeroth, sub, gps = _tags(dest)
    assert zeroth[_MAKE] == "Canon" and zeroth[_MODEL] == "EOS 5D"
    assert zeroth[_COPYRIGHT] == "(c) me"
    assert sub[_DATE] == "2005:01:02 03:04:05"
    assert sub[_LENS] == "EF 50mm"
    assert gps                                   # GPS sub-IFD survived


def test_orientation_is_forced_upright(tmp_path):
    """THE trap. `_load_image` runs exif_exif_transpose and auto-straighten may have
    rotated a temp copy, so the output pixels are already upright; copying the
    source's Orientation=6 would make every viewer rotate them again."""
    src = _source(tmp_path / "s.jpg", "jpeg", orientation=6)
    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest, size=(160, 120)), "jpeg")
    assert _tags(dest)[0][_ORIENT] == 1


def test_pixel_dimensions_describe_the_upscaled_image(tmp_path):
    src = _source(tmp_path / "s.jpg", "jpeg")           # source is 40x30
    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest, size=(160, 120)), "jpeg")
    _z, sub, _g = _tags(dest)
    assert (sub[_PX_X], sub[_PX_Y]) == (160, 120)


def test_pixel_dimensions_are_dropped_when_the_size_is_unknown(tmp_path):
    """Absent is honest; the original's numbers would be a lie."""
    src = _source(tmp_path / "s.jpg", "jpeg")
    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest), "jpeg")
    _z, sub, _g = _tags(dest)
    assert _PX_X not in sub and _PX_Y not in sub


def test_the_stale_thumbnail_does_not_survive(tmp_path):
    """The embedded thumbnail shows the OLD image at the OLD size. Pillow's
    tobytes() does not serialise IFD1 at all, which is the mechanism; this pins it
    so an implementation change cannot quietly reintroduce a wrong thumbnail."""
    piexif = pytest.importorskip("piexif")
    import io
    buf = io.BytesIO()
    Image.new("RGB", (16, 12), (7, 7, 7)).save(buf, "jpeg")
    blob = piexif.dump({"0th": {_MAKE: b"Canon"}, "Exif": {}, "GPS": {},
                        "1st": {259: 6}, "thumbnail": buf.getvalue()})
    src = str(tmp_path / "s.jpg")
    Image.new("RGB", (40, 30)).save(src, "jpeg", exif=blob)
    assert piexif.load(src)["thumbnail"]              # the fixture really has one

    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest, size=(160, 120)), "jpeg")
    assert not piexif.load(dest)["thumbnail"]


def test_a_tiff_source_does_not_smuggle_its_layout_tags_into_a_jpeg(tmp_path):
    """A TIFF's 0th IFD carries StripOffsets, RowsPerStrip and friends. Copied
    verbatim into a JPEG they describe a strip layout that does not exist there."""
    src = _source(tmp_path / "s.tif")
    with Image.open(src) as im:
        assert 273 in dict(im.getexif())             # the fixture really has them

    dest = str(tmp_path / "out.jpg")
    _write_with(dest, ec.exif_for_upscaled(src, dest, size=(160, 120)), "jpeg")
    zeroth = _tags(dest)[0]
    assert not (set(zeroth) & ec._STRUCTURAL)
    assert zeroth[_MAKE] == "Canon"                  # the real metadata still came


def test_a_source_with_no_metadata_produces_none(tmp_path):
    """Not an empty-ish block. A three-tag block saying only "upright, 3840x2160"
    reads as metadata and is not."""
    src = _plain(tmp_path / "plain.jpg", "jpeg", size=(40, 30))
    assert ec.exif_for_upscaled(src, str(tmp_path / "o.jpg"), size=(160, 120)) is None


def test_an_unreadable_or_missing_source_is_not_an_error(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    assert ec.exif_for_upscaled(str(broken), str(tmp_path / "o.jpg")) is None
    assert ec.exif_for_upscaled(str(tmp_path / "gone.jpg"), str(tmp_path / "o.jpg")) is None


def test_an_oversized_makernote_costs_only_the_makernote(tmp_path):
    """A JPEG's APP1 segment caps at 64 KB and Pillow raises rather than
    truncating, so an over-large camera MakerNote used to be able to fail the save.
    It is dropped first because it is also the one part that cannot survive being
    moved into another file (its offsets are file-relative)."""
    src = _source(tmp_path / "s.webp", "webp", makernote=b"X" * 100000)
    dest = str(tmp_path / "out.jpg")
    blob = ec.exif_for_upscaled(src, dest, size=(160, 120))
    assert blob is not None and len(blob) < ec._JPEG_EXIF_LIMIT
    _write_with(dest, blob, "jpeg")                  # must not raise
    _z, sub, _g = _tags(dest)
    assert sub[_DATE] == "2005:01:02 03:04:05"       # the useful fields survived
    assert _MAKERNOTE not in sub


def test_a_big_makernote_is_kept_where_the_format_allows_it(tmp_path):
    """The shrink is a JPEG rule, not a global one: WebP and PNG have no 64 KB
    segment limit, so nothing is dropped for them."""
    src = _source(tmp_path / "s.webp", "webp", makernote=b"X" * 100000)
    dest = str(tmp_path / "out.webp")
    blob = ec.exif_for_upscaled(src, dest, size=(160, 120))
    assert len(blob) > ec._JPEG_EXIF_LIMIT


def test_save_kwargs_stays_silent_for_a_format_that_cannot_carry_metadata(tmp_path):
    """Pillow accepts `exif=` for BMP and writes nothing, so claiming to support it
    would report a copy that did not happen."""
    src = _source(tmp_path / "s.jpg", "jpeg")
    blob = ec.exif_for_upscaled(src, str(tmp_path / "o.jpg"), size=(160, 120))
    assert ec.save_kwargs(str(tmp_path / "o.bmp"), blob) == {}
    assert ec.save_kwargs(str(tmp_path / "o.jpg"), blob) == {"exif": blob}
    assert ec.save_kwargs(str(tmp_path / "o.jpg"), None) == {}


# ── 13a: the engine's save path ──────────────────────────────────────────────

def test_the_engine_saves_the_source_metadata(tmp_path):
    """_save_image is a staticmethod on purpose, so it can be exercised without a
    GPU or a SeedVR2 install."""
    pytest.importorskip("numpy")
    from upscale_engine import UpscaleEngine

    src = _source(tmp_path / "s.jpg", "jpeg")
    dest = str(tmp_path / "out.jpg")
    tensor = _FakeFrames(160, 120)

    UpscaleEngine._save_image(tensor, dest, src)
    zeroth, sub, _gps = _tags(dest)
    assert zeroth[_MAKE] == "Canon" and zeroth[_ORIENT] == 1
    assert sub[_DATE] == "2005:01:02 03:04:05"
    assert (sub[_PX_X], sub[_PX_Y]) == (160, 120)    # taken from the written array


def test_the_engine_writes_nothing_when_metadata_is_turned_off(tmp_path):
    pytest.importorskip("numpy")
    from upscale_engine import UpscaleEngine

    _source(tmp_path / "s.jpg", "jpeg")               # a rich source exists, unused
    dest = str(tmp_path / "out.jpg")
    tensor = _FakeFrames(160, 120)

    UpscaleEngine._save_image(tensor, dest, None)     # what copy_metadata=False sends
    assert _tags(dest)[0] == {}


def test_a_metadata_failure_never_costs_the_image(tmp_path, monkeypatch):
    """The image is the product, the metadata is a bonus. An EXIF block the encoder
    rejects must fall back to writing the file with none."""
    pytest.importorskip("numpy")
    from upscale_engine import UpscaleEngine

    src = _source(tmp_path / "s.jpg", "jpeg")
    dest = str(tmp_path / "out.jpg")
    tensor = _FakeFrames(160, 120)
    # A block far past the APP1 limit, sneaked past the size guard.
    monkeypatch.setattr(ec, "exif_for_upscaled",
                        lambda *_a, **_k: b"Exif\x00\x00" + b"X" * 200000)

    UpscaleEngine._save_image(tensor, dest, src)
    assert os.path.isfile(dest)
    with Image.open(dest) as im:
        assert im.size == (160, 120)


# ── 13b: the retroactive backfill ────────────────────────────────────────────

def test_backfill_fills_a_metadata_free_upscale(tmp_path):
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    assert ec.pending_backfill(orig, proc) > 0

    added, reason = ec.backfill(orig, proc)
    assert added > 0 and reason is None
    zeroth, sub, gps = _tags(proc)
    assert zeroth[_MAKE] == "Canon" and sub[_DATE] == "2005:01:02 03:04:05" and gps


def test_backfill_is_lossless_on_a_jpeg(tmp_path):
    """Conciliation writes into a file that is about to become the only copy, so
    the compressed scan data has to come out byte-for-byte identical."""
    pytest.importorskip("piexif")
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    before = open(proc, "rb").read()

    ec.backfill(orig, proc)
    after = open(proc, "rb").read()
    assert before[before.index(b"\xff\xda"):] == after[after.index(b"\xff\xda"):]


def test_backfill_never_overwrites_what_the_processed_file_already_has(tmp_path):
    """"Copy what is missing, keep what is present" is the whole policy. The field
    that would hurt is the description Tag & Rename wrote: the source still has the
    older one."""
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = str(tmp_path / "p.jpg")
    with Image.open(orig) as im:
        keep = im.getexif()
    kept = Image.new("RGB", (160, 120))
    ex = kept.getexif()
    ex[_DESC] = "a cat on a windowsill"
    kept.save(proc, "jpeg", exif=ex.tobytes())
    assert keep is not None

    ec.backfill(orig, proc)
    zeroth = _tags(proc)[0]
    assert zeroth[_DESC] == "a cat on a windowsill"
    assert zeroth[_MAKE] == "Canon"                  # the missing fields still came


def test_backfill_excludes_orientation_and_pixel_dimensions(tmp_path):
    """On a metadata-free upscale both are "missing", so the general rule would
    happily copy them - and both would then be wrong (a second rotation, and the
    small original's dimensions)."""
    orig = _source(tmp_path / "o.jpg", "jpeg", orientation=6)
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    ec.backfill(orig, proc)
    zeroth, sub, _gps = _tags(proc)
    assert _ORIENT not in zeroth
    assert _PX_X not in sub and _PX_Y not in sub


def test_backfill_is_idempotent(tmp_path):
    """Re-running Conciliation finds nothing missing. The same holds once 13a has
    shipped: those outputs already carry their metadata, so this quietly becomes a
    no-op and only the older backlog is touched."""
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    assert ec.backfill(orig, proc)[0] > 0
    assert ec.backfill(orig, proc) == (0, None)
    assert ec.pending_backfill(orig, proc) == 0


def test_backfill_on_a_png_keeps_the_pixels(tmp_path):
    """PNG has no in-place EXIF patch, so it is re-saved whole - which costs
    nothing because the format is lossless."""
    orig = _source(tmp_path / "o.png")
    proc = _plain(tmp_path / "p.png")
    with Image.open(proc) as im:
        before = im.tobytes()

    added, reason = ec.backfill(orig, proc)
    assert added > 0 and reason is None
    with Image.open(proc) as im:
        assert im.tobytes() == before
    assert _tags(proc)[0][_MAKE] == "Canon"


@pytest.mark.parametrize("name, fmt", [("p.webp", "webp"), ("p.tif", None),
                                       ("p.bmp", None)])
def test_a_lossy_or_awkward_format_is_refused_with_a_reason(tmp_path, name, fmt):
    """Spending a generation of WebP quality to add a capture date is a bad trade,
    and re-saving a TIFF through Pillow can change its compression. Named, not
    silently skipped."""
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = _plain(tmp_path / name, fmt)
    added, reason = ec.backfill(orig, proc)
    assert added == 0 and "re-encoding" in reason
    assert ec.pending_backfill(orig, proc) == 0      # preview and run agree


def test_backfill_reports_rather_than_raises_on_an_unreadable_file(tmp_path):
    """It runs one statement before an archive or a delete; it may not raise."""
    orig = _source(tmp_path / "o.jpg", "jpeg")
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    assert ec.backfill(orig, str(broken)) == (0, "unreadable")
    assert ec.backfill(str(tmp_path / "gone.jpg"), str(broken)) == (0, "unreadable")


def test_backfill_from_a_source_with_no_metadata_does_nothing(tmp_path):
    orig = _plain(tmp_path / "o.jpg", "jpeg", size=(40, 30))
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    assert ec.backfill(orig, proc) == (0, None)
    assert ec.pending_backfill(orig, proc) == 0


def test_pending_backfill_writes_nothing(tmp_path):
    """Scan/Preview promises to touch nothing, and the count is produced during it."""
    orig = _source(tmp_path / "o.jpg", "jpeg")
    proc = _plain(tmp_path / "p.jpg", "jpeg")
    before = open(proc, "rb").read()
    mtime = os.path.getmtime(proc)
    assert ec.pending_backfill(orig, proc) > 0
    assert open(proc, "rb").read() == before
    assert os.path.getmtime(proc) == mtime


# ── 13b inside Conciliation ──────────────────────────────────────────────────

class _FakeLog:
    def __init__(self):
        self.lines = []

    def tee(self, msg=""):
        self.lines.append(msg)

    def log_only(self, msg):
        self.lines.append(msg)


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """A throwaway cache.db, so a test never writes the user's real one."""
    import db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def test_conciliation_restores_metadata_before_the_original_goes_away(db_conn, tmp_path):
    """The whole point of putting this in Conciliation: in delete mode the original
    stops existing one statement later, so this is the last possible moment."""
    import conciliate as cc
    orig_root, proc_root = tmp_path / "orig", tmp_path / "proc"
    orig_root.mkdir(); proc_root.mkdir()
    _source(orig_root / "photo.jpg", "jpeg")
    _plain(proc_root / "photo.jpg", "jpeg")

    plan, _f, _k, *_ = cc.build_plan(str(orig_root), str(proc_root),
                                     tr_index=None, conn=db_conn)
    assert len(plan) == 1
    assert cc.count_pending_metadata(plan) == 1

    log = _FakeLog()
    done, conflicts, errors, restored = cc.execute(plan, str(orig_root), "delete", log)
    assert (done, conflicts, errors, restored) == (1, 0, 0, 1)

    moved_in = str(orig_root / "photo.jpg")
    zeroth, sub, _gps = _tags(moved_in)
    assert zeroth[_MAKE] == "Canon"
    assert sub[_DATE] == "2005:01:02 03:04:05"


def test_conciliation_metadata_preview_writes_nothing(db_conn, tmp_path):
    """Scan/Preview reports the count and must still touch nothing."""
    import conciliate as cc
    orig_root, proc_root = tmp_path / "orig", tmp_path / "proc"
    orig_root.mkdir(); proc_root.mkdir()
    _source(orig_root / "photo.jpg", "jpeg")
    proc = _plain(proc_root / "photo.jpg", "jpeg")
    before = open(proc, "rb").read()

    plan, _f, _k, *_ = cc.build_plan(str(orig_root), str(proc_root),
                                     tr_index=None, conn=db_conn)
    assert cc.count_pending_metadata(plan) == 1
    assert open(proc, "rb").read() == before


def test_conciliation_carries_on_when_the_metadata_pass_fails(db_conn, tmp_path,
                                                              monkeypatch):
    """A bonus pass must never abort the file operation it precedes."""
    import conciliate as cc
    orig_root, proc_root = tmp_path / "orig", tmp_path / "proc"
    orig_root.mkdir(); proc_root.mkdir()
    _source(orig_root / "photo.jpg", "jpeg")
    _plain(proc_root / "photo.jpg", "jpeg")

    def _boom(*_a, **_k):
        raise RuntimeError("piexif exploded")

    monkeypatch.setattr(cc.exif_copy, "backfill", _boom)
    plan, _f, _k, *_ = cc.build_plan(str(orig_root), str(proc_root),
                                     tr_index=None, conn=db_conn)
    done, conflicts, errors, restored = cc.execute(plan, str(orig_root), "archive",
                                                   _FakeLog())
    assert (done, conflicts, errors, restored) == (1, 0, 0, 0)
    assert os.path.isfile(str(orig_root / cc.ARCHIVE_DIRNAME / "photo.jpg"))


def test_conciliation_skips_the_metadata_pass_when_it_is_turned_off(db_conn, tmp_path,
                                                                    monkeypatch):
    import conciliate as cc
    orig_root, proc_root = tmp_path / "orig", tmp_path / "proc"
    orig_root.mkdir(); proc_root.mkdir()
    _source(orig_root / "photo.jpg", "jpeg")
    _plain(proc_root / "photo.jpg", "jpeg")
    monkeypatch.setattr(cc, "COPY_METADATA", False)

    plan, _f, _k, *_ = cc.build_plan(str(orig_root), str(proc_root),
                                     tr_index=None, conn=db_conn)
    assert cc.count_pending_metadata(plan) == 0
    _d, _c, _e, restored = cc.execute(plan, str(orig_root), "archive", _FakeLog())
    assert restored == 0
    assert _tags(str(orig_root / "photo.jpg"))[0] == {}


def test_a_video_pair_never_reaches_the_image_backfill(tmp_path):
    """Container-level video metadata is an ffmpeg job with its own rules. Gated on
    the extension so Pillow is never asked to open an .mp4, which would report every
    video as 'unreadable'."""
    import conciliate as cc
    assert not cc._is_image_pair(str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4"))
    assert not cc._is_image_pair(str(tmp_path / "a.avi"), str(tmp_path / "b_4K.mp4"))
    assert cc._is_image_pair(str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg"))


# ── the shared setting ───────────────────────────────────────────────────────

def test_both_halves_default_to_copying(tmp_path):
    """13a and 13b share ONE setting, and both must default to on: keeping the
    capture date is what a user expects, and after Conciliation replaces the
    original there is no second chance."""
    import inspect
    import batch_upscale
    import conciliate
    import upscale_engine

    for mod in (batch_upscale, conciliate, upscale_engine):
        src = inspect.getsource(mod)
        assert 'copy_metadata", True' in src, mod.__name__
    assert batch_upscale.COPY_METADATA is True
    assert conciliate.COPY_METADATA is True
    assert inspect.isfunction(ec.backfill)
