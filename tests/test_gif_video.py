"""
Animated GIF -> upscaled video (roadmap #27 phase 2, recorded in `docs/video-upscaler.md`
section 20).

The feature is three stages around the EXISTING video pipeline: prep the GIF into an
ordinary constant-rate video, let the pipeline do what it already does, then put the
original per-frame timing back. Two of those stages are new and both have a failure
mode that reports success, so that is what these tests aim at.

  * PREP must be frame-exact. Handing the GIF straight to the splitter is not wrong,
    it is EXPENSIVE: the CFR normalise inflates a messy-timed 10-frame GIF to 38
    frames, and every one of those is a full diffusion pass that nothing in the UI
    shows. A prep that quietly gained or lost frames would look identical to one that
    worked.
  * RETIME must be exact, and the obvious form is not. `duration` directives plus the
    repeated last frame the concat demuxer needs ran +60 ms long on a 760 ms clip, and
    `-t` overcorrected to -40 ms; both leave the trailing frame's length to ffmpeg.
    plan_timing removes the decision, so its arithmetic is tested directly.
  * The MATTE must be applied deliberately. A transparent GIF decodes as bgra and a
    default conversion composites it to BLACK, which is also the default matte, so a
    matte that silently did nothing would pass any test that only checked the default.
"""

import os
import subprocess

import pytest

pytest.importorskip("PIL")

from PIL import Image                                       # noqa: E402

import video_pipeline as vp                                 # noqa: E402
import gif_video as gv                                      # noqa: E402


def _have_ffmpeg():
    try:
        vp.find_ffmpeg()
        return True
    except Exception:                                       # noqa: BLE001
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not available")

# Timing shapes that matter. "one_fast_tick" is the pathological one: a single 10 ms
# frame among 100 ms frames is what many encoders emit for "as fast as possible".
MESSY = [80, 40, 40, 120, 40, 200, 40, 40, 60, 100]
UNIFORM = [100] * 10
ONE_FAST_TICK = [100, 100, 100, 10, 100, 100, 100, 100, 100, 100]
COPRIME = [70, 30, 110, 50, 90]


GIF_SIZE = (384, 288)      # clears NVENC's 256x256 floor: retime encodes
                           # the UPSCALED output in production, never a
                           # thumbnail, so a tiny fixture is the unrealistic
                           # thing here, not the encoder choice.


def _gif(path, delays, transparent=False, size=GIF_SIZE):
    """An animated GIF with explicit per-frame delays, verified after writing.

    Both properties under test are ones Pillow's WRITER discards when it can: frames
    must DIFFER or they collapse to one, and a transparent index must be USED in the
    pixel data or no transparency block is written. See tests/test_gif_input.py.
    """
    frames = []
    for i, _ in enumerate(delays):
        im = Image.new("P", size, 1)
        im.putpalette([0, 0, 0, 200, 30, 30, 30, 200, 30] + [0] * 759)
        for x in range(min(size[0], i * 6), min(size[0], i * 6 + 6)):
            for y in range(size[1] - 12):        # the bar never reaches the corner
                im.putpixel((x, y), 2)
        if transparent:
            for x in range(size[0] - 10, size[0]):
                for y in range(size[1] - 10, size[1]):
                    im.putpixel((x, y), 0)
        frames.append(im)
    kw = {"transparency": 0} if transparent else {}
    frames[0].save(str(path), save_all=True, append_images=frames[1:],
                   duration=delays, loop=0, disposal=2, **kw)
    with Image.open(str(path)) as im:
        assert im.n_frames == len(delays), "identical frames collapsed"
        assert ("transparency" in (im.info or {})) is transparent, \
            "the transparent index has to be USED or the writer drops it"
    return str(path)


# -- plan_timing: the arithmetic that decides the output's length ------------

@pytest.mark.parametrize("delays", [MESSY, UNIFORM, ONE_FAST_TICK, COPRIME])
def test_the_timing_plan_reproduces_the_duration_exactly(delays):
    """The property that matters, stated as a property rather than as four magic
    numbers: total frames divided by the rate IS the GIF's own duration."""
    fps, reps = gv.plan_timing(delays)
    assert abs(sum(reps) / fps - sum(delays) / 1000.0) < 1e-9


