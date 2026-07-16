"""
First-start Wizard (0.4.6): the pure model-recommendation tiers.

recommend_models() maps a card's total VRAM (MiB, as system_telemetry.sample_gpu
reports it) to the SeedVR2 DiT + Ollama vision model that fit it. This is the one
piece of the wizard with real branching logic, so it is pinned here across every
tier boundary and the awkward "reports slightly under nominal" cases (a 16 GB card
that says 16376 MiB, a 4090 that says 24564 MiB). Stdlib only: no GPU, no tkinter,
no torch, so it runs everywhere.
"""
import gui.wizard_recommend as wr


# ── vram_mb_to_gb: nominal rounding, not floor ───────────────────────────────

def test_vram_rounding_handles_under_nominal_reports():
    assert wr.vram_mb_to_gb(8192) == 8
    assert wr.vram_mb_to_gb(12288) == 12
    assert wr.vram_mb_to_gb(16376) == 16      # a "16 GB" card that reports 15.99 GB
    assert wr.vram_mb_to_gb(24564) == 24      # a 4090 reporting 23.98 GB
    assert wr.vram_mb_to_gb(32607) == 32      # a 5090 reporting 31.84 GB


def test_vram_none_or_zero_is_zero():
    assert wr.vram_mb_to_gb(None) == 0
    assert wr.vram_mb_to_gb(0) == 0
    assert wr.vram_mb_to_gb(-1) == 0


# ── recommend_models: the calibrated tiers ───────────────────────────────────

def test_low_tier_8_to_12gb_is_3b_q8_and_gemma():
    for mb in (8192, 10240, 11264, 12288):    # 8, 10, 11, 12 GB cards
        rec = wr.recommend_models(mb)
        assert rec.dit_model == "seedvr2_ema_3b-Q8_0.gguf"
        assert rec.ollama_model == "gemma3:4b"


def test_16gb_tier_is_7b_fp8_mixed_and_minicpm():
    rec = wr.recommend_models(16376)          # nominal 16 GB
    assert rec.dit_model == "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors"
    assert rec.ollama_model == "minicpm-v:latest"
    assert rec.tier == "16GB"


def test_16gb_lower_boundary_is_exactly_16():
    # 15 GB stays in the low tier; a true 16 GB card (even reporting a touch under)
    # crosses into the 16 GB tier.
    assert wr.recommend_models(15360).tier == "<=12GB"   # 15 GB -> low
    assert wr.recommend_models(16384).tier == "16GB"     # 16 GB -> mid


def test_24gb_tier_is_7b_fp16_and_qwen():
    rec = wr.recommend_models(24576)          # 3090 / 4090
    assert rec.dit_model == "seedvr2_ema_7b_fp16.safetensors"
    assert rec.ollama_model == "qwen2.5vl:7b"
    assert rec.tier == "24GB+"


def test_32gb_and_above_matches_24gb_recommendation():
    # 24 GB and 32 GB+ resolve to the same models (only the pre-selection differs
    # by nothing); both get the full 7B FP16 + qwen2.5vl:7b pair.
    for mb in (32607, 49152, 81920):          # 32, 48, 80 GB cards
        rec = wr.recommend_models(mb)
        assert rec.dit_model == "seedvr2_ema_7b_fp16.safetensors"
        assert rec.ollama_model == "qwen2.5vl:7b"


def test_none_vram_falls_back_to_lowest_tier():
    rec = wr.recommend_models(None)
    assert rec.dit_model == "seedvr2_ema_3b-Q8_0.gguf"
    assert rec.ollama_model == "gemma3:4b"
    assert rec.vram_gb == 0


# ── the recommended models are real options in the picklists ─────────────────

def test_every_recommended_model_is_a_listed_option():
    seed_ids = {mid for _, mid in wr.SEEDVR_OPTIONS}
    oll_ids = {mid for _, mid in wr.OLLAMA_OPTIONS}
    for mb in (None, 8192, 16384, 24576, 40960):
        rec = wr.recommend_models(mb)
        assert rec.dit_model in seed_ids, f"{rec.dit_model} not in SEEDVR_OPTIONS"
        assert rec.ollama_model in oll_ids, f"{rec.ollama_model} not in OLLAMA_OPTIONS"


# ── recommend_compile: the torch.compile speedup advice by card size ─────────

def test_compile_recommended_on_big_cards():
    for gb in (24, 32, 48, 80):
        adv = wr.recommend_compile(gb)
        assert adv.verdict == "recommended"
        assert adv.blurb                      # non-empty reason for the wizard to show


def test_compile_optional_at_16gb():
    assert wr.recommend_compile(16).verdict == "optional"
    assert wr.recommend_compile(23).verdict == "optional"   # just under the big-card line


def test_compile_not_recommended_on_small_or_no_gpu():
    for gb in (0, 8, 10, 12, 15):
        assert wr.recommend_compile(gb).verdict == "not_recommended"


def test_compile_boundaries_match_the_model_tiers():
    # 16 is the first "optional" GB; 24 the first "recommended" GB, same cut points
    # the model recommendation uses, so the two steps tell a consistent story.
    assert wr.recommend_compile(15).verdict == "not_recommended"
    assert wr.recommend_compile(16).verdict == "optional"
    assert wr.recommend_compile(23).verdict == "optional"
    assert wr.recommend_compile(24).verdict == "recommended"


def test_label_for_returns_label_or_raw_id():
    assert wr.label_for(wr.OLLAMA_OPTIONS, "qwen2.5vl:7b").startswith("qwen2.5vl:7b")
    assert wr.label_for(wr.SEEDVR_OPTIONS, "seedvr2_ema_3b-Q8_0.gguf").startswith("3B Q8")
    # An unknown / custom id passes through unchanged.
    assert wr.label_for(wr.OLLAMA_OPTIONS, "custom:model") == "custom:model"
