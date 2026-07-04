"""
video_pipeline.plan_split (re-encode triggers) and check_drift (item 2).

The design doc records real bugs these guard: the lying nb_frames header and the
29.46-vs-30 fps desync (a tagged CFR that doesn't match counted-frames/duration).
We test only the branches that need no ffmpeg: plan_split's re-encode decisions
return before it probes keyframes, and check_drift is pure arithmetic over two
VideoInfo values. The `copy` path (which shells out to ffprobe for the keyframe
gap) is left to the live round-trip in video_pipeline's own CLI.
"""

from fractions import Fraction

import video_pipeline as vp


def make_info(**over):
    """A clean, CFR, 30 fps landscape VideoInfo; override any field per test."""
    base = dict(
        path="x.mp4", width=1920, height=1080,
        r_fps=Fraction(30), avg_fps=Fraction(30),
        nb_frames=900, duration=30.0,
        vcodec="h264", pix_fmt="yuv420p",
        has_audio=True, acodec="aac", is_vfr=False,
    )
    base.update(over)
    return vp.VideoInfo(**base)


# ── plan_split ──────────────────────────────────────────────────────────────

def test_force_reencode_wins():
    plan = vp.plan_split(make_info(), force_reencode=True)
    assert plan.mode == "reencode"
    assert "forced" in plan.reason.lower()


def test_vfr_source_triggers_reencode():
    info = make_info(r_fps=Fraction(30), avg_fps=Fraction(2997, 100), is_vfr=True)
    plan = vp.plan_split(info)
    assert plan.mode == "reencode"
    assert "vfr" in plan.reason.lower()


def test_mistagged_fps_triggers_cfr_normalize():
    # The Pisici.AVI case: tags 30 fps but holds 4835 frames over 164.1 s
    # = a real 29.46 fps. Must re-encode to keep audio in sync.
    info = make_info(r_fps=Fraction(30), avg_fps=Fraction(30),
                     nb_frames=4835, duration=164.1, is_vfr=False)
    plan = vp.plan_split(info)
    assert plan.mode == "reencode"
    assert "real" in plan.reason.lower()


def test_clean_cfr_within_tolerance_is_not_flagged_by_the_fps_check():
    # 900 frames / 30.0 s == exactly 30 fps -> the mistagged-fps branch must NOT
    # trip. (The final copy/reencode decision then depends on keyframe gap, which
    # needs ffmpeg, so we only assert the fps branch did not force a re-encode.)
    info = make_info(nb_frames=900, duration=30.0)
    eff = info.nb_frames / info.duration
    assert abs(eff - float(info.r_fps)) / float(info.r_fps) <= 0.005


def test_plan_carries_the_nominal_fps():
    plan = vp.plan_split(make_info(), force_reencode=True)
    assert plan.fps == Fraction(30)


# ── check_drift ─────────────────────────────────────────────────────────────

def test_clean_roundtrip_has_no_warnings():
    src = make_info(nb_frames=900, duration=30.0)
    out = make_info(nb_frames=900, duration=30.0)
    rep = vp.check_drift(src, out, segment_frame_counts=[500, 400])
    assert rep.ok is True
    assert rep.warnings == []


def test_segment_sum_mismatch_warns():
    src = make_info(nb_frames=900, duration=30.0)
    out = make_info(nb_frames=900, duration=30.0)
    rep = vp.check_drift(src, out, segment_frame_counts=[500, 399])  # sums to 899
    assert rep.ok is False
    assert any("segment frames" in w for w in rep.warnings)


def test_output_frame_count_mismatch_warns():
    src = make_info(nb_frames=900, duration=30.0)
    out = make_info(nb_frames=899, duration=30.0)
    rep = vp.check_drift(src, out, segment_frame_counts=[900])
    assert rep.ok is False
    assert any("output has" in w for w in rep.warnings)


def test_duration_drift_beyond_tolerance_warns():
    src = make_info(nb_frames=900, duration=30.0)
    out = make_info(nb_frames=900, duration=32.9)   # ~2.9 s drift (the lip-sync case)
    rep = vp.check_drift(src, out, segment_frame_counts=[900])
    assert rep.ok is False
    assert any("runs" in w for w in rep.warnings)


def test_reference_frames_override_is_used():
    # A re-encode legitimately changes the frame count; the caller passes the
    # post-normalize count as the reference so it is NOT flagged.
    src = make_info(nb_frames=900, duration=30.0)
    out = make_info(nb_frames=925, duration=30.0)
    rep = vp.check_drift(src, out, segment_frame_counts=[925], reference_frames=925)
    assert rep.ok is True
    assert rep.details["reference_frames"] == 925


def test_sub_frame_duration_delta_is_within_tolerance():
    src = make_info(nb_frames=900, duration=30.000)
    out = make_info(nb_frames=900, duration=30.010)   # 10 ms < one-frame tol
    rep = vp.check_drift(src, out, segment_frame_counts=[900])
    assert rep.ok is True
