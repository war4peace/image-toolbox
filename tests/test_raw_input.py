"""
Roadmap #19: RAW and DNG input for the Batch Upscaler.

Three things are worth a test here, and they are not the same kind of thing.

1. **The trap.** A DNG/CR2/NEF/ARW is a TIFF/EP derivative that puts a small
   preview in IFD 0 by spec, and Pillow sniffs content rather than extension. So
   the dimension reader's usual Pillow fallback does not FAIL on a RAW, it
   answers confidently and wrongly - measured at 15 of 24 real camera files, and
   several of those plausible photo sizes rather than obvious thumbnails. The
   rule that dissolves it is "a RAW extension never reaches Pillow", and it is
   the one thing here whose failure is silent and produces garbage in bulk.

2. **Render-always.** Measured on the same 24 files: at the shipped 4K target and
   66% cutoff, ZERO would ever have been upscaled, because RAW is by nature a
   high-resolution format and this app targets low-resolution photos. So a RAW is
   exempt from the size skip and always produces a viewable JPEG; the size check
   only chooses render-only vs render-then-upscale.

3. **The naming rule**, which exists solely so that shooting RAW+JPEG - two
   sources, one stem - cannot map both to one output file.

The pure helpers are tested directly. The parts that need a real camera file are
covered by docs/raw-preview-survey.csv rather than by fixtures: a RAW cannot be
synthesised, and checking in 400 MB of them to test a tuple of extensions would
be a poor trade.
"""

import os

import pytest

pytest.importorskip("PIL")

from PIL import Image                                       # noqa: E402

import raw_decode                                           # noqa: E402
import runner_common as rc                                  # noqa: E402
import exif_copy                                            # noqa: E402
import batch_upscale as bu                                  # noqa: E402


# ─────────────────────────────────────────────
#  The pure geometry helpers
# ─────────────────────────────────────────────

def test_upright_size_swaps_only_for_the_two_transposing_flips():
    # LibRaw reports iwidth/iheight BEFORE `flip` is applied. Verified against
    # rawpy: postprocess(user_flip=5|6) swaps the output shape, 0|3 do not.
    assert raw_decode.upright_size(3000, 2000, 0) == (3000, 2000)
    assert raw_decode.upright_size(3000, 2000, 3) == (3000, 2000)   # 180 degrees
    assert raw_decode.upright_size(3000, 2000, 5) == (2000, 3000)
    assert raw_decode.upright_size(3000, 2000, 6) == (2000, 3000)


def test_a_full_size_preview_is_recognised_through_the_border_crop():
    # Every camera in the survey crops a few pixels off its preview: a 3900x2611
    # NEF previews at 3872x2592. That must still count as full size.
    assert raw_decode.preview_is_full_size(3872, 2592, 3900, 2611)
    assert raw_decode.preview_is_full_size(8192, 5464, 8191, 5463)


def test_a_reduced_preview_is_never_mistaken_for_a_full_one():
    # The real reduced previews from the survey: if any of these were accepted,
    # the app would upscale a thumbnail into a 4K file and never look wrong.
    assert not raw_decode.preview_is_full_size(1024, 683, 3900, 2611)   # D80 DNG
    assert not raw_decode.preview_is_full_size(1536, 1024, 3522, 2348)  # 20D
    assert not raw_decode.preview_is_full_size(2496, 1664, 4386, 2920)  # 5D
    assert not raw_decode.preview_is_full_size(672, 504, 4032, 3024)    # Pixel 3a


def test_a_rotated_full_size_preview_still_counts():
    # A preview may be stored pre- or post-rotation depending on the maker, and a
    # rotated full-size preview is still a full-size preview.
    assert raw_decode.preview_is_full_size(2592, 3872, 3900, 2611)


def test_nothing_is_full_size_against_a_zero_dimension():
    assert not raw_decode.preview_is_full_size(0, 0, 3900, 2611)
    assert not raw_decode.preview_is_full_size(3872, 2592, 0, 0)


# ─────────────────────────────────────────────
#  Trap 1: a RAW must never be measured by Pillow
# ─────────────────────────────────────────────

def _two_ifd_tiff(path, small=(256, 171), large=(6000, 4000)):
    """A TIFF whose FIRST page is a small preview and whose second is the real
    image - the structure every TIFF/EP raw uses, which is what makes Pillow's
    content sniffing answer with the preview's size."""
    pages = [Image.new("RGB", small, "red"), Image.new("RGB", large, "blue")]
    pages[0].save(path, "tiff", save_all=True, append_images=pages[1:])


