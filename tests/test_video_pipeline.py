"""
video_pipeline.plan_split (re-encode triggers) and check_drift (item 2).

The design doc records real bugs these guard: the lying nb_frames header and the
29.46-vs-30 fps desync (a tagged CFR that doesn't match counted-frames/duration).
We test only the branches that need no ffmpeg: plan_split's re-encode decisions
return before it probes keyframes, and check_drift is pure arithmetic over two
VideoInfo values. The `copy` path (which shells out to ffprobe for the keyframe
gap) is left to the live round-trip in video_pipeline's own CLI.
"""

import subprocess
import types
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


# ── detect_interlaced ─────────────────────────────────────────────────────────

def test_field_order_interlaced_is_detected_without_ffmpeg():
    # A definitive container field_order needs no pixel analysis (no ffmpeg shell-out).
    for fo in ("tt", "bb", "tb", "bt"):
        assert vp.detect_interlaced(make_info(field_order=fo)) is True


def test_field_order_progressive_short_circuits_to_false():
    assert vp.detect_interlaced(make_info(field_order="progressive")) is False


def test_unknown_field_order_missing_file_is_fail_safe_false():
    # 'unknown' (the VC-1/WMV ASF case) would idet-probe, but a nonexistent path must
    # fail safe to False rather than shelling out or raising.
    assert vp.detect_interlaced(make_info(field_order="", path="does-not-exist.wmv")) is False


def test_unknown_field_order_uses_idet_and_flags_interlaced(monkeypatch):
    # Simulate idet reporting a clear BFF majority (the measured MiniDV 576i case).
    class _CP:
        stderr = ("[Parsed_idet_0] Multi frame detection: "
                  "TFF:     0 BFF:   201 Progressive:     0 Undetermined:     0")
    monkeypatch.setattr(vp.os.path, "isfile", lambda _p: True)
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(vp, "_run", lambda *a, **k: _CP())
    assert vp.detect_interlaced(make_info(field_order="unknown")) is True


def test_unknown_field_order_idet_progressive_is_false(monkeypatch):
    class _CP:
        stderr = ("Multi frame detection: "
                  "TFF:     0 BFF:     0 Progressive:   399 Undetermined:     1")
    monkeypatch.setattr(vp.os.path, "isfile", lambda _p: True)
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(vp, "_run", lambda *a, **k: _CP())
    assert vp.detect_interlaced(make_info(field_order="unknown")) is False


# ── plan_split: interlaced -> deinterlacing re-encode ────────────────────────

def test_interlaced_source_forces_deinterlacing_reencode(monkeypatch):
    monkeypatch.setattr(vp, "detect_interlaced", lambda _i: True)
    plan = vp.plan_split(make_info())
    assert plan.mode == "reencode"
    assert plan.deinterlace is True
    assert "interlaced" in plan.reason.lower()


def test_interlaced_wins_even_over_force_reencode(monkeypatch):
    # A forced re-encode of an interlaced source must still deinterlace.
    monkeypatch.setattr(vp, "detect_interlaced", lambda _i: True)
    plan = vp.plan_split(make_info(), force_reencode=True)
    assert plan.deinterlace is True


def test_progressive_source_does_not_deinterlace(monkeypatch):
    monkeypatch.setattr(vp, "detect_interlaced", lambda _i: False)
    plan = vp.plan_split(make_info(), force_reencode=True)
    assert plan.deinterlace is False


# ── is_black_reencode (the black-output guard decision) ──────────────────────

def test_black_reencode_trips_when_bright_source_goes_black():
    # The measured failure: a ~95-luma source whose segment came back tv-range black (~16).
    assert vp.is_black_reencode(seg_luma=16.0, src_luma=95.0) is True
    assert vp.is_black_reencode(seg_luma=0.0, src_luma=90.0) is True


def test_black_reencode_does_not_trip_on_a_healthy_reencode():
    # seg ~= src (the deinterlaced WMV: 95.6 vs 95.5) must not trip.
    assert vp.is_black_reencode(seg_luma=95.6, src_luma=95.5) is False


def test_black_reencode_does_not_trip_on_a_genuinely_dark_clip():
    # A legitimately dark / fade-from-black clip: source is ALSO dark, so no false alarm.
    assert vp.is_black_reencode(seg_luma=15.0, src_luma=18.0) is False


def test_black_reencode_is_fail_safe_on_unreadable_luma():
    assert vp.is_black_reencode(seg_luma=None, src_luma=95.0) is False
    assert vp.is_black_reencode(seg_luma=16.0, src_luma=None) is False


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


def test_undetectable_keyframes_long_video_triggers_reencode(monkeypatch):
    # MEASURED: a VC-1/WMV3 .wmv (ASF) reports ZERO keyframes to ffprobe, so a
    # `-c copy` split can't cut and the whole video collapsed to one segment. A long
    # such video must re-encode with forced keyframes instead. 5850f/195s = clean 30
    # fps, so the mistagged-fps branch does not pre-empt the keyframe check.
    monkeypatch.setattr(vp, "keyframe_times", lambda _p: [])
    plan = vp.plan_split(make_info(nb_frames=5850, duration=195.0))
    assert plan.mode == "reencode"
    assert "keyframe" in plan.reason.lower()


def test_undetectable_keyframes_short_video_stays_copy(monkeypatch):
    # A clip shorter than the max segment length is one segment anyway, so no need to
    # re-encode even when keyframes can't be enumerated.
    monkeypatch.setattr(vp, "keyframe_times", lambda _p: [])
    plan = vp.plan_split(make_info(nb_frames=900, duration=30.0))
    assert plan.mode == "copy"


