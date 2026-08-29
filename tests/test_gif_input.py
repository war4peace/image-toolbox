"""
Static GIF input (future-features #27), and the guard that is the real feature.

`.gif` was in no extension list at all, so this is a clean addition rather than a
latent bug. What makes it worth a test file of its own is that adding it to
IMAGE_EXTS *alone* reproduces #17's data loss exactly: the engine is RGB frame-0
only, so an animated or transparent GIF would be written back flattened, and
Conciliation's mirrored-name fallback would then match it with full confidence
and archive or DELETE the animated original. The two list entries are one change,
and `test_the_two_lists_moved_together` is what keeps them that way.

READ THIS BEFORE ADDING A FIXTURE. Pillow's GIF *writer* silently discards the
very properties these tests are about, in two independent ways measured here:

  * `save(transparency=N)` writes NO transparency block unless index N is
    actually USED in the pixel data. A uniform fill saved with `transparency=0`
    reloads with info keys ['background', 'version'] and nothing else.
  * `append_images=[im] * 5` collapses to n_frames == 1, because identical
    frames are optimised away. The "animated" fixture is then a 105-byte static
    GIF.

Either one produces a fixture that is quietly the opposite of what it claims, so
every builder below asserts its own result before a test relies on it.
"""

import os

import pytest

pytest.importorskip("PIL")

from PIL import Image                                       # noqa: E402

import db                                                   # noqa: E402
import runner_common as rc                                  # noqa: E402
import batch_upscale as bu                                  # noqa: E402
import conciliate as cc                                     # noqa: E402


# -- fixtures that verify themselves -----------------------------------------