def test_pillow_really_does_report_the_preview_size(tmp_path):
    # The premise of the whole trap. If this ever stops being true, the guard
    # below is still correct but this file should say so.
    p = tmp_path / "photo.tiff"
    _two_ifd_tiff(str(p))
    with Image.open(str(p)) as im:
        assert im.size == (256, 171)


def test_a_raw_extension_never_gets_pillows_confident_wrong_answer(tmp_path):
    # Same bytes, named .dng. The dimension reader must NOT hand back (256, 171):
    # that is the failure that would upscale a thumbnail into a 4K file, in bulk,
    # without ever looking wrong in a log.
    p = tmp_path / "photo.dng"
    _two_ifd_tiff(str(p))
    assert rc.get_image_dimensions(str(p)) != (256, 171)


def test_an_unreadable_raw_reports_unreadable_rather_than_guessing(tmp_path):
    # (0, 0) is the runners' "corrupted / unreadable" signal. For a RAW that is
    # the RIGHT answer and the fail-safe Pillow fallback is the wrong one - the
    # inversion this module exists to make.
    p = tmp_path / "photo.cr2"
    p.write_bytes(b"not a raw file at all")
    assert rc.get_image_dimensions(str(p)) == (0, 0)


def test_every_supported_raw_extension_is_routed_away_from_pillow(tmp_path):
    for ext in raw_decode.RAW_EXTS:
        p = tmp_path / f"photo{ext}"
        _two_ifd_tiff(str(p))
        assert rc.get_image_dimensions(str(p)) != (256, 171), ext


def test_ordinary_formats_are_untouched_by_the_raw_branch(tmp_path):
    p = tmp_path / "photo.jpg"
    Image.new("RGB", (640, 480), "green").save(str(p), "jpeg")
    assert rc.get_image_dimensions(str(p)) == (640, 480)


# ─────────────────────────────────────────────
#  Render-always
# ─────────────────────────────────────────────

def test_a_raw_is_never_skipped_for_being_too_large(tmp_path):
    # The measured reality: at the shipped target and cutoff, every real DSLR and
    # phone RAW is "too large". If the size check could veto them the feature
    # would produce nothing at all, for anyone.
    for ext in raw_decode.RAW_EXTS:
        p = tmp_path / f"huge{ext}"
        p.write_bytes(b"x")            # never opened: the extension decides
        skip, _reason = bu.should_skip_resolution(str(p))
        assert skip is False, ext


def test_an_ordinary_image_is_still_skipped_for_being_too_large(tmp_path):
    p = tmp_path / "big.jpg"
    Image.new("RGB", (5000, 3000), "grey").save(str(p), "jpeg")
    skip, reason = bu.should_skip_resolution(str(p))
    assert skip is True and reason


# ─────────────────────────────────────────────
#  Output naming
# ─────────────────────────────────────────────

def test_a_raw_renders_to_a_jpg():
    assert raw_decode.render_name("IMG_1234.CR2") == "IMG_1234_raw.jpg"
    assert raw_decode.render_name("IMG_1234.dng") == "IMG_1234_raw.jpg"


def test_the_raw_render_cannot_collide_with_a_sibling_camera_jpeg(tmp_path):
    # Shooting RAW+JPEG is ordinary. Without the suffix both sources map to
    # IMG_1234.jpg in the mirrored output tree, and the second one processed is
    # silently counted as "already upscaled" - with the film strip and the
    # lineage row pointing at a file produced from the other source.
    src = tmp_path / "src"
    src.mkdir()
    (src / "IMG_1234.CR2").write_bytes(b"x")
    Image.new("RGB", (800, 600), "red").save(str(src / "IMG_1234.JPG"), "jpeg")

    items, _folders = bu.collect_work_items(str(src), str(tmp_path / "out"))
    out_names = sorted(name for _d, _p, _o, name in items)
    assert out_names == ["IMG_1234.jpg", "IMG_1234_raw.jpg"]
    assert len(set(out_names)) == 2


