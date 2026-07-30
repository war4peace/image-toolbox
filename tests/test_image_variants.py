"""
Roadmap #17: skip image variants the pipeline cannot round-trip.

The upscale engine is RGB-only end to end (`convert("RGB")` in, `arr[..., :3]` as
mode="RGB" out, frame 0 only), so transparency, extra pages and >8-bit depth are
silently discarded - and because the output keeps the SAME name and extension,
Conciliation's mirrored-name fallback then matches it with full confidence and
archives or DELETES the only copy that still had them.

The decision was to detect these and leave them alone, the way non-media files are
already left alone. These tests cover the detector, the Batch Upscaler's
eligibility/skip path, and Conciliation's refusal to replace them.
"""

import os

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

import numpy as np                                          # noqa: E402
from PIL import Image                                       # noqa: E402

import db                                                   # noqa: E402
import runner_common as rc                                  # noqa: E402
import batch_upscale as bu                                  # noqa: E402
import conciliate as cc                                     # noqa: E402


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """A throwaway cache.db, so a test never writes the user's real one."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


# ── file factories ───────────────────────────────────────────────────────────

def _plain(path, size=(40, 30)):
    Image.new("RGB", size, (10, 20, 30)).save(path)
    return str(path)


def _alpha(path, size=(40, 30)):
    Image.new("RGBA", size, (10, 20, 30, 128)).save(path)
    return str(path)


def _gray16(path, size=(40, 30)):
    w, h = size
    Image.fromarray(np.arange(w * h).reshape(h, w).astype("uint16")).save(path)
    return str(path)


def _multipage_tiff(path, pages=3, size=(40, 30)):
    extra = [Image.new("RGB", size) for _ in range(pages - 1)]
    Image.new("RGB", size).save(path, save_all=True, append_images=extra)
    return str(path)


# ── the detector ─────────────────────────────────────────────────────────────

def test_a_plain_rgb_image_is_not_a_variant(tmp_path):
    assert rc.image_variant_reason(_plain(tmp_path / "a.png")) is None
    assert rc.image_variant_reason(_plain(tmp_path / "a.jpg")) is None
    assert rc.image_variant_reason(_plain(tmp_path / "a.webp")) is None
    assert rc.image_variant_reason(_plain(tmp_path / "a.tif")) is None


@pytest.mark.parametrize("name", ["a.png", "a.webp", "a.tif"])
def test_alpha_is_detected(tmp_path, name):
    reason = rc.image_variant_reason(_alpha(tmp_path / name))
    assert reason == "would lose transparency"
    assert rc.is_variant_reason(reason)


def test_palette_transparency_is_detected(tmp_path):
    """A paletted PNG carries its transparency in a tRNS chunk, so the MODE is
    "P" and only img.info gives it away."""
    p = tmp_path / "pal.png"
    Image.new("P", (20, 20)).save(p, transparency=0)
    assert rc.image_variant_reason(str(p)) == "would lose transparency"


@pytest.mark.parametrize("name", ["a.png", "a.tif"])
def test_sixteen_bit_depth_is_detected(tmp_path, name):
    """Pillow presents a 16-bit file as an ordinary 8-bit mode in some cases, so
    the detector reads the format's own header (IHDR / BitsPerSample)."""
    assert rc.image_variant_reason(_gray16(tmp_path / name)) == "would lose 16-bit depth"


def test_multi_page_tiff_is_detected(tmp_path):
    assert rc.image_variant_reason(_multipage_tiff(tmp_path / "m.tif", pages=4)) \
        == "would lose 3 of 4 pages"


def test_several_losses_are_listed_together(tmp_path):
    p = tmp_path / "both.tif"
    Image.new("RGBA", (20, 20), (1, 2, 3, 4)).save(
        p, save_all=True, append_images=[Image.new("RGBA", (20, 20))])
    reason = rc.image_variant_reason(str(p))
    assert reason.startswith(rc.VARIANT_PREFIX)
    assert "transparency" in reason and "1 of 2 pages" in reason


def test_a_multi_frame_webp_is_reported_as_frames_not_pages(tmp_path, monkeypatch):
    """Wording only: a WebP is animated, not paginated. Pillow in this venv can't
    WRITE an animated WebP, so the frame count is stubbed rather than skipped."""
    real_open = Image.open

    class _Fake:
        mode = "RGB"
        info = {}
        n_frames = 5

        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(Image, "open", lambda p, *a, **k: _Fake())
    try:
        assert rc.image_variant_reason("x.webp") == "would lose 4 of 5 frames"
    finally:
        monkeypatch.setattr(Image, "open", real_open)


def test_an_unreadable_file_is_not_claimed_as_a_variant(tmp_path):
    """The runners already have a "corrupted / unreadable" classification with its
    own count and its own listing; this check must not steal it."""
    p = tmp_path / "broken.png"
    p.write_bytes(b"this is not a png")
    assert rc.image_variant_reason(str(p)) is None