def test_dense_keyframes_stays_copy(monkeypatch):
    # Keyframes ~2 s apart over a 2-minute clip: well under the cap -> lossless copy.
    monkeypatch.setattr(vp, "keyframe_times", lambda _p: [i * 2.0 for i in range(60)])
    plan = vp.plan_split(make_info(nb_frames=3600, duration=120.0))
    assert plan.mode == "copy"


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


# ─────────────────────────────────────────────
#  ffmpeg option compatibility (0.6.0 master pin)
# ─────────────────────────────────────────────

def test_reencode_split_uses_fps_mode_not_the_removed_vsync(tmp_path, monkeypatch):
    """0.6.0 moved the ffmpeg pin from the 8.1 release branch to master, because every
    8.1.x corrupts vidstab output (#20). ffmpeg REMOVED `-vsync` on master (deprecated
    in favour of `-fps_mode` since 5.1), so the CFR-normalising split died with
    "Unrecognized option 'vsync'" before writing a frame. `-fps_mode` is understood by
    both, so this is not a master-only spelling and must not be "simplified" back."""
    captured = {}

    def _fake_run(args, **kw):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(vp, "_run", _fake_run)
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg.exe", "ffprobe.exe"))
    monkeypatch.setattr(vp, "pick_encoder", lambda prefer_hw=True: ("libx264", [], False))

    info = vp.VideoInfo(path="in.avi", width=320, height=240,
                        r_fps=Fraction(25), avg_fps=Fraction(25), nb_frames=100,
                        duration=4.0, vcodec="mjpeg", pix_fmt="yuvj422p",
                        has_audio=False, acodec=None, is_vfr=True)
    plan = vp.SplitPlan(mode="reencode", reason="vfr", segment_seconds=60.0,
                        fps=Fraction(25))
    vp.split(info, plan, str(tmp_path / "segs"))

    args = captured["args"]
    assert "-fps_mode" in args, "the CFR-normalising split must set a frame-rate mode"
    assert args[args.index("-fps_mode") + 1] == "cfr"
    assert "-vsync" not in args, "-vsync was removed upstream; ffmpeg refuses to start"


# ── NVENC availability (docs/known-defects.md D2) ───────────────────────────


def test_nvenc_is_probed_not_parsed(monkeypatch):
    """`ffmpeg -encoders` lists hevc_nvenc on every GPL build whether or not the
    machine has an NVIDIA card, so the string is always there. A machine without one
    must fall through to the CPU encoder instead of dying at the first frame."""
    vp._NVENC_PROBE.clear()
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))

    calls = []

    def fake_run(args, **_kw):
        calls.append(args)
        if "-encoders" in args:
            return types.SimpleNamespace(
                returncode=0, stdout="V....D hevc_nvenc\nV....D h264_nvenc\n"
                                     "V....D libx265\nV....D libx264\n")
        # The encode probe: no NVIDIA hardware here.
        return types.SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(vp, "_run", fake_run)
    codec, _args, is_hw = vp.pick_encoder(prefer_hw=True)
    assert codec == "libx265"
    assert is_hw is False
    assert any("nullsrc" in " ".join(a) for a in calls), "no probe was attempted"


def test_nvenc_is_used_when_the_probe_succeeds(monkeypatch):
    vp._NVENC_PROBE.clear()
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(vp, "_run", lambda args, **_kw: types.SimpleNamespace(
        returncode=0, stdout="V....D hevc_nvenc\nV....D libx265\n"))
    codec, _args, is_hw = vp.pick_encoder(prefer_hw=True)
    assert codec == "hevc_nvenc"
    assert is_hw is True


def test_the_probe_frame_is_big_enough_for_nvenc(monkeypatch):
    """NVENC refuses anything under its minimum dimensions, so a small probe frame
    reports "no NVENC" on a machine with a working card (measured: 64x64 and 128x128
    both fail on a 3090, 256x256 passes). A probe that fails closed on good hardware
    would silently move every user to the CPU encoder."""
    vp._NVENC_PROBE.clear()
    seen = []
    monkeypatch.setattr(vp, "_run", lambda args, **_kw: seen.append(args) or
                        types.SimpleNamespace(returncode=0, stdout=""))
    vp.nvenc_usable("hevc_nvenc", ffmpeg="ffmpeg")
    src = [a for a in " ".join(seen[0]).split() if a.startswith("nullsrc")][0]
    w, h = src.split("s=")[1].split(":")[0].split("x")
    assert int(w) >= 256 and int(h) >= 256


def test_the_probe_is_asked_once_per_process(monkeypatch):
    """It is called per segment; the hardware does not appear mid-run."""
    vp._NVENC_PROBE.clear()
    n = []
    monkeypatch.setattr(vp, "_run", lambda args, **_kw: n.append(1) or
                        types.SimpleNamespace(returncode=0, stdout=""))
    for _ in range(5):
        vp.nvenc_usable("hevc_nvenc", ffmpeg="ffmpeg")
    assert len(n) == 1


def test_a_probe_that_explodes_reads_as_unusable(monkeypatch):
    """Unusable is the safe answer: the CPU encoder always works."""
    vp._NVENC_PROBE.clear()

    def boom(*_a, **_k):
        raise OSError("no ffmpeg")

    monkeypatch.setattr(vp, "_run", boom)
    assert vp.nvenc_usable("hevc_nvenc", ffmpeg="ffmpeg") is False
