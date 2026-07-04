"""
Fit-math for the Batch Upscaler (item 2 in docs/improvement-recommendations.md).

`_skip_for_dims` (the skip rule) and `compute_seedvr2_resolution` (the portrait
short-side fix) are pure functions with real edge cases the comments call out.
The Resolution Target is user-configurable, so `batch_upscale` reads
MAX_RESOLUTION / RESOLUTION / UPSCALE_CUTOFF_PCT from config at import. These
tests therefore anchor to the module's live constants, not hard-coded 3840/2160,
so they pass whatever target the local config.json carries.
"""

import pytest

import batch_upscale as bu


MAX = bu.MAX_RESOLUTION
RES = bu.RESOLUTION


# ── _skip_for_dims ──────────────────────────────────────────────────────────

def test_skip_when_width_at_or_over_max():
    skip, reason = bu._skip_for_dims(MAX, 10, cutoff_pct=66)
    assert skip is True
    assert "width" in reason


def test_skip_when_height_at_or_over_target():
    skip, reason = bu._skip_for_dims(10, RES, cutoff_pct=66)
    assert skip is True
    assert "height" in reason


def test_small_image_below_cutoff_is_not_skipped():
    # A quarter of each axis is far below any sane cutoff — always upscale.
    skip, reason = bu._skip_for_dims(MAX // 4, RES // 4, cutoff_pct=66)
    assert skip is False
    assert reason == ""


def test_within_cutoff_is_skipped():
    # Just past 66% of the max width should trip the "close enough" cutoff.
    w = int(MAX * 0.66) + 5
    skip, reason = bu._skip_for_dims(w, 10, cutoff_pct=66)
    assert skip is True
    assert "cutoff" in reason


def test_cutoff_zero_disables_the_close_enough_rule():
    # With the cutoff disabled, a mid-size image is eligible (only the hard
    # per-axis limits still skip).
    w = int(MAX * 0.66) + 5
    skip, _ = bu._skip_for_dims(w, 10, cutoff_pct=0)
    assert skip is False


# ── compute_seedvr2_resolution ──────────────────────────────────────────────

def _expected_short_side(w, h):
    scale = min(MAX / w, RES / h)
    return min(round(w * scale), round(h * scale))


@pytest.mark.parametrize("w,h", [
    (600, 799),      # portrait — the case the fix exists for
    (1600, 1200),    # landscape 4:3
    (1080, 1920),    # tall portrait
    (4000, 400),     # extreme landscape
    (400, 4000),     # extreme portrait
])
def test_resolution_matches_min_of_scaled_axes(w, h):
    assert bu.compute_seedvr2_resolution(w, h) == _expected_short_side(w, h)


@pytest.mark.parametrize("w,h", [
    (600, 799), (1600, 1200), (1080, 1920), (3000, 2000), (2000, 3000),
])
def test_output_fits_inside_both_axis_limits(w, h):
    # The whole point of the short-side fix: neither output axis overshoots.
    scale = min(MAX / w, RES / h)
    out_w, out_h = round(w * scale), round(h * scale)
    assert out_w <= MAX + 1   # +1 tolerates rounding at the boundary
    assert out_h <= RES + 1


def test_docstring_examples_hold_at_default_target():
    # Concrete values only assert when the local config uses the shipped 4K target.
    if (MAX, RES) != (3840, 2160):
        pytest.skip(f"local Resolution Target is {MAX}x{RES}, not the default 4K")
    assert bu.compute_seedvr2_resolution(600, 799) == 1622
    assert bu.compute_seedvr2_resolution(1600, 1200) == 2160
    assert bu.compute_seedvr2_resolution(1080, 1920) == 1215
