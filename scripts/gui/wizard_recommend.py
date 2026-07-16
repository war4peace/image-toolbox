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

# SeedVR2 upscale DiT options: (label, dit_model filename), heaviest first so the
# picklist reads best-quality at the top down to lightest. The filenames mirror
# tab_settings._VIDEO_MODEL_OPTIONS and seedvr2's MODEL_REGISTRY.
SEEDVR_OPTIONS = [
    ("7B FP16 (best detail)",                 "seedvr2_ema_7b_fp16.safetensors"),
    ("7B FP16 Sharp (crisper)",               "seedvr2_ema_7b_sharp_fp16.safetensors"),
    ("7B FP8 mixed (7B quality, less VRAM)",  "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors"),
    ("3B FP16 (small, full precision)",       "seedvr2_ema_3b_fp16.safetensors"),
    ("3B Q8 (smallest, quantized)",           "seedvr2_ema_3b-Q8_0.gguf"),
]

# Ollama vision options: (label, model tag), heaviest first. These must be pulled
# (nothing auto-fetches them), which the wizard's pull step handles.
OLLAMA_OPTIONS = [
    ("qwen2.5vl:7b (richest captions)",  "qwen2.5vl:7b"),
    ("minicpm-v (balanced)",             "minicpm-v:latest"),
    ("gemma3:4b (lightest)",             "gemma3:4b"),
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

    Calibrated tiers (docs/first-start-wizard.md):
      * <= ~12 GB : 3B Q8            + gemma3:4b       (8/10/12 GB cards)
      *    16 GB  : 7B FP8 mixed     + minicpm-v
      *  >= 24 GB : 7B FP16          + qwen2.5vl:7b    (24 GB and above are equal)
    """
    gb = vram_mb_to_gb(vram_total_mb)
    if gb >= 24:
        return Recommendation("24GB+", gb,
                              "seedvr2_ema_7b_fp16.safetensors", "qwen2.5vl:7b")
    if gb >= 16:
        return Recommendation("16GB", gb,
                              "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors",
                              "minicpm-v:latest")
    return Recommendation("<=12GB", gb,
                          "seedvr2_ema_3b-Q8_0.gguf", "gemma3:4b")


def recommend_compile(vram_gb):
    """Advice on the optional torch.compile speedup for LOCAL video runs, by card size.

    compile makes local runs ~20-40% faster after the first (compiling) segment, but
    it raises VRAM use, so the largest safe batch shrinks. That trade tips on card
    size: a clear win on a big card (esp. long videos), marginal at 16 GB, and a
    likely NET LOSS on a small card where the smaller batch costs more than compile
    saves (see tab_settings' compile tooltip and gate_local_compile). This only
    guides the wizard's copy; runtime still gates compile on Triton + a real compiler.

    Takes rounded GB (as vram_mb_to_gb yields); 0 / no GPU -> not recommended.
    """
    if vram_gb >= 24:
        return CompileAdvice("recommended",
                             "A clear win on your card, especially on long videos.")
    if vram_gb >= 16:
        return CompileAdvice("optional",
                             "Marginal on 16 GB: the smaller batch can eat the gain. "
                             "Benchmark both ways if you care.")
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
