"""
Item 6: the video runner's slow-segment health signal (notify-only).

The longest, most expensive runs (video) had no counterpart to the image watchdog. A
segment running far slower than the run's healthy rate (rare shared-infra contention on
the pod, invisible from the guest) just kept billing. VideoSlowWatch anchors to the
run's BEST seconds-per-output-megapixel (a slow creep can't drift the baseline up) and
warns, edge-triggered, at >= factor x that baseline. It NEVER auto-stops: contention
usually clears and killing a half-done segment wastes its cost. Pure, so tested like
funds_guard.
"""

import batch_video_upscale as bv


def _watch(factor=3.0, min_samples=2):
    return bv.VideoSlowWatch(factor=factor, min_samples=min_samples)


def test_first_samples_establish_baseline_without_tripping():
    w = _watch(min_samples=2)
    # A slow sample as the very first one can't trip (no trusted baseline yet).
    assert w.observe(seconds=100.0, out_megapixels=1.0) == (False, 1.0)
    warn, ratio = w.observe(seconds=10.0, out_megapixels=1.0)   # this one is the baseline
    assert warn is False and ratio == 1.0                       # it lowered min to 10 spmp


def test_slow_segment_warns_once_then_re_arms():
    w = _watch(factor=3.0, min_samples=1)
    w.observe(10.0, 1.0)                      # baseline: 10 s/MP
    warn, ratio = w.observe(40.0, 1.0)        # 40 s/MP = 4x -> leading edge
    assert warn is True and ratio == 4.0
    # still slow, but we've already warned this episode -> no repeat
    warn2, _ = w.observe(50.0, 1.0)
    assert warn2 is False
    # back to healthy -> episode ends (re-arm)
    warn3, _ = w.observe(11.0, 1.0)
    assert warn3 is False
    # slow again -> a NEW episode warns
    warn4, ratio4 = w.observe(45.0, 1.0)
    assert warn4 is True and ratio4 == 4.5


def test_just_under_factor_does_not_warn():
    w = _watch(factor=3.0, min_samples=1)
    w.observe(10.0, 1.0)
    warn, ratio = w.observe(29.0, 1.0)        # 2.9x < 3x
    assert warn is False and ratio == 2.9


def test_baseline_anchors_to_minimum_not_average():
    # A slow creep must not raise the baseline and hide a real slowdown.
    w = _watch(factor=3.0, min_samples=1)
    w.observe(10.0, 1.0)                      # min = 10
    w.observe(15.0, 1.0)                      # 1.5x, not slow; min stays 10 (not averaged up)
    w.observe(20.0, 1.0)                      # 2.0x, still not slow; min stays 10
    warn, ratio = w.observe(31.0, 1.0)        # 3.1x vs the true min of 10 -> warns
    assert warn is True and ratio == 3.1


def test_per_megapixel_normalisation_is_aspect_and_size_fair():
    # Same GPU health, different segment sizes: a bigger segment takes proportionally
    # longer, so per-MP the rate is identical and it must NOT look slow.
    w = _watch(factor=3.0, min_samples=1)
    w.observe(20.0, 2.0)                      # 10 s/MP
    warn, ratio = w.observe(80.0, 8.0)        # also 10 s/MP (4x frames, 4x time)
    assert warn is False and ratio == 1.0


def test_zero_or_missing_timing_is_ignored():
    # A passthrough/local segment with no engine timing must never trip the watch.
    w = _watch(min_samples=1)
    assert w.observe(None, 1.0) == (False, 0.0)
    assert w.observe(10.0, 0.0) == (False, 0.0)
    assert w.samples == 0                     # ignored inputs don't advance the baseline


def test_disabled_via_config():
    vcfg = bv.resolve_video_cfg({"upscale": {"watchdog_enabled": False}})
    assert vcfg["watchdog_enabled"] is False


def test_video_override_beats_upscale_default():
    vcfg = bv.resolve_video_cfg({"upscale": {"watchdog_factor": 3.0},
                                 "video": {"watchdog_factor": 5.0}})
    assert vcfg["watchdog_factor"] == 5.0
