"""
Interrupted-split guard (recommendations item 1): a resumed video run must reuse the
split segments already in its work area ONLY when that split ran to completion. A split
killed mid-write (app closed, crash, power loss, the stall watchdog killing ffmpeg)
leaves a partial, often truncated segment set; reusing it blindly would upscale a
truncated deliverable at real pod cost, caught only by the soft end-of-run drift
warning. The decision is batch_video_upscale.split_is_complete; ensure_split discards an
incomplete split and re-splits. Both are exercised here without ffmpeg (split_is_complete
is pure; ensure_split's ffmpeg calls are monkeypatched).
"""

import json
import os

import batch_video_upscale as bv
import video_pipeline as vp


def _seg(in_dir, i, frames):
    return vp.Segment(index=i,
                      path=os.path.join(in_dir, f"seg_{i:05d}{vp.SEGMENT_EXT}"),
                      frame_count=frames)


def _write_marker(in_dir, count, frame_sum, mode="copy"):
    with open(os.path.join(in_dir, bv._SPLIT_MARKER), "w", encoding="utf-8") as f:
        json.dump({"segment_count": count, "frame_sum": frame_sum, "mode": mode}, f)


# ── split_is_complete (pure) ────────────────────────────────────────────────

def test_complete_split_is_accepted(tmp_path):
    d = str(tmp_path)
    segs = [_seg(d, 0, 100), _seg(d, 1, 100), _seg(d, 2, 40)]
    _write_marker(d, 3, 240)
    ok, why = bv.split_is_complete(d, segs)
    assert ok, why


def test_missing_marker_rejected(tmp_path):
    # an interrupted split (or a pre-marker dir) has no split.done -> reject
    d = str(tmp_path)
    segs = [_seg(d, 0, 100), _seg(d, 1, 100)]
    ok, why = bv.split_is_complete(d, segs)
    assert not ok
    assert "marker" in why


def test_truncated_segment_rejected(tmp_path):
    # marker recorded 240 frames but the last segment came back short (truncated file:
    # count_frames under-counts) -> reject rather than upscale a truncated deliverable
    d = str(tmp_path)
    segs = [_seg(d, 0, 100), _seg(d, 1, 100), _seg(d, 2, 5)]
    _write_marker(d, 3, 240)
    ok, why = bv.split_is_complete(d, segs)
    assert not ok
    assert "truncated" in why or "sum" in why


def test_missing_middle_segment_rejected(tmp_path):
    # seg_00001 missing: enumeration yields seg_00000 + seg_00002 as indices 0,1, so the
    # second file's name (seg_00002) != expected seg_00001 -> the gap is detected
    d = str(tmp_path)
    segs = [vp.Segment(0, os.path.join(d, f"seg_00000{vp.SEGMENT_EXT}"), 100),
            vp.Segment(1, os.path.join(d, f"seg_00002{vp.SEGMENT_EXT}"), 40)]
    _write_marker(d, 3, 240)
    ok, why = bv.split_is_complete(d, segs)
    assert not ok
    assert "gapless" in why or "hole" in why


def test_segment_count_mismatch_rejected(tmp_path):
    # only 2 segment files present but the split recorded 3 (the tail was lost) -> reject
    d = str(tmp_path)
    segs = [_seg(d, 0, 100), _seg(d, 1, 100)]
    _write_marker(d, 3, 200)
    ok, why = bv.split_is_complete(d, segs)
    assert not ok


# ── ensure_split (integration; ffmpeg calls monkeypatched) ───────────────────

def test_ensure_split_reuses_a_complete_split(tmp_path, monkeypatch):
    d = str(tmp_path)
    segs = [_seg(d, 0, 100), _seg(d, 1, 50)]
    _write_marker(d, 2, 150)
    monkeypatch.setattr(bv, "_enumerate_segments", lambda _d: segs)
    calls = {"split": 0}
    monkeypatch.setattr(bv.vp, "split",
                        lambda *a, **k: calls.__setitem__("split", calls["split"] + 1))
    out, mode = bv.ensure_split(object(), d,
                                {"segment_seconds": 60, "max_segment_seconds": 120})
    assert out is segs and mode == "reused"
    assert calls["split"] == 0                    # a complete set is never re-split


def test_ensure_split_discards_an_incomplete_split_and_re_splits(tmp_path, monkeypatch):
    d = str(tmp_path)
    # A partial split: a stale segment file is present but there is NO marker, so the
    # split was interrupted -> it must be discarded, cleared, and re-split.
    stale = [_seg(d, 0, 100)]
    open(stale[0].path, "wb").close()
    fresh = [_seg(d, 0, 100), _seg(d, 1, 50)]
    monkeypatch.setattr(bv, "_enumerate_segments", lambda _d: stale)
    monkeypatch.setattr(bv.vp, "plan_split",
                        lambda *a, **k: vp.SplitPlan("copy", "x", 60, vp.Fraction(30)))
    monkeypatch.setattr(bv.vp, "split", lambda info, plan, in_dir, **k: fresh)
    # The discard is developer detail, so it is recorded FILE-ONLY (out of the GUI terminal).
    logged = []
    monkeypatch.setattr(bv, "log_file_only", lambda m: logged.append(m))
    out, mode = bv.ensure_split(object(), d,
                                {"segment_seconds": 60, "max_segment_seconds": 120})
    assert out is fresh and mode == "copy"
    assert any("discarding an incomplete split" in m for m in logged)
    assert not os.path.exists(stale[0].path)      # the stale file was cleared
    # a fresh marker records the new split, so the next resume trusts it
    assert bv._read_split_marker(d) == {"segment_count": 2, "frame_sum": 150, "mode": "copy"}
