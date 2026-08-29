"""
seedvr2_models.py
-----------------
The SeedVR2 DiT catalog: which upscale models exist, what each one costs, and how
each is labelled in the GUI. Torch-free, tkinter-free, stdlib-only.

WHY THIS MODULE EXISTS (0.6.3, future-features #26 Part A). The same list of DiT
weights was written out THREE times -- the wizard's `SEEDVR_OPTIONS`, Settings'
`_VIDEO_MODEL_OPTIONS` and the Video tab's `_SEEDVR2_METHODS` -- each with a comment
asserting it mirrored the others. They had already drifted in two ways by the time
this was written:

  * `7b_fp8_e4m3fn_mixed_block35_fp16` was in the wizard but NOT in either video
    list, and `recommend_models` hands that exact weight to every 16 GB card. So a
    16 GB user was recommended a model for images that they could not select for
    video.
  * the two lists that did agree on contents disagreed on ORDER (Settings had 3B Q8
    before 3B FP16, the wizard the other way round).

Pinning three copies with a test would only DETECT the next drift. One catalog that
all three derive from cannot drift at all, which is the same reasoning (and the same
shape) as `esrgan_models.py`, whose catalog Settings and the Video tab already share.

WHAT IS OFFERED. `upscale_engine._SEEDVR2_WEIGHTS` hash-pins TEN DiT variants and the
shared VAE; every one of the ten is downloadable, SHA-256 verified and already plumbed
to the pod via `DIT_MODEL`. Before this module only five were reachable from the GUI,
purely because nothing listed the other five. All ten are listed here. Adding a model
is a row here and nothing else -- but its download pin must exist in
`upscale_engine._SEEDVR2_WEIGHTS` first, which `tests/test_seedvr2_models.py` enforces
in both directions.

NO NEW DEPENDENCY. `gguf` (the Q4/Q8 loader) arrives via `seedvr2/requirements.txt`,
which `bootstrap.ps1` installs for Local/Both; the quantised path has shipped since
0.4.6 (3B Q8 is the wizard's <=12 GB tier). The loader guards on `GGUF_AVAILABLE` and
fails with a named message, so a broken install reports itself rather than crashing.

THE VOLUME BUDGET IS A SEPARATE AXIS, and this is the thing not to conflate. What the
GUI OFFERS (this file) and what a RunPod network volume PRE-CACHES
(`pod/provision.sh`'s `DIT_MODEL_LIST`) are independent:

  * the configured `DIT_MODEL` is always appended to the provision download list and
    de-duplicated, so whatever the user picks is on the volume after a re-provision;
  * and a model picked WITHOUT a re-provision is downloaded to the volume on first
    use (`remote_run`'s health wait budgets 15 minutes for exactly this).

So listing all ten costs the volume nothing. Pre-caching all ten would cost 70.1 GiB
of DiT weights (70.5 with the shared VAE) against the 26.6 GiB the three cached tiers
take today, which does not fit a 50 GB volume alongside the vision models -- which is
why `DIT_MODEL_LIST` still caches three tiers and why `size_bytes` is recorded below.
See `pod/provision.sh`.
"""

# Measured 2026-08-23 from the HuggingFace `Content-Length` of each pinned file.
# Recorded because the numbers answer a real question (what fits on a 50 GB volume)
# and because a GUI can show them without a network call. They are file sizes, NOT
# VRAM figures: the measured wall on a big card is activations, roughly 80% of peak,
# not weights (docs/local-video-upscaler.md; future-features #26 Part B).
_GIB = 1 << 30


class DitSpec:
    """One catalog entry.

    `key`       short stable id (log-friendly, and how the tests name a row)
    `filename`  the weight file, and the value stored in config as `dit_model`
    `family`    "7b" | "3b"
    `precision` "fp16" | "fp8" | "q8" | "q4"
    `sharp`     the sharp-trained variant (crisper, more stylised)
    `label`     full picklist label, leading with the trade (Settings + wizard)
    `short`     compact label for the Video tab's narrow Method combobox
    """

    def __init__(self, key, filename, family, precision, sharp, size_bytes, label, short):
        self.key = key
        self.filename = filename
        self.family = family
        self.precision = precision
        self.sharp = bool(sharp)
        self.size_bytes = int(size_bytes)
        self.label = label
        self.short = short

    @property
    def size_gib(self):
        return self.size_bytes / _GIB

    def __repr__(self):                                # pragma: no cover (debug aid)
        return f"<DitSpec {self.key} {self.size_gib:.2f} GiB>"