def test_our_own_render_is_recognisable_without_a_database():
    assert raw_decode.is_render_name("IMG_1234_raw.jpg")
    assert raw_decode.is_render_name(r"D:\pics\__upscaled__\IMG_1234_RAW.JPG")
    assert not raw_decode.is_render_name("IMG_1234.jpg")
    assert not raw_decode.is_render_name("IMG_1234.CR2")


def test_the_walk_accepts_raw_alongside_the_ordinary_formats(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.CR2", "b.nef", "c.ARW", "d.rw2"):
        (src / name).write_bytes(b"x")
    Image.new("RGB", (100, 100)).save(str(src / "e.png"))
    items, _ = bu.collect_work_items(str(src), str(tmp_path / "out"))
    assert len(items) == 5


# ─────────────────────────────────────────────
#  Metadata
# ─────────────────────────────────────────────

def _jpeg_with_exif(path, size=(400, 300), orientation=6, date="2005:07:14 09:30:00"):
    exif = Image.Exif()
    exif[271] = "TestCam"                 # Make
    exif[272] = "Model X"                 # Model
    exif[274] = orientation               # Orientation
    exif[306] = date                      # DateTime
    exif[0x8769] = {36867: date, 33434: 0.004}     # DateTimeOriginal, ExposureTime
    img = Image.new("RGB", size, "blue")
    img.save(path, "jpeg", exif=exif.tobytes())
    return exif


def test_a_block_in_hand_is_sanitised_exactly_like_one_read_from_a_file(tmp_path):
    # The RAW path cannot read its metadata out of the file the pixels came from
    # (it is merged from two places), so it needs a blob entry point. The two must
    # not drift: the Orientation rule in particular is one both have to obey.
    src = tmp_path / "src.jpg"
    exif = _jpeg_with_exif(str(src))

    from_file = exif_copy.exif_for_upscaled(str(src), "out.jpg", (800, 600))
    from_blob = exif_copy.exif_for_upscaled_blob(exif.tobytes(), "out.jpg", (800, 600))
    assert from_file and from_blob
    assert from_file == from_blob


def test_the_sanitised_block_forces_orientation_upright():
    # A RAW's pixels come out of the render already upright (LibRaw applies its
    # own `flip`), so a copied Orientation would rotate them a second time. Same
    # answer as the file path, arrived at for a different reason.
    exif = Image.Exif()
    exif[274] = 8                          # rotate 270 CW
    exif[271] = "TestCam"
    blob = exif_copy.exif_for_upscaled_blob(exif.tobytes(), "out.jpg", (100, 50))
    out = Image.Exif()
    out.load(blob)
    assert dict(out)[274] == 1


def test_the_sanitised_block_states_the_written_size():
    exif = Image.Exif()
    exif[271] = "TestCam"
    exif[0x8769] = {40962: 100, 40963: 50}         # the ORIGINAL's dimensions
    blob = exif_copy.exif_for_upscaled_blob(exif.tobytes(), "out.jpg", (3840, 2160))
    out = Image.Exif()
    out.load(blob)
    sub = dict(out.get_ifd(0x8769))
    assert (sub[40962], sub[40963]) == (3840, 2160)


def test_an_empty_block_produces_nothing():
    assert exif_copy.exif_for_upscaled_blob(None, "out.jpg") is None
    assert exif_copy.exif_for_upscaled_blob(b"", "out.jpg") is None
    assert exif_copy.exif_for_upscaled_blob(b"garbage", "out.jpg") is None


def test_raw_development_tags_are_dropped():
    # A DNG's block measures 79-317 KB against a JPEG APP1 ceiling of 64 KB, and
    # exif_copy correctly refuses an oversized block - so before this strip those
    # files came out with NO metadata at all. What makes them big is the raw
    # DEVELOPMENT data, which is meaningless once the file is a picture.
    exif = Image.Exif()
    exif[271] = "TestCam"
    exif[306] = "2009:01:24 13:25:03"
    exif[50721] = b"\x00" * 64             # ColorMatrix1
    exif[51008] = b"\x00" * 64             # OpcodeList1
    exif[50740] = b"\x00" * 64             # DNGPrivateData
    raw_decode._drop_development_tags(exif)
    kept = dict(exif)
    assert 271 in kept and 306 in kept
    assert 50721 not in kept and 51008 not in kept and 50740 not in kept


def test_the_merge_fills_gaps_without_overwriting():
    # The same one rule exif_copy's backfill uses. The RAW container's block wins
    # where both exist (DateTimeOriginal and GPS live there and are absent from
    # every full-size preview in the survey); the preview fills what it lacks,
    # which for a CR3/ORF/RW2/RAF/ARW/SRW is everything.
    # Built the way production builds them - parsed from bytes - so the sub-IFDs
    # behave here exactly as they do on a real file.
    raw = Image.Exif()
    raw[271] = "FromRaw"
    raw[0x8769] = {36867: "2005:07:14 09:30:00"}
    preview = Image.Exif()
    preview[271] = "FromPreview"            # must NOT win
    preview[272] = "Model X"                # must be adopted
    preview[0x8769] = {33434: 0.004}        # must be adopted

    primary   = raw_decode._exif_from_bytes(raw.tobytes())
    secondary = raw_decode._exif_from_bytes(preview.tobytes())
    merged = raw_decode._merge_exif(primary, secondary)

    # Asserted on the SERIALISED block, not on merged.get_ifd(): Pillow caches a
    # parsed sub-IFD in Exif._ifds and does not invalidate it when the pointer
    # tag is assigned, so get_ifd() here still hands back the pre-merge copy
    # while tobytes() writes the merged one. See the note in _merge_exif.
    out = raw_decode._exif_from_bytes(merged.tobytes())
    top, sub = dict(out), dict(out.get_ifd(0x8769))
    assert top[271] == "FromRaw"
    assert top[272] == "Model X"
    assert sub[36867] == "2005:07:14 09:30:00"
    assert 33434 in sub


def test_the_merge_survives_either_side_being_absent():
    only = Image.Exif()
    only[271] = "TestCam"
    assert raw_decode._merge_exif(only, None) is only
    assert raw_decode._merge_exif(None, only) is only
    assert raw_decode._merge_exif(None, None) is None


# ─────────────────────────────────────────────
#  Writing the two outputs
# ─────────────────────────────────────────────

def test_the_render_is_written_as_a_jpeg_carrying_the_metadata(tmp_path):
    exif = Image.Exif()
    exif[271] = "TestCam"
    exif[274] = 6                                   # would rotate if copied raw
    exif[306] = "2005:07:14 09:30:00"
    img = Image.new("RGB", (640, 480), "red")
    out = tmp_path / "IMG_0001_raw.jpg"

    bu._save_render(img, str(out), exif.tobytes())

    with Image.open(str(out)) as im:
        assert im.format == "JPEG"
        assert im.size == (640, 480)
        got = dict(im.getexif())
    assert got[271] == "TestCam"
    assert got[306] == "2005:07:14 09:30:00"
    assert got[274] == 1                            # normalised, not copied


def test_a_failed_render_write_leaves_no_partial_file(tmp_path):
    out = tmp_path / "sub" / "IMG_0001_raw.jpg"     # parent does not exist
    with pytest.raises(Exception):
        bu._save_render(Image.new("RGB", (10, 10)), str(out), None)
    assert not out.exists()
    assert not (tmp_path / "sub").exists()


def test_the_upscaler_input_temp_is_lossless_and_not_a_jpeg(tmp_path):
    # The whole point of rendering from RAW is to avoid the generational loss the
    # app exists to undo. A JPEG temp here is the easy accident, because .jpg is
    # what the output is.
    exif = Image.Exif()
    exif[271] = "TestCam"
    exif[306] = "2005:07:14 09:30:00"
    img = Image.new("RGB", (64, 48), "red")

    tmp = bu._write_upscale_input(img, exif.tobytes())
    try:
        assert tmp.lower().endswith(".png")
        with Image.open(tmp) as im:
            assert im.format == "PNG"
            assert im.convert("RGB").tobytes() == img.tobytes()   # lossless
            got = dict(im.getexif())
        assert got[306] == "2005:07:14 09:30:00"
        # Load-bearing: upscale_engine._load_image runs exif_transpose on whatever
        # it is given, so a temp carrying the camera's Orientation would have its
        # already-upright pixels rotated a second time.
        assert got[274] == 1
    finally:
        os.remove(tmp)


def test_the_upscaler_input_temp_survives_a_source_with_no_metadata(tmp_path):
    tmp = bu._write_upscale_input(Image.new("RGB", (8, 8)), None)
    try:
        with Image.open(tmp) as im:
            assert im.format == "PNG"
    finally:
        os.remove(tmp)


# ─────────────────────────────────────────────
#  Conciliation must never touch a negative
# ─────────────────────────────────────────────

import conciliate as cc                                     # noqa: E402


def test_conciliation_never_plans_to_replace_a_raw_original(tmp_path):
    """The one failure in this whole feature that cannot be undone.

    Conciliation is the app's only destructive tool. If a RAW were ever matched
    with its render, an archive run would move the negative into __Archive__ and
    a DELETE run would remove it outright - and a rendered JPEG is not a
    replacement for a negative, it is one 8-bit interpretation of it with the
    sensor data gone.
    """
    orig = tmp_path / "orig"
    proc = tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    (orig / "IMG_1234.CR2").write_bytes(b"negative")
    # The render, sitting in the processed tree exactly as a real run leaves it,
    # under both the name we produce AND the mirrored name a name-based match
    # would look for.
    Image.new("RGB", (3840, 2160)).save(str(proc / "IMG_1234_raw.jpg"), "jpeg")
    Image.new("RGB", (3840, 2160)).save(str(proc / "IMG_1234.jpg"), "jpeg")

    plan, _folders, _kept, _variants, raw_files = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=None)

    assert plan == []
    assert [os.path.basename(p) for p in raw_files] == ["IMG_1234.CR2"]


