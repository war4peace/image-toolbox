"""
OOM-recovery batch carry-forward: when the pod OOMs on one segment and recovers
to a smaller batch, the remaining segments of the SAME video should start at that
smaller batch instead of re-discovering it (a failed forward pass + VRAM churn) on
every segment. The decision is `batch_video_upscale.updated_learned_batch`, kept
pure so it can be tested apart from the heavy streaming loop.
"""

import batch_video_upscale as bv


def test_drop_learns_the_recovered_batch():
    # segment tried 33, OOM-recovered to 9 -> carry 9 forward
    assert bv.updated_learned_batch(None, 33, 9) == 9


def test_no_drop_leaves_learned_none():
    # pod resolved 33 and it fit (first == final) -> nothing learned yet
    assert bv.updated_learned_batch(None, 33, 33) is None


def test_forced_batch_that_survives_does_not_relearn():
    # a later segment forced to 9 reports first == final == 9 -> stays 9
    assert bv.updated_learned_batch(9, 9, 9) == 9


def test_keeps_the_smallest_safe_batch():
    # already carrying 9; a later segment drops further to 6 -> keep 6
    assert bv.updated_learned_batch(9, 9, 6) == 6
    # a shallower later drop never raises the learned floor back up
    assert bv.updated_learned_batch(6, 33, 9) == 6


def test_missing_stats_are_ignored():
    # the pod never reported a resolved batch (None/0) -> no change, no crash
    assert bv.updated_learned_batch(None, None, None) is None
    assert bv.updated_learned_batch(9, 0, 0) == 9
    assert bv.updated_learned_batch(None, 33, None) is None