# Ordered as the picklists read: the 7B family first (heaviest precision down to
# lightest), then the 3B family the same way. Family-grouped rather than strictly
# size-sorted on purpose -- by raw size 3B FP16 (6.32 GiB) sits between 7B FP8
# (7.88) and 7B Q4 (4.43), and interleaving the families reads as a jumble.
CATALOG = [
    DitSpec("7b_fp16", "seedvr2_ema_7b_fp16.safetensors", "7b", "fp16", False,
            16479334424,
            "7B FP16 (best detail, default)", "SeedVR2 / 7B FP16"),
    DitSpec("7b_sharp_fp16", "seedvr2_ema_7b_sharp_fp16.safetensors", "7b", "fp16", True,
            16479334424,
            "7B Sharp FP16 (crisper, stylized)", "SeedVR2 / 7B Sharp"),
    DitSpec("7b_fp8_mixed", "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors",
            "7b", "fp8", False, 8466296338,
            "7B FP8 mixed (7B quality, less VRAM)", "SeedVR2 / 7B FP8"),
    DitSpec("7b_sharp_fp8_mixed",
            "seedvr2_ema_7b_sharp_fp8_e4m3fn_mixed_block35_fp16.safetensors",
            "7b", "fp8", True, 8466296338,
            "7B Sharp FP8 mixed (crisper, less VRAM)", "SeedVR2 / 7B Sharp FP8"),
    DitSpec("7b_q4", "seedvr2_ema_7b-Q4_K_M.gguf", "7b", "q4", False, 4758306592,
            "7B Q4 (7B on a smaller card)", "SeedVR2 / 7B Q4"),
    DitSpec("7b_sharp_q4", "seedvr2_ema_7b_sharp-Q4_K_M.gguf", "7b", "q4", True,
            4758306592,
            "7B Sharp Q4 (crisper, smaller card)", "SeedVR2 / 7B Sharp Q4"),
    DitSpec("3b_fp16", "seedvr2_ema_3b_fp16.safetensors", "3b", "fp16", False, 6783018808,
            "3B FP16 (small, full precision)", "SeedVR2 / 3B FP16"),
    DitSpec("3b_q8", "seedvr2_ema_3b-Q8_0.gguf", "3b", "q8", False, 3660613984,
            "3B Q8 (small, more VRAM headroom)", "SeedVR2 / 3B Q8"),
    DitSpec("3b_fp8", "seedvr2_ema_3b_fp8_e4m3fn.safetensors", "3b", "fp8", False,
            3391544696,
            "3B FP8 (small, less VRAM than FP16)", "SeedVR2 / 3B FP8"),
    DitSpec("3b_q4", "seedvr2_ema_3b-Q4_K_M.gguf", "3b", "q4", False, 1995344224,
            "3B Q4 (smallest, lowest VRAM)", "SeedVR2 / 3B Q4"),
]

# The shipped default, and the fallback every consumer lands on for an unknown
# configured value. Kept here so the three GUI lists cannot disagree about it either.
DEFAULT_MODEL = "seedvr2_ema_7b_fp16.safetensors"

# The three DiTs pre-cached on a RunPod network volume by default. Mirrors
# pod/provision.sh's DIT_MODEL_LIST; see the module docstring for why that is a
# SHORTER list than the catalog and must stay one.
VOLUME_CACHED = (
    "seedvr2_ema_7b_fp16.safetensors",
    "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors",
    "seedvr2_ema_3b-Q8_0.gguf",
)


def catalog():
    """The catalog rows, in picklist order."""
    return list(CATALOG)


def filenames():
    """Every offered weight filename, in picklist order."""
    return [m.filename for m in CATALOG]


def by_filename(filename):
    """The DitSpec for a weight filename, or None for an unlisted/custom one."""
    for m in CATALOG:
        if m.filename == filename:
            return m
    return None


def options():
    """[(full label, filename)] for the Settings + wizard picklists."""
    return [(m.label, m.filename) for m in CATALOG]


def short_options():
    """[(short label, filename)] for the Video tab's narrow Method combobox."""
    return [(m.short, m.filename) for m in CATALOG]


def total_bytes(names):
    """Summed on-disk size of a set of weight filenames (unknown names ignored).
    Used to reason about the network-volume budget without a network call."""
    seen, total = set(), 0
    for n in names or ():
        spec = by_filename(n)
        if spec and n not in seen:
            seen.add(n)
            total += spec.size_bytes
    return total
