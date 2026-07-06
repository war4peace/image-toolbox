"""
Pure helpers of the segment-extractor GUI (section 16): the time<->frame and
keyframe-navigation math in gui.video_player, and the mark validation in
gui.video_segment_picker. These modules import tkinter at load (they are GUI
modules), so the whole file skips where tkinter is unavailable; the functions
themselves are dependency-free. libVLC is NOT required — load_vlc() returns False
and that path is exercised too.
"""

import pytest

pytest.importorskip("tkinter")           # the gui.* modules import tkinter at load

from gui import video_player as vp        # noqa: E402
from gui import video_segment_picker as sp  # noqa: E402


def test_load_vlc_is_fail_safe():
    # No python-vlc / libVLC in the test env: must report False, never raise.
    assert vp.load_vlc() in (True, False)


def test_time_frame_roundtrip():
    assert vp.time_to_frame(0.0, 30) == 0
    assert vp.time_to_frame(1.0, 30) == 30
    assert vp.time_to_frame(1.05, 30) == 32          # rounds to nearest
    assert vp.frame_to_time(30, 30) == pytest.approx(1.0)
    assert vp.frame_to_time(0, 30) == 0.0


def test_time_frame_bad_fps():
    assert vp.time_to_frame(5.0, 0) == 0
    assert vp.frame_to_time(5, 0) == 0.0


def test_nearest_keyframe_next_and_prev():
    kf = [0.0, 2.0, 4.0, 6.0]
    assert vp.nearest_keyframe(kf, 3.0, 1) == 4.0     # next after 3
    assert vp.nearest_keyframe(kf, 3.0, -1) == 2.0    # prev before 3
    # exactly on a keyframe -> the eps keeps it from returning itself
    assert vp.nearest_keyframe(kf, 2.0, 1) == 4.0
    assert vp.nearest_keyframe(kf, 2.0, -1) == 0.0


def test_nearest_keyframe_edges():
    kf = [0.0, 2.0, 4.0]
    assert vp.nearest_keyframe(kf, 5.0, 1) is None    # nothing after the last
    assert vp.nearest_keyframe(kf, 0.0, -1) is None   # nothing before the first
    assert vp.nearest_keyframe([], 1.0, 1) is None


def test_validate_marks():
    assert sp.validate_marks(1.0, 3.0, 10.0)[0] is True
    assert sp.validate_marks(None, 3.0, 10.0)[0] is False
    assert sp.validate_marks(3.0, 1.0, 10.0)[0] is False      # end before start
    assert sp.validate_marks(3.0, 3.0, 10.0)[0] is False      # zero length
    assert sp.validate_marks(1.0, 12.0, 10.0)[0] is False     # past the end
    assert sp.validate_marks(1.0, 10.3, 10.0)[0] is True      # within the 0.5s slack
