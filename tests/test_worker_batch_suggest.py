"""
Item 9 (worker side): the pod returns a MEASURED-ANCHORED suggested_batch so the runner's
auto-tuner can seed + converge, and the OOM-recovery overlap is re-derived (not inherited)
so a hard batch drop can't collapse the stride. worker.py's module imports are stdlib-only
(the SeedVR2 engine loads lazily in main), so these pure functions test off the pod.
"""

from pod import worker as w

MODEL = "seedvr2_ema_7b_fp16.safetensors"


def _sb(out_w, out_h, vram, ran, measured, resident=True):
    return w._suggested_batch(out_w, out_h, vram, resident, MODEL, ran, measured)


# ── _suggested_batch (measured-anchored) ─────────────────────────────────────

def test_suggested_matches_max_vram_batch_when_measurement_matches_model():
    # Zero offset (measured == the model's own prediction at the ran batch) -> the
    # suggestion is just the VRAM ceiling, capped at the auto-path _BATCH_CAP.
    out_w, out_h, vram, ran = 3840, 2160, 48.0, 9
    mp = out_w * out_h / 1_000_000.0
    measured = w._ws_gb(mp, ran, MODEL, True)
    expected = min(w._max_vram_batch(out_w, out_h, vram, True, MODEL), w._BATCH_CAP)
    assert _sb(out_w, out_h, vram, ran, measured) == expected


def test_higher_measured_suggests_lower_batch_than_lower_measured():
    # More VRAM used than the model expected (tighter card) -> a smaller safe batch.
    out_w, out_h, vram, ran = 3840, 2160, 32.0, 9
    mp = out_w * out_h / 1_000_000.0
    base = w._ws_gb(mp, ran, MODEL, True)
    lean = _sb(out_w, out_h, vram, ran, base)                 # on-model
    heavy = _sb(out_w, out_h, vram, ran, base + 8.0)          # used 8 GB more than modelled
    assert heavy <= lean


def test_headroom_suggests_going_up_from_a_low_run():
    # Ran a low batch and used far LESS than budget -> suggest a higher batch (recover a
    # stale-low seed). Anchored so it reflects the real headroom, not the cold guess.
    out_w, out_h, vram, ran = 1920, 1080, 48.0, 5
    mp = out_w * out_h / 1_000_000.0
    tiny = w._ws_gb(mp, ran, MODEL, True) - 5.0               # measured 5 GB UNDER model
    assert _sb(out_w, out_h, vram, ran, max(1.0, tiny)) >= ran


def test_suggested_is_a_valid_4n1_batch_within_bounds():
    b = _sb(3840, 2160, 80.0, 13, 30.0)
    assert b is not None
    assert (b - 1) % 4 == 0                                   # 1,5,9,...
    assert w._BATCH_FLOOR <= b <= w._BATCH_CAP


def test_suggested_none_on_missing_inputs():
    assert _sb(0, 0, 48.0, 9, 20.0) is None                  # no dims
    assert _sb(3840, 2160, 48.0, 9, None) is None            # no measurement
    assert _sb(3840, 2160, 0.0, 9, 20.0) is None             # no vram


def test_suggested_is_monotonic_non_increasing_in_measured():
    out_w, out_h, vram, ran = 3840, 2160, 48.0, 13
    vals = [_sb(out_w, out_h, vram, ran, m) for m in (10.0, 20.0, 30.0, 40.0)]
    assert vals == sorted(vals, reverse=True)                # never suggests MORE as VRAM rises


# ── OOM-recovery overlap re-derive (seam floor, not inherited) ────────────────

def test_recovery_overlap_re_derives_to_the_seam_floor_not_the_inherited_value():
    # A big starting batch's auto-overlap is large; a hard OOM drop to a small batch used
    # to INHERIT it (min(inherited, smaller-1)), collapsing the stride (batch 9 + overlap 8
    # = stride 1). Re-deriving gives the seam-quality floor (6) => stride 3, not 1. Note the
    # floor is _MIN_OVERLAP=6 for seam invisibility, so it does NOT go to 2.
    assert w._auto_overlap(49) == 8                          # large batch -> large overlap
    assert w._auto_overlap(9) == 6                           # re-derived small: the seam floor
    assert w._auto_overlap(9) < w._auto_overlap(49)          # never inherits the larger value