def test_every_frame_survives_the_plan():
    """A frame reduced to zero repeats would vanish from the animation. reps is
    floored at 1 precisely so a short frame is shown briefly rather than dropped."""
    fps, reps = gv.plan_timing(ONE_FAST_TICK)
    assert len(reps) == len(ONE_FAST_TICK)
    assert min(reps) >= 1


def test_a_uniform_gif_needs_no_duplication_at_all():
    """The common case must stay cheap and obvious: 10 frames at 100 ms is 10 fps and
    one repeat each, not 100 fps and ten repeats each."""
    assert gv.plan_timing(UNIFORM) == (10, [1] * 10)


def test_a_zero_delay_becomes_the_browser_default():
    """Some encoders write 0 ms meaning "as fast as possible". Treated literally the
    frame has no duration and disappears."""
    fps, reps = gv.plan_timing([0, 0, 0])
    assert reps == [1, 1, 1] and fps == gv.MIN_FPS


def test_a_slideshow_gif_is_clamped_not_run_at_a_third_of_a_frame_per_second():
    """3 s per frame is 0.33 fps, which no player handles sensibly. It clamps to the
    floor and duplicates instead, and the duration still comes out exact."""
    fps, reps = gv.plan_timing([3000, 3000])
    assert fps == gv.MIN_FPS
    assert sum(reps) / fps == 6.0


def test_an_empty_timing_plan_is_not_a_crash():
    assert gv.plan_timing([]) == (0, [])
    assert gv.plan_timing(None) == (0, [])


# -- naming ------------------------------------------------------------------

def test_the_output_name_carries_the_gif_marker():
    """`logo.gif` and `logo.mp4` in one folder would both claim `logo_4K.mp4`. Third
    time this codebase has met that collision (#19 RAW+JPEG, #27 phase 1 GIF+PNG)."""
    assert gv.output_name("logo.gif", "4K") == "logo_gif_4K.mp4"
    assert gv.output_name("logo.gif", "4K") != "logo_4K.mp4"
    assert gv.is_output_name("logo_gif_4K.mp4")
    assert not gv.is_output_name("logo_4K.mp4")


def test_the_marker_is_unconditional():
    """Never "only when it would collide": a name that depends on what else is in the
    folder changes when a sibling is added later, breaking every inverse after."""
    assert gv.output_name("nothing_else_like_it.gif", "1080p").endswith(
        "_gif_1080p.mp4")


# -- is_animated: the switch that decides which tool handles the file --------

def test_is_animated_separates_the_two_tools(tmp_path):
    """A static GIF belongs to the Batch Upscaler and an animated one to the Video
    Upscaler. This is the whole of that decision, so it must not guess."""
    assert gv.is_animated(_gif(tmp_path / "a.gif", UNIFORM))
    Image.new("P", (40, 30), 1).save(str(tmp_path / "s.gif"))
    assert not gv.is_animated(str(tmp_path / "s.gif"))


def test_is_animated_is_false_for_everything_else(tmp_path):
    """Unreadable and non-GIF both answer False rather than raising: the callers have
    their own "corrupted / unreadable" reporting and this must not steal it."""
    png = tmp_path / "x.png"
    Image.new("RGB", (8, 8)).save(str(png))
    assert not gv.is_animated(str(png))
    bad = tmp_path / "bad.gif"
    bad.write_bytes(b"not a gif")
    assert not gv.is_animated(str(bad))
    assert not gv.is_animated(str(tmp_path / "missing.gif"))


# -- prep and retime, against real ffmpeg ------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("delays", [MESSY, UNIFORM, ONE_FAST_TICK])
def test_prep_is_frame_exact_and_clean_cfr(tmp_path, delays):
    """The measurement the whole feature rests on. The intermediate must hold exactly
    one frame per SOURCE frame (not per normalised tick) and declare a constant rate,
    because plan_split drives its maths off the declared rate."""
    src = _gif(tmp_path / "in.gif", delays)
    dest = str(tmp_path / "prep.mkv")
    got = gv.prepare(src, dest, str(tmp_path / "work"))

    assert got == delays
    assert vp.count_frames(dest) == len(delays)
    info = vp.probe(dest, count=True)
    assert not info.is_vfr
    assert info.r_fps == gv.NOMINAL_FPS