def test_every_raw_extension_is_refused_by_conciliation(tmp_path):
    orig = tmp_path / "orig"
    proc = tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    for ext in raw_decode.RAW_EXTS:
        (orig / f"shot{ext}").write_bytes(b"negative")
        Image.new("RGB", (64, 64)).save(str(proc / f"shot{ext[1:]}.jpg"), "jpeg")

    plan, _folders, _kept, _variants, raw_files = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=None)
    assert plan == []
    assert len(raw_files) == len(raw_decode.RAW_EXTS)


def test_a_raw_is_reported_as_a_negative_not_as_a_non_media_file(tmp_path):
    # It lands in `skipped` ("left untouched"), not in `kept` ("non-media"). A
    # CR2 is very much media, and a preview that files it under Thumbs.db does
    # not tell the user their negatives were spared on purpose.
    orig = tmp_path / "orig"
    proc = tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    (orig / "IMG_1234.CR2").write_bytes(b"negative")
    (orig / "Thumbs.db").write_bytes(b"junk")

    _plan, folders, kept_files, _variants, raw_files = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=None)

    _rel, replaced, skipped, kept = folders[0]
    assert (replaced, skipped, kept) == (0, 1, 1)
    assert [os.path.basename(p) for p in kept_files] == ["Thumbs.db"]
    assert len(raw_files) == 1


