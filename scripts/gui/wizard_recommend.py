"""
gui/wizard_recommend.py
-----------------------
First-start Wizard: pure model-recommendation logic (0.4.6).

No tkinter, no torch, no nvidia-smi here. Given a detected total VRAM (MiB, as
system_telemetry.sample_gpu reports it), map to the SeedVR2 upscale DiT + Ollama
vision model that fit the card, per the calibrated tiers in
docs/first-start-wizard.md. Kept deliberately tkinter-free so it is unit-testable
stdlib-only; the wizard UI (gui/wizard.py) imports from here.

The recommendation is a SUGGESTION, not a gate: SeedVR2 offloads to system RAM, so
any supported card can run any model (just slower). The wizard pre-selects these
but always shows every option (SEEDVR_OPTIONS / OLLAMA_OPTIONS), so a user can
knowingly pick a heavier or lighter model.
"""
from collections import namedtuple

import seedvr2_models as _seedvr2_models

# SeedVR2 upscale DiT options: (label, dit_model filename), heaviest first so the
# picklist reads best-quality at the top down to lightest.
#
# DERIVED, not written out (0.6.3, #26 Part A). This list, tab_settings'
# _VIDEO_MODEL_OPTIONS and tab_video's _SEEDVR2_METHODS were three hand-kept copies
# that had already drifted apart in contents AND order; they now all come from the one
# torch-free catalog in seedvr2_models.py. Add a model there, not here.
SEEDVR_OPTIONS = _seedvr2_models.options()

# Ollama vision options: (label, model tag), heaviest first. These must be pulled
# (nothing auto-fetches them), which the wizard's pull step handles. The qwen3-vl
# family swept a 100-image Tag & Rename benchmark on a 3090 (docs/tag-and-rename.md):
# it beat qwen2.5vl:7b / minicpm-v / gemma3:4b on quality, speed, or both at every
# tier, and needs the tagging.ollama_num_ctx cap (config.json) to stay fast.
OLLAMA_OPTIONS = [
    ("qwen3-vl:8b-instruct (clearest captions)", "qwen3-vl:8b-instruct"),
    ("qwen3-vl:4b-instruct (balanced)",          "qwen3-vl:4b-instruct"),
    ("qwen3-vl:2b-instruct (lightest)",          "qwen3-vl:2b-instruct"),
    ("qwen2.5vl:7b (previous default)",          "qwen2.5vl:7b"),
]

# A recommendation: the tier label (for display), the rounded VRAM in GB used to
# pick it, and the two recommended model identifiers.
Recommendation = namedtuple("Recommendation", "tier vram_gb dit_model ollama_model")

# Advice for the optional torch.compile speedup (feature #7, local video). The
# verdict is one of "recommended" | "optional" | "not_recommended"; blurb is a short
# reason for the wizard to show. Kept here (pure) so it is unit-tested alongside the
# model tiers.
CompileAdvice = namedtuple("CompileAdvice", "verdict blurb")


def vram_mb_to_gb(vram_total_mb):
    """Round a MiB VRAM total to nominal GB. Rounding (not floor) is deliberate: a
    nominal 16 GB card reports ~16376 MiB (15.99 GB), which must still read as 16,
    and a 4090's 24564 MiB must read as 24. A None / non-positive input is 0."""
    if not vram_total_mb or vram_total_mb <= 0:
        return 0
    return round(vram_total_mb / 1024)


def recommend_models(vram_total_mb):
    """Map detected total VRAM (MiB) to the recommended (SeedVR2 DiT, Ollama model).

    Returns a Recommendation. A None / non-positive input yields the lowest tier as
    a safe default; the wizard handles "no GPU detected" separately (it offers the
    manual path rather than silently trusting this default).

    Calibrated tiers (docs/first-start-wizard.md; vision models: docs/tag-and-rename.md):
      * <= ~12 GB : 3B Q8            + qwen3-vl:2b-instruct   (8/10/12 GB cards)
      *    16 GB  : 7B FP8 mixed     + qwen3-vl:4b-instruct
      *  >= 24 GB : 7B FP16          + qwen3-vl:8b-instruct   (24 GB and above are equal)
    """
    gb = vram_mb_to_gb(vram_total_mb)
    if gb >= 24:
        return Recommendation("24GB+", gb,
                              "seedvr2_ema_7b_fp16.safetensors", "qwen3-vl:8b-instruct")
    if gb >= 16:
        return Recommendation("16GB", gb,
                              "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors",
                              "qwen3-vl:4b-instruct")
    return Recommendation("<=12GB", gb,
                          "seedvr2_ema_3b-Q8_0.gguf", "qwen3-vl:2b-instruct")


def recommend_compile(vram_gb):
    """Advice on the optional torch.compile speedup for LOCAL video runs, by card size.

    compile can speed up local runs after the first (compiling) segment, but it
    roughly doubles activation VRAM, so the largest safe batch shrinks. That trade
    tips on card size, and it tips LATER than first assumed: a measured 24 GB sweep
    (RTX 3090, 7B) showed compile winning only at sub-1080p outputs (+3-5%) and
    losing at every real 1080p-class target (24% slower up to a hard OOM above
    1440x1080), because the halved batch outweighs the per-frame gain right where it
    matters. So it only pays off on a card with enough VRAM headroom to absorb the
    batch halving at real targets (>=32 GB); at 24 GB it is a wash-to-loss, and a net
    loss on a smaller card (see tab_settings' compile tooltip and gate_local_compile).
    This only guides the wizard's copy; runtime still gates compile on Triton + a real
    compiler.

    Takes rounded GB (as vram_mb_to_gb yields); 0 / no GPU -> not recommended.
    """
    if vram_gb >= 32:
        return CompileAdvice("recommended",
                             "A clear win on your card, especially on long videos: "
                             "enough VRAM to keep a big batch even with compile on.")
    if vram_gb >= 16:
        return CompileAdvice("optional",
                             "A wash-to-loss at 24 GB: compile halves the batch at "
                             "real 1080p+ targets and the smaller batch eats the gain "
                             "(measured on a 3090). Benchmark both ways before committing "
                             "to the ~2-3 GB toolchain.")
    return CompileAdvice("not_recommended",
                         "On a smaller card the extra VRAM shrinks the batch, so it "
                         "often ends up slower. Leave it off unless you've measured a win.")


def label_for(options, model_id):
    """The human label paired with a model id in one of the *_OPTIONS lists, or the
    raw id if it is not a listed option (a user may have configured a custom one)."""
    for label, mid in options:
        if mid == model_id:
            return label
    return model_id