@needs_ffmpeg
def test_prep_refuses_a_static_gif(tmp_path):
    """A static GIF is the Batch Upscaler's job (#27 phase 1). Producing a one-frame
    video from it would be a silently useless result."""
    Image.new("P", (40, 30), 1).save(str(tmp_path / "s.gif"))
    with pytest.raises(vp.FFmpegError):
        gv.prepare(str(tmp_path / "s.gif"), str(tmp_path / "o.mkv"),
                   str(tmp_path / "w"))


@needs_ffmpeg
def test_the_matte_is_applied_deliberately(tmp_path):
    """The trap: a default conversion composites transparency to BLACK, which is also
    the default matte, so a matte that did nothing would pass a black-only test. White
    is what proves the setting is wired to the pixels."""
    src = _gif(tmp_path / "t.gif", UNIFORM, transparent=True)
    corners = {}
    for matte in ("black", "white"):
        dest = str(tmp_path / f"prep_{matte}.mkv")
        gv.prepare(src, dest, str(tmp_path / f"work_{matte}"), matte=matte)
        png = str(tmp_path / f"{matte}.png")
        ffmpeg, _ = vp.find_ffmpeg()
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", dest,
                        "-frames:v", "1", png], check=True)
        with Image.open(png) as im:
            corners[matte] = im.convert("RGB").getpixel(
                (GIF_SIZE[0] - 5, GIF_SIZE[1] - 5))

    assert corners["black"] != corners["white"], \
        "the matte colour changed nothing: it is not reaching the pixels"
    assert min(corners["white"]) > 200, corners["white"]
    assert max(corners["black"]) < 55, corners["black"]


@needs_ffmpeg
@pytest.mark.parametrize("delays", [MESSY, UNIFORM, ONE_FAST_TICK, COPRIME])
def test_retime_restores_the_original_duration(tmp_path, delays):
    """End to end on the two new stages: prep, then re-time the SAME frames (standing
    in for the upscale, which does not change the count). Zero drift is the bar,
    because the forms that leave the trailing frame to ffmpeg missed by 40-60 ms."""
    src = _gif(tmp_path / "in.gif", delays)
    prep = str(tmp_path / "prep.mkv")
    got = gv.prepare(src, prep, str(tmp_path / "work"))

    final = str(tmp_path / "out.mp4")
    gv.retime(prep, got, final, str(tmp_path / "work2"))

    info = vp.probe(final, count=True)
    want = sum(delays) / 1000.0
    assert abs(info.duration - want) < 0.005, f"drift {info.duration - want:+.3f}s"
    fps, reps = gv.plan_timing(delays)
    assert vp.count_frames(final) == sum(reps)


@needs_ffmpeg
def test_retime_works_in_place(tmp_path):
    """The caller re-times the upscaled output ONTO ITSELF, and ffmpeg cannot open one
    path for reading and writing at once. Pinned because the in-place call is the only
    one production makes, and a version that only worked src != dest would pass every
    other test here."""
    src = _gif(tmp_path / "in.gif", MESSY)
    target = str(tmp_path / "out.mkv")
    delays = gv.prepare(src, target, str(tmp_path / "work"))

    gv.retime(target, delays, target, str(tmp_path / "work2"))

    info = vp.probe(target, count=True)
    assert abs(info.duration - sum(MESSY) / 1000.0) < 0.005


@needs_ffmpeg
def test_retime_refuses_a_frame_count_it_did_not_expect(tmp_path):
    """If the pipeline ever returned a different frame count, re-timing would map the
    wrong frames onto the wrong delays and still produce a playable file. That is the
    failure that must be loud."""
    src = _gif(tmp_path / "in.gif", UNIFORM)
    prep = str(tmp_path / "prep.mkv")
    delays = gv.prepare(src, prep, str(tmp_path / "work"))

    with pytest.raises(vp.FFmpegError, match="different frame count"):
        gv.retime(prep, delays + [100], str(tmp_path / "out.mp4"),
                  str(tmp_path / "work2"))