def test_an_ordinary_image_is_still_replaced_normally(tmp_path):
    # The guard must not have widened into a refusal to conciliate anything.
    orig = tmp_path / "orig"
    proc = tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    Image.new("RGB", (800, 600)).save(str(orig / "photo.jpg"), "jpeg")
    Image.new("RGB", (3840, 2160)).save(str(proc / "photo.jpg"), "jpeg")

    plan, _folders, _kept, _variants, raw_files = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=None)
    assert len(plan) == 1
    assert raw_files == []


# ─────────────────────────────────────────────
#  Real camera files, when they are on hand
# ─────────────────────────────────────────────

_SAMPLES = os.environ.get("IMGTBX_RAW_SAMPLES")


@pytest.mark.skipif(not _SAMPLES or not os.path.isdir(_SAMPLES or ""),
                    reason="set IMGTBX_RAW_SAMPLES to a folder of RAW files")
def test_every_sample_renders_upright_with_a_date():
    """The end-to-end check against real camera files. Not part of the default
    run - a RAW cannot be synthesised and the corpus is 400 MB - but this is the
    test that actually exercised both the preview and demosaic paths, and it is
    how docs/raw-preview-survey.csv was produced."""
    pytest.importorskip("rawpy")
    seen = {"preview": 0, "demosaic": 0}
    for name in sorted(os.listdir(_SAMPLES)):
        if os.path.splitext(name)[1].lower() not in raw_decode.RAW_EXTS:
            continue
        path = os.path.join(_SAMPLES, name)
        result = raw_decode.render(path)
        seen[result.how] += 1
        assert result.image.mode == "RGB"
        assert min(result.image.size) > 0

        blob = exif_copy.exif_for_upscaled_blob(result.exif, "out.jpg",
                                                result.image.size)
        assert blob, f"{name}: no metadata survived"
        exif = Image.Exif()
        exif.load(blob)
        top, sub = dict(exif), dict(exif.get_ifd(0x8769))
        assert top.get(274) == 1, f"{name}: orientation not normalised"
        assert (sub.get(36867) or top.get(36867)
                or sub.get(36868) or top.get(306)), f"{name}: no capture date"
    assert seen["preview"] and seen["demosaic"], "sample exercised only one path"