def test_a_missing_file_is_not_claimed_as_a_variant(tmp_path):
    assert rc.image_variant_reason(str(tmp_path / "nope.png")) is None


def test_jpeg_never_pays_for_a_pillow_open(tmp_path, monkeypatch):
    """JPEG is the bulk of a photo tree and cannot carry any of the three traits,
    so the eligibility scan must not open it at all."""
    def _boom(*a, **k):
        raise AssertionError("Image.open must not be called for .jpg")

    monkeypatch.setattr(Image, "open", _boom)
    assert rc.image_variant_reason(str(tmp_path / "photo.jpg")) is None


def test_is_variant_reason_rejects_an_ordinary_skip_reason():
    assert not rc.is_variant_reason("width 4000px >= 3840px")
    assert not rc.is_variant_reason("")
    assert not rc.is_variant_reason(None)


# ── Batch Upscaler: eligibility ──────────────────────────────────────────────

def test_the_eligibility_cache_records_the_variant_reason(db_conn, tmp_path):
    """The cache stores only the reason STRING, so a later run has to classify a
    cached ineligible entry from its prefix. Round-trip that."""
    src = tmp_path / "src"
    src.mkdir()
    _alpha(src / "logo.png")
    cache = bu.EligibilityCache(str(src), str(tmp_path / "out"))
    reason = bu.image_variant_reason(str(src / "logo.png"))
    cache.set(str(src / "logo.png"), eligible=False, already_done=False,
              skip_reason=reason)
    cache.save()

    reloaded = bu.EligibilityCache(str(src), str(tmp_path / "out"))
    entry = reloaded.get(str(src / "logo.png"))
    assert entry["eligible"] is False
    assert bu.is_variant_reason(entry["skip_reason"])


def test_the_upscaler_re_exports_the_detector():
    """batch_upscale re-exports it under a local name, the pattern every other
    runner_common helper follows. Keep them the same object."""
    assert bu.image_variant_reason is rc.image_variant_reason
    assert bu.is_variant_reason is rc.is_variant_reason


def test_the_variant_counter_is_part_of_the_folder_stats():
    """A missing key here is what took down the whole end-of-run summary once (the
    'skipped_corrupt' incident), so pin it."""
    assert "skipped_variant" in bu._FOLDER_STAT_KEYS
    assert bu._new_folder_stats()["skipped_variant"] == 0


def test_the_two_pass_merge_carries_the_variant_totals():
    def _stats(n, files):
        s = {k: 0 for k in ("total_processed", "total_skipped_done",
                            "total_skipped_size", "total_skipped_variant",
                            "total_skipped_missing", "total_skipped_corrupt",
                            "total_failed")}
        s["total_skipped_variant"] = n
        s["folder_stats"] = {}
        s["variant_files"] = files
        return s

    merged = bu._merge_pass_stats(_stats(2, [("a.png", "would lose transparency")]),
                                  _stats(1, [("b.tif", "would lose 16-bit depth")]))
    assert merged["total_skipped_variant"] == 3
    assert len(merged["variant_files"]) == 2


# ── Conciliation: never replace one ──────────────────────────────────────────

def test_conciliation_refuses_to_replace_a_transparent_png(db_conn, tmp_path):
    """The sharp case: a pre-#17 run flattened it under the SAME name, so the
    mirrored-name fallback matches with full confidence and the original (the only
    copy with an alpha channel) gets archived or deleted."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir(); proc.mkdir()
    _alpha(orig / "logo.png")
    _plain(proc / "logo.png")            # what the old upscaler produced

    plan, folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                   tr_index=None, conn=db_conn)
    assert plan == []
    assert [os.path.basename(p) for p, _r in variants] == ["logo.png"]
    assert folders[0][1] == 0            # replaced
    assert folders[0][2] == 1            # counted as left untouched


def test_conciliation_still_replaces_an_ordinary_image(db_conn, tmp_path):
    """The guard must not swallow the normal path."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir(); proc.mkdir()
    _plain(orig / "photo.png")
    _plain(proc / "photo.png", size=(400, 300))

    plan, _folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                    tr_index=None, conn=db_conn)
    assert len(plan) == 1
    assert variants == []


def test_conciliation_refuses_a_variant_even_with_recorded_lineage(db_conn, tmp_path):
    """Lineage is the STRONGER match, so the check has to sit before both paths:
    a hash link proves the two files are a pair, not that the pair is lossless."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir(); proc.mkdir()
    src = _gray16(orig / "scan.tif")
    out = _plain(proc / "scan.tif")
    db.record_upscale_lineage(db_conn,
                              db.hash_file_cached(db_conn, src),
                              db.hash_file_cached(db_conn, out), src, out)

    plan, _folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                    tr_index=None, conn=db_conn)
    assert plan == []
    assert len(variants) == 1
    assert "16-bit" in variants[0][1]