@needs_ffmpeg
def test_the_prep_survives_the_apps_own_pipeline(tmp_path):
    """Reaches for the effect rather than the plan: run the intermediate through the
    real container pipeline (split / concat / mux / drift) with the no-pod passthrough
    engine. If the prep were not clean CFR this is where it would show."""
    src = _gif(tmp_path / "in.gif", MESSY)
    prep = str(tmp_path / "prep.mkv")
    gv.prepare(src, prep, str(tmp_path / "work"))

    report, out = vp.passthrough_roundtrip(prep, str(tmp_path / "rt.mkv"),
                                           work_dir=str(tmp_path / "rtwork"),
                                           log=lambda *_a, **_k: None)
    assert report.ok, report.warnings
    assert vp.count_frames(out) == len(MESSY)


# -- wiring into the Video Upscaler ------------------------------------------

def test_the_walk_queues_an_animated_gif_and_skips_a_static_one(tmp_path):
    """The static/animated split decides which TOOL owns the file, and a user should
    never have to know that it exists. Reaches for the real walk rather than the
    predicate, because the walk is where the decision is actually made."""
    import batch_video_upscale as bv
    src = tmp_path / "src"
    src.mkdir()
    _gif(src / "moving.gif", UNIFORM)
    Image.new("P", (40, 30), 1).save(str(src / "still.gif"))
    (src / "clip.mp4").write_bytes(b"")          # extension-only: the walk never opens it

    found = {os.path.basename(p) for p, _rel in bv.iter_videos(str(src))}
    assert "moving.gif" in found
    assert "still.gif" not in found, "a static GIF belongs to the Batch Upscaler"
    assert "clip.mp4" in found


def test_the_gif_marker_reaches_the_real_output_path():
    """`_output_path` owns naming for every caller (scan, adopt, estimate, the job), so
    the marker goes THERE rather than at the one call site that happens to be a GIF."""
    import batch_video_upscale as bv
    assert os.path.basename(bv._output_path("OUT", "logo.gif", "4K")) == \
        "logo_gif_4K.mp4"
    assert os.path.basename(bv._output_path("OUT", "logo.mp4", "4K")) == \
        "logo_4K.mp4"


def test_the_two_naming_rules_agree():
    """gif_video states the rule and batch_video_upscale applies it inside the naming
    it already owns (target, clip range, engine tag). Two implementations of one rule
    is exactly what drifts, so they share OUTPUT_MARKER and this pins the result."""
    import batch_video_upscale as bv
    assert os.path.basename(bv._output_path("", "holiday.gif", "1080p")) == \
        gv.output_name("holiday.gif", "1080p")


def test_the_matte_default_is_black_and_configurable():
    """Black because that is what a bare conversion already produces, so the default
    matches the measured behaviour instead of inventing a second one."""
    import batch_video_upscale as bv
    assert bv.resolve_video_cfg({"video": {}})["gif_matte"] == "black"
    assert bv.resolve_video_cfg({"video": {"gif_matte": "white"}})["gif_matte"] == \
        "white"
    # A blank or missing value must not produce an empty filter argument.
    assert bv.resolve_video_cfg({"video": {"gif_matte": ""}})["gif_matte"] == "black"


def test_conciliation_never_replaces_an_animated_gif(db_conn, tmp_path):
    """The decision that had to be made before any of this was built. An MP4 is NOT a
    superset of an animated GIF: looping is gone and transparency has been flattened
    onto a matte. Both are fine for a derived file the user asked for; neither is fine
    when the original is about to be archived or DELETED."""
    import conciliate as cc
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    _gif(orig / "logo.gif", UNIFORM)
    (proc / "logo_gif_4K.mp4").write_bytes(b"not really a video")

    plan, _folders, _kept, _variants, _raws, gifs = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=db_conn)
    assert plan == []
    assert [os.path.basename(p) for p in gifs] == ["logo.gif"]


def test_a_static_gif_is_still_conciliated_normally(db_conn, tmp_path):
    """The guard must be about ANIMATION, not about the extension: phase 1's static
    GIF -> PNG replacement is a genuine superset and has to keep working."""
    import conciliate as cc
    orig, proc = tmp_path / "orig", tmp_path / "proc"
    orig.mkdir()
    proc.mkdir()
    Image.new("P", (40, 30), 1).save(str(orig / "flat.gif"))
    Image.new("RGB", (160, 120)).save(str(proc / "flat_gif.png"))

    plan, _folders, _kept, _variants, _raws, gifs = cc.build_plan(
        str(orig), str(proc), tr_index=None, conn=db_conn)
    assert gifs == []
    assert [os.path.basename(o) for o, _p, _r in plan] == ["flat.gif"]