class _StubPause:
    paused_seconds = 0.0
    quit_requested = False

    def check(self):
        return True


class _StubLogger:
    def tee(self, msg="", timestamp=False):
        pass

    log_only = terminal_only = tee


@pytest.mark.skipif(not _SAMPLES or not os.path.isdir(_SAMPLES or ""),
                    reason="set IMGTBX_RAW_SAMPLES to a folder of RAW files")
def test_the_run_loop_renders_every_raw_and_leaks_no_temps(tmp_path, monkeypatch):
    """The render-only branch, through the REAL run_pass loop.

    This is the branch every real RAW takes: at the shipped target and cutoff,
    not one of the 24 survey files would ever be upscaled. The stub engine is
    here to be LOUD if that ever stops being true, not to be called.
    """
    pytest.importorskip("rawpy")
    import tempfile
    import glob

    class _NoUpscale:
        def upscale(self, *_a, **_k):
            raise AssertionError("upscaled a file that is already above target")

    monkeypatch.setattr(bu, "ENGINE", _NoUpscale())
    monkeypatch.setattr(bu, "AUTO_STRAIGHTEN", False)

    out = tmp_path / "out"
    items, _folders = bu.collect_work_items(_SAMPLES, str(out))
    assert items, "no RAW files in the sample folder"

    import time
    stats = bu.run_pass(items, _SAMPLES, str(out), time.time(),
                        _StubPause(), _StubLogger(), set(), cache=None)

    assert stats["total_processed"] == 0            # nothing was upscaled
    assert stats["total_rendered"] == len(items)    # everything was rendered
    assert stats["total_failed"] == 0
    assert len(list(out.glob("*_raw.jpg"))) == len(items)
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "itbx_raw_*"))


@pytest.mark.skipif(not _SAMPLES or not os.path.isdir(_SAMPLES or ""),
                    reason="set IMGTBX_RAW_SAMPLES to a folder of RAW files")
def test_the_run_loop_hands_the_upscaler_a_lossless_upright_temp(tmp_path, monkeypatch):
    """The other branch: a RAW small enough to be worth upscaling.

    Reached here by raising the target rather than by finding a small RAW,
    because no such file exists in the wild - which is the finding that shaped
    this whole feature.
    """
    pytest.importorskip("rawpy")
    import glob
    import tempfile
    import time

    monkeypatch.setattr(bu, "MAX_RESOLUTION", 20000)
    monkeypatch.setattr(bu, "RESOLUTION", 12000)
    monkeypatch.setattr(bu, "UPSCALE_CUTOFF_PCT", 0)
    monkeypatch.setattr(bu, "AUTO_STRAIGHTEN", False)
    # The stub returns instantly, so every timing the watchdog sees is decode
    # jitter around a zero baseline and it trips on the sixth image. That is the
    # stub's artifact, not the pipeline's: in a real run the SeedVR2 upscale is
    # minutes and the RAW decode is under a second.
    monkeypatch.setattr(bu, "WATCHDOG_ENABLED", False)

    handed = []

    class _Recorder:
        def upscale(self, src, dest, _resolution):
            with Image.open(src) as im:
                handed.append((im.format, dict(im.getexif()).get(274)))
            Image.new("RGB", (64, 64)).save(dest, "jpeg")

    monkeypatch.setattr(bu, "ENGINE", _Recorder())

    out = tmp_path / "out"
    items, _folders = bu.collect_work_items(_SAMPLES, str(out))
    stats = bu.run_pass(items, _SAMPLES, str(out), time.time(),
                        _StubPause(), _StubLogger(), set(), cache=None)

    assert stats["total_processed"] == len(items)
    assert stats["total_rendered"] == 0
    assert stats["total_failed"] == 0
    # Lossless, and already upright - the two properties the temp exists to have.
    assert {fmt for fmt, _o in handed} == {"PNG"}
    assert {orient for _f, orient in handed} == {1}
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "itbx_raw_*"))
