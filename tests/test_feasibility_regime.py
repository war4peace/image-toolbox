"""
The feasibility guard is scoped to the regime it was measured under (0.6.3).

`db.max_feasible_output_mp` is the ONLY way a card BELOW its target's VRAM floor gets
offered: `tab_video._seedvr2_gpu_ok` has no seed underneath it, unlike
`video_estimate.max_output_mp`. So an over-claim there is a card offered for a run that
then OOMs, billed if it is a pod. It was ignoring both halves of the key it should have
been reading.

  * MODEL. #26 Part A made ten weights selectable spanning 1.86 to 15.35 GiB, so a 4K
    probe under 3B-Q4 could qualify a card for a 7B-FP16 4K job. Reachable only since
    that change, and not yet realised: every probe in this project's corpus is 7B.
  * COMPILE, which is the half that already bites, and the numbers below are from this
    project's own database: an RTX 3090 proves 2.07 MP uncompiled and 1.23 MP compiled,
    and at 1920x1080 compiled it recorded ok=0 with an OOM at batch 5. Taking the max
    over both told a compiled run 1080p was proven when the card said otherwise.

The ordering is PARTIAL on purpose and a size-only rule would be wrong, which is what
most of these tests are about.
"""

import pytest

import db
import video_vram_sizer as sizer


# -- the model ordering ------------------------------------------------------

def test_the_heaviest_model_proves_every_other():
    """7B FP16 is the heaviest weight AND model_tag's unknown-model fallback, which is
    what makes this change free: every row in every existing corpus is '7b', so nothing
    needs migrating and no card loses a target it had."""
    for tag in ("7b", "7b_fp8", "7b_q4", "3b_fp16", "3b", "3b_fp8", "3b_q4"):
        assert sizer.model_outranks("7b", tag), tag


def test_a_lighter_model_never_proves_a_heavier_one():
    """The whole point. A 3B-Q4 probe reaching 4K says nothing about 7B FP16 at 4K."""
    assert not sizer.model_outranks("3b_q4", "7b")
    assert not sizer.model_outranks("7b_q4", "7b")
    assert not sizer.model_outranks("3b_fp8", "3b")


def test_the_order_is_partial_across_families_not_a_size_comparison():
    """The case a plain size comparison gets WRONG. 3b_fp16 (6.32 GiB) is HEAVIER than
    7b_q4 (4.43 GiB) but has SMALLER activations, so it cannot prove it. Sorting by bytes
    alone would let a 3B proof qualify a 7B job, which is the exact false positive this
    exists to stop."""
    w = sizer._tag_weights()
    assert w["3b_fp16"] > w["7b_q4"], "the premise: heavier by bytes"
    assert not sizer.model_outranks("3b_fp16", "7b_q4"), "but it must NOT prove it"
    # The other direction IS sound: 7B outranks 3B on both weights and activations.
    assert sizer.model_outranks("7b_q4", "3b_fp16")


def test_within_a_family_weights_order_it():
    """Same architecture means the same activations, so the lighter weight leaves strictly
    more VRAM for them."""
    assert sizer.model_outranks("7b", "7b_fp8")
    assert sizer.model_outranks("7b_fp8", "7b_q4")
    assert sizer.model_outranks("3b_fp16", "3b")
    assert sizer.model_outranks("3b", "3b_q4")


def test_every_tag_has_exactly_one_weight():
    """The table is derived from the catalog rather than written out, so it cannot drift.
    It is only well-defined because the two `sharp` variants are byte-identical to their
    twins, which is why they deliberately share a tag."""
    import seedvr2_models as sm
    seen = {}
    for spec in sm.catalog():
        tag = sizer.model_tag(spec.filename)
        seen.setdefault(tag, set()).add(spec.size_bytes)
    for tag, sizes in seen.items():
        assert len(sizes) == 1, f"{tag} maps to several sizes: {sizes}"


def test_an_unknown_tag_proves_nothing():
    """Refusing costs a card a target it might have reached; accepting costs a failed run
    and, on a pod, billed minutes."""
    assert not sizer.model_outranks("", "7b")
    assert not sizer.model_outranks("7b", "")
    assert not sizer.model_outranks("mystery", "7b")
    assert not sizer.model_outranks("7b", "mystery")


# -- the regime half ---------------------------------------------------------

def test_the_compile_state_must_match_exactly():
    """Compile moves the ceiling (measured 125 -> 53 at 540x720), so the two regimes are
    different measurements. A compiled proof probably covers an uncompiled run, but
    "probably" is not what a guard admitting a below-floor card should rest on, and
    requiring a match can only shrink the answer back toward the VRAM seed."""
    assert sizer.regime_qualifies("7b|c", "7b|c")
    assert not sizer.regime_qualifies("7b", "7b|c")
    assert not sizer.regime_qualifies("7b|c", "7b")