# -- the Settings field (D5/D6: reach for the effect, not the constant) -------

@pytest.fixture
def settings_tab():
    import tkinter.ttk as ttk
    from conftest import make_tk_root
    from gui.tab_settings import SettingsTab

    class _FakeApp:
        def refresh_tab_exclusivity(self): pass
        def mqtt_publish(self, *a, **k): pass
        def sync_settings_defaults(self): pass

    root = make_tk_root()
    tab = SettingsTab(ttk.Notebook(root), _FakeApp())
    root.update_idletasks()
    yield tab
    root.destroy()


def test_the_matte_setting_round_trips_through_save(settings_tab):
    """A field that DISPLAYS but maps back to the default on save is the silent failure
    this codebase has now been bitten by twice (D5, D6). `_video_section` is what Save
    actually writes, so the picked value has to come back out of it."""
    settings_tab.video_gif_matte_var.set("white")
    assert settings_tab._video_section()["gif_matte"] == "white"


def test_a_blank_matte_never_reaches_the_config(settings_tab):
    """The field is editable, not readonly, so it can be cleared. An empty colour would
    become an empty ffmpeg filter argument on the next animated GIF."""
    settings_tab.video_gif_matte_var.set("   ")
    assert settings_tab._video_section()["gif_matte"] == "black"


def test_the_matte_lives_on_the_video_upscaler_section(settings_tab):
    """Placement is a decision, not a detail: upscaling an animated GIF is a video
    upscaler's job, and whether a thing is "a video or a series of images" is a
    technical split a user should never have to hold to find the setting."""
    assert "gif_matte" in settings_tab._video_section()
    assert "gif_matte" not in settings_tab._collect()[0].get("upscale", {})


# -- the pointer out of the Batch Upscaler -----------------------------------

def test_an_animated_gif_skip_names_the_tool_that_can_do_it():
    """The Batch Upscaler still refuses an animated GIF, correctly: it makes images, not
    videos. But "would lose 5 of 6 frames" is right about the danger and useless about
    the remedy, and leaving it there leaks the app's own internal split at the user."""
    import batch_upscale as bu
    hint = bu.variant_next_step("holiday.gif", "would lose 5 of 6 frames")
    assert "Video Upscaler" in hint


def test_only_an_ANIMATED_gif_gets_the_pointer():
    """A transparent STATIC GIF has no other tab to be sent to: the Video Upscaler would
    refuse it too. Sending the user somewhere that also says no is worse than silence."""
    import batch_upscale as bu
    assert bu.variant_next_step("logo.gif", "would lose transparency") == ""
    assert bu.variant_next_step("scan.tif", "would lose 3 of 4 pages") == ""
    assert bu.variant_next_step("clip.webp", "would lose 3 of 4 frames") == ""
    # A transparent ANIMATED GIF is animated, so it does get the pointer.
    assert "Video Upscaler" in bu.variant_next_step(
        "both.gif", "would lose transparency, 5 of 6 frames")


@needs_ffmpeg
def test_the_scan_records_a_gifs_real_frame_count(tmp_path):
    """Needs ffmpeg despite the count itself coming from Pillow: `scan_file` returns None
    outright when `vp.probe` fails, so without it this asserts against a None row rather
    than against a frame count. (Found by CI the day Pillow was installed there and this
    module stopped being skipped whole.)

    A GIF container declares no frame count, so a metadata-only probe leaves it None
    and the tab reads `nb_frames or 0`: the scan list shows "?" and the cost estimate
    counts the job as ZERO frames. Under-reporting is the wrong direction for a number
    a "confirm before renting a pod" dialog quotes, and the right value is a header read
    away. It must be the SOURCE count (what the run actually pays), not the inflated
    CFR-normalised one."""
    import sqlite3
    import batch_video_upscale as bv
    import db as _db

    src = tmp_path / "src"
    src.mkdir()
    g = _gif(src / "a.gif", MESSY)                 # 10 frames, normalises to 38

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_db.SCHEMA)
    rid = _db.get_video_root_id(conn, str(src), str(tmp_path / "out"))

    row = bv.scan_file(conn, rid, g, "a.gif")
    assert row["nb_frames"] == len(MESSY)
    assert row["nb_frames"] != 38, "the CFR-normalised count is not what the run pays"