_PALETTE = [0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * 759


def _gif(path, frames=1, transparent=False):
    """Write a GIF and PROVE it came out as asked. See the module docstring."""
    def _frame(i):
        im = Image.new("P", (40, 30), 1)
        im.putpalette(_PALETTE)
        if transparent:                      # index 0 must be USED, or the
            for x in range(10):              # writer drops the transparency
                for y in range(10):
                    im.putpixel((x, y), 0)
        for x in range(i * 5, i * 5 + 5):    # frames must DIFFER, or they
            for y in range(30):              # collapse into one
                im.putpixel((x, y), 2)
        return im

    kw = {"transparency": 0} if transparent else {}
    seq = [_frame(i) for i in range(frames)]
    if frames > 1:
        seq[0].save(str(path), save_all=True, append_images=seq[1:],
                    duration=100, **kw)
    else:
        seq[0].save(str(path), **kw)

    with Image.open(str(path)) as im:
        assert getattr(im, "n_frames", 1) == frames, (
            "fixture claims %d frames but Pillow wrote %d: identical frames "
            "collapse" % (frames, getattr(im, "n_frames", 1)))
        assert ("transparency" in (im.info or {})) is transparent, (
            "fixture transparency claim is wrong: the transparent palette "
            "index has to be USED in the pixel data or the writer drops it")
    return str(path)


def _png(path, size=(40, 30)):
    Image.new("RGB", size, (10, 20, 30)).save(str(path))
    return str(path)


def test_the_fixture_builder_is_not_lying(tmp_path):
    """The builders assert their own output, so this only pins that the two
    collapse modes are real: without the distinct-frames and used-index tricks
    Pillow writes a static, opaque GIF and every guard test below would be
    testing nothing."""
    naive = tmp_path / "naive.gif"
    im = Image.new("P", (40, 30), 1)
    im.putpalette(_PALETTE)
    im.save(str(naive), save_all=True, append_images=[im.copy()] * 5,
            duration=100, transparency=0)
    with Image.open(str(naive)) as got:
        assert got.n_frames == 1, "identical frames no longer collapse"
        assert "transparency" not in (got.info or {}), \
            "an unused transparent index no longer gets dropped"


# -- the guard (#17 applied to #27) ------------------------------------------

def test_the_two_lists_moved_together():
    """The invariant of this whole feature. `.gif` in IMAGE_EXTS without `.gif`
    in VARIANT_CANDIDATE_EXTS is #17's data loss in a format nobody guarded, so
    the two entries must never be made in sequence."""
    assert (".gif" in bu.IMAGE_EXTS) == (".gif" in rc.VARIANT_CANDIDATE_EXTS)
    assert ".gif" in bu.IMAGE_EXTS


def test_a_plain_static_gif_is_upscaled_normally(tmp_path):
    """The one GIF this feature actually processes. It must NOT be a variant, or
    the feature would be a refusal with extra steps."""
    assert rc.image_variant_reason(_gif(tmp_path / "flat.gif")) is None


def test_a_transparent_static_gif_is_refused(tmp_path):
    """`convert("RGB")` composites transparent pixels to black SILENTLY, so
    without this the output is a black-cornered flatten under a mirrored name."""
    reason = rc.image_variant_reason(_gif(tmp_path / "t.gif", transparent=True))
    assert reason == "would lose transparency"
    assert rc.is_variant_reason(reason)


def test_an_animated_gif_is_refused_and_counted_in_frames(tmp_path):
    """A GIF measures time, not paper: "5 of 6 pages" is the wrong noun and
    nothing else in the suite would catch it."""
    reason = rc.image_variant_reason(_gif(tmp_path / "a.gif", frames=6))
    assert reason == "would lose 5 of 6 frames"
    assert "pages" not in reason


def test_an_animated_transparent_gif_lists_both(tmp_path):
    reason = rc.image_variant_reason(_gif(tmp_path / "b.gif", frames=6,
                                          transparent=True))
    assert reason == "would lose transparency, 5 of 6 frames"


def test_gif_dimensions_are_read_correctly(tmp_path):
    """There is no fast GIF header parser, so this goes through the Pillow
    fallback. Pinned because a wrong size here would upscale to the wrong
    target, the way a RAW's IFD-0 preview did (#19)."""
    assert rc.get_image_dimensions(_gif(tmp_path / "d.gif")) == (40, 30)


# -- the output name ---------------------------------------------------------

def test_the_output_is_a_png_with_a_marker():
    assert rc.gif_output_name("holiday.gif") == "holiday_gif.png"
    assert rc.gif_output_name(os.path.join("sub", "holiday.GIF")) == \
        "holiday_gif.png"


def test_the_marker_is_what_stops_two_sources_claiming_one_output():
    """`logo.gif` and `logo.png` in one folder would both want `logo.png`. That
    is not a crash: the first processed wins and the second is silently counted
    "already upscaled", pointing the film strip and the lineage row at a file
    produced from the other source. Exactly #19's RAW+JPEG collision."""
    assert rc.gif_output_name("logo.gif") != "logo.png"


def test_the_suffix_is_unconditional():
    """Never "only when it would collide". A name that depends on what else is
    in the folder changes when a sibling is added later, which breaks every
    inverse below after the fact."""
    assert rc.gif_output_name("unique.gif").endswith(rc.GIF_OUTPUT_SUFFIX)


def test_the_name_round_trips_both_ways():
    assert rc.gif_source_name(rc.gif_output_name("cat.gif")) == "cat.gif"
    assert rc.is_gif_output_name("cat_gif.png")
    assert not rc.is_gif_output_name("cat.png")
    assert rc.gif_source_name("cat.png") is None
    assert rc.gif_source_name("") is None


def test_the_walk_names_a_gif_output(tmp_path):
    """Reaches for the effect: the real scanner, not the naming helper. There is
    exactly ONE place that decides an output name, and this is what proves the
    GIF branch is wired into it."""
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    _gif(src / "holiday.gif")
    _png(src / "photo.png")

    items, _folders = bu.collect_work_items(str(src), str(out))
    names = {os.path.basename(p): n for _d, p, _o, n in items}
    assert names["holiday.gif"] == "holiday_gif.png"
    assert names["photo.png"] == "photo.png", "other formats still mirror"


# -- Conciliation: it must recognise the output and move it in ---------------

def test_conciliation_matches_a_gif_by_name_without_lineage(db_conn, tmp_path):
    """The condition this feature was approved on. Lineage would usually match
    it, but "usually" is not good enough for the tool that archives or deletes
    the original: a tree upscaled by another install, or one whose cache.db was
    deleted, has no lineage row at all."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    src = _gif(orig / "holiday.gif")
    out = _png(proc / "holiday_gif.png")

    plan, folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                       tr_index=None,
                                                       conn=db_conn)
    assert variants == []
    assert [(o, p) for o, p, _r in plan] == [(src, out)]
    assert folders[0][1] == 1                     # replaced


def test_the_conciliated_gif_keeps_its_marker_in_the_original_tree(tmp_path):
    """It lands as `<stem>_gif.png`, not `<stem>.png`. Stripping the marker on
    the way in looks tidier and is not safe: `logo.gif` and `logo.png` can both
    be conciliated into one folder, and this moves with shutil.move, which
    overwrites without asking."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    _gif(orig / "holiday.gif")
    out = _png(proc / "holiday_gif.png")

    dest = cc._move_processed_in(out, "holiday.gif", str(orig))
    assert os.path.basename(dest) == "holiday_gif.png"
    assert os.path.isfile(dest)


def test_conciliation_finds_a_gif_output_that_was_tagged_and_renamed(tmp_path):
    """Tag & Rename runs on the upscaled tree, so by the time Conciliation looks
    the output may be `holiday_gif_A_Red_Boat.png`. The tag index is consulted
    for the GIF name as well as the mirrored one."""
    proc = tmp_path / "proc"
    proc.mkdir()
    renamed = _png(proc / "holiday_gif_A_Red_Boat.png")
    tr_index = {os.path.normcase("holiday_gif.png"):
                "holiday_gif_A_Red_Boat.png"}

    assert cc.resolve_by_name("holiday.gif", str(proc), tr_index) == renamed


def test_conciliation_refuses_an_animated_gif(db_conn, tmp_path):
    """The data-loss case, checked on the ORIGINAL before either matching path,
    so a tree upscaled before this shipped is protected too."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    _gif(orig / "clip.gif", frames=6)
    _png(proc / "clip_gif.png")               # what a careless build produced

    plan, _folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                        tr_index=None,
                                                        conn=db_conn)
    assert plan == []
    assert [os.path.basename(p) for p, _r in variants] == ["clip.gif"]
    assert "frames" in variants[0][1]


def test_conciliation_refuses_an_animated_gif_even_with_lineage(db_conn,
                                                                tmp_path):
    """Lineage is the STRONGER match, so the guard has to sit before it: a hash
    link proves the two files are a pair, not that the pair is lossless."""
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    src = _gif(orig / "clip.gif", frames=6, transparent=True)
    out = _png(proc / "clip_gif.png")
    db.record_upscale_lineage(db_conn,
                              db.hash_file_cached(db_conn, src),
                              db.hash_file_cached(db_conn, out), src, out)

    plan, _folders, _kept, variants, *_ = cc.build_plan(str(orig), str(proc),
                                                        tr_index=None,
                                                        conn=db_conn)
    assert plan == []
    assert len(variants) == 1


# -- the other tools ---------------------------------------------------------

def test_the_browser_pairs_a_gif_output_back_to_its_source():
    """Unlike a RAW render (excluded from pairing, because the browser cannot
    draw a RAW as the "before" half), a GIF is an ordinary Pillow image, so
    inverting the name is what gives it Compare long after the run."""
    from gui import browse_upscaled as bup

    def resolve(path):
        return path if path.endswith("holiday.gif") else None

    got = bup.pair_source(os.path.join("sub", "holiday_gif.png"), "SRC",
                          inv_tag=None, resolve=resolve)
    assert got == os.path.join("SRC", "sub", "holiday.gif")


def test_tag_and_rename_ignores_gif():
    """It writes a description into the file's own metadata and GIF has nowhere
    to put one. Nothing is lost: the documented workflow points that tab at the
    UPSCALED folder, where a GIF is already a PNG."""
    import tag_and_rename as tr
    assert ".gif" not in tr.IMAGE_EXTS


def test_the_browser_does_not_list_stray_gifs():
    """The upscaler ACCEPTS a GIF but never WRITES one, so an output tree holds
    no .gif and listing them would only surface files this app did not make."""
    from gui import browse_upscaled as bup
    assert ".gif" not in bup.IMAGE_EXTS