def test_the_model_order_still_applies_inside_a_regime():
    assert sizer.regime_qualifies("7b|c", "3b_q4|c")
    assert not sizer.regime_qualifies("3b_q4|c", "7b|c")


# -- the guard itself --------------------------------------------------------

def _probe(conn, gpu, model, w, h, outcome="ok"):
    db.record_bench_probe(conn, gpu, model, w, h, 5, outcome, frames=37, seconds=10.0)


def test_omitting_the_regime_keeps_the_old_behaviour(db_conn):
    """Backward compatible by construction: any caller that cannot know its regime, and
    every pre-0.6.3 one, sees exactly what it saw before."""
    _probe(db_conn, "CARD", "3b_q4", 3840, 2160)
    assert db.max_feasible_output_mp(db_conn, "CARD") == pytest.approx(8.29, abs=0.01)


def test_a_light_models_proof_does_not_qualify_a_heavy_job(db_conn):
    """The #26 Part A hole, stated as the scenario: benchmark 3B-Q4 at 4K on a card, then
    queue 7B FP16 at 4K on the same card."""
    _probe(db_conn, "CARD", "3b_q4", 3840, 2160)
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="7b") is None
    # ...and the light model itself is still allowed to use its own measurement.
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="3b_q4") == \
        pytest.approx(8.29, abs=0.01)


def test_the_3090_case_from_the_real_corpus(db_conn):
    """The one that already bites, with this project's own numbers. Uncompiled probes
    reach 1920x1080; compiled ones stop at 1280x960 and OOM at 1080p. Unscoped, the guard
    reported 2.07 MP to a COMPILED run."""
    for w, h in ((1280, 960), (1920, 1080)):
        _probe(db_conn, "RTX 3090", "7b", w, h)
    _probe(db_conn, "RTX 3090", "7b|c", 1280, 960)
    _probe(db_conn, "RTX 3090", "7b|c", 1920, 1080, outcome="oom")

    assert db.max_feasible_output_mp(db_conn, "RTX 3090") == pytest.approx(2.07, abs=0.01)
    assert db.max_feasible_output_mp(db_conn, "RTX 3090", regime="7b") == \
        pytest.approx(2.07, abs=0.01)
    assert db.max_feasible_output_mp(db_conn, "RTX 3090", regime="7b|c") == \
        pytest.approx(1.23, abs=0.01)


def test_an_oom_probe_is_never_proof(db_conn):
    _probe(db_conn, "CARD", "7b", 3840, 2160, outcome="oom")
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="7b") is None


def test_esrgan_probes_stay_excluded(db_conn):
    """Pre-existing rule, re-pinned because the filtering rewrote this query: a GAN tiles
    on OOM and reaches sizes SeedVR2 never could."""
    _probe(db_conn, "CARD", "esrgan-quality", 3840, 2160)
    assert db.max_feasible_output_mp(db_conn, "CARD") is None
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="7b") is None


def test_a_learned_row_counts_only_under_its_own_regime(db_conn):
    """The learned store is the second half of the proof and was over-claiming the same
    way. Its key is `card|model|regime`, so it scopes cleanly."""
    db_conn.execute("INSERT OR REPLACE INTO video_batch_learn (gpu_id, mp_key, batch,"
                    " updated_at) VALUES (?,?,?,?)",
                    ("CARD|3b_q4", 166, 5, "2026-08-29T10:00:00"))     # ~8.3 MP
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="3b_q4") is not None
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="7b") is None


def test_a_legacy_untagged_learned_row_proves_nothing_specific(db_conn):
    """A row the 0.6.3 migration could not attribute on evidence names no model, so it
    cannot prove a particular one. Dropping it costs a predictive cap and falls back to the
    VRAM seed; keeping it would be a guess in the direction that OOMs."""
    db_conn.execute("INSERT OR REPLACE INTO video_batch_learn (gpu_id, mp_key, batch,"
                    " updated_at) VALUES (?,?,?,?)",
                    ("CARD", 166, 5, "2026-08-29T10:00:00"))
    assert db.max_feasible_output_mp(db_conn, "CARD") is not None      # unscoped: as before
    assert db.max_feasible_output_mp(db_conn, "CARD", regime="7b") is None


def test_filtering_can_only_shrink_the_answer(db_conn):
    """The property that makes tightening safe to ship: every regime's answer is bounded by
    the unscoped one, so no card can gain a target from this change."""
    for model in ("7b", "7b|c", "3b_q4"):
        for w, h in ((1280, 960), (1920, 1080), (2560, 1440)):
            _probe(db_conn, "CARD", model, w, h)
    unscoped = db.max_feasible_output_mp(db_conn, "CARD")
    for regime in ("7b", "7b|c", "3b_q4", "3b", "7b_fp8"):
        scoped = db.max_feasible_output_mp(db_conn, "CARD", regime=regime)
        assert (scoped or 0) <= unscoped + 1e-9, regime
