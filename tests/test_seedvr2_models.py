"""
The SeedVR2 DiT catalog (#26 Part A) and the three picklists that derive from it.

The whole point of scripts/seedvr2_models.py is that a weight cannot be offered in
one place and missing from another. Before it there were three hand-kept lists with
comments claiming they mirrored each other, and they had already drifted twice over:
`7b_fp8_e4m3fn_mixed_block35_fp16` was in the wizard but in NEITHER video list (while
`recommend_models` hands that exact weight to every 16 GB card), and the two lists
that did agree on contents disagreed on order.

So these tests are not "does the list have ten rows". They check the properties whose
violation is silent in the GUI:

  * every offered weight has a download pin, or the user picks a model that cannot
    be fetched and the run fails at load time with an engine-level message;
  * every pinned weight is offered, so a new pin is never stranded the way five were;
  * every recommendation the wizard can make is selectable everywhere, which is the
    exact bug that motivated the entry;
  * the labels fit the comboboxes that show them (a too-long label is truncated on
    screen, which is invisible to any test that only checks contents).
"""

import pytest

import seedvr2_models as sm
import upscale_engine
from gui import wizard_recommend as wr


# The combobox widths that actually render these labels. Kept as literals with their
# source so a widened/narrowed combobox is a deliberate edit here too.
WIDTH_SETTINGS = 40      # tab_settings._model_cb
WIDTH_WIZARD = 44        # gui/wizard.py DiT combobox
WIDTH_VIDEO = 22         # tab_video.method_combo


def test_every_offered_weight_has_a_download_pin():
    """A listed weight with no entry in upscale_engine._SEEDVR2_WEIGHTS cannot be
    pre-fetched or SHA-verified: ensure_seedvr2_weights skips it silently and the user
    only finds out when the engine fails to load it."""
    pinned = set(upscale_engine._SEEDVR2_WEIGHTS)
    for spec in sm.catalog():
        assert spec.filename in pinned, f"{spec.key} is offered but has no download pin"


def test_every_pinned_dit_is_offered():
    """The reverse direction, and the one that produced #26: five pinned, verified,
    already-plumbed weights were unreachable purely because nothing listed them."""
    vae = upscale_engine._SEEDVR2_DEFAULT_VAE
    pinned_dits = {f for f in upscale_engine._SEEDVR2_WEIGHTS if f != vae}
    assert pinned_dits == set(sm.filenames())


def test_catalog_rows_are_unique():
    keys = [m.key for m in sm.catalog()]
    files = sm.filenames()
    labels = [m.label for m in sm.catalog()]
    shorts = [m.short for m in sm.catalog()]
    for name, seq in (("key", keys), ("filename", files),
                      ("label", labels), ("short", shorts)):
        assert len(seq) == len(set(seq)), f"duplicate {name} in the catalog"


def test_default_model_is_in_the_catalog():
    assert sm.by_filename(sm.DEFAULT_MODEL) is not None
    assert sm.catalog()[0].filename == sm.DEFAULT_MODEL, \
        "the default should head the picklist (it is also the [0] fallback in tab_settings)"


def test_volume_cached_set_is_a_subset_of_the_catalog():
    """VOLUME_CACHED mirrors pod/provision.sh's DIT_MODEL_LIST. A name here that is not
    a real weight would make the recorded volume budget meaningless."""
    for name in sm.VOLUME_CACHED:
        assert sm.by_filename(name) is not None, f"{name} is not a catalog weight"


def test_volume_cached_set_still_fits_a_50gb_volume():
    """The measured reason DIT_MODEL_LIST is shorter than the catalog. The cached DiTs
    plus the three vision tiers (~11 GB) and the venv have to leave room on a 50 GB
    volume for ONE more model downloaded on first use; caching everything does not."""
    gib = 1 << 30
    cached = sm.total_bytes(sm.VOLUME_CACHED) / gib
    everything = sm.total_bytes(sm.filenames()) / gib
    assert cached < 30, f"cached DiT set grew to {cached:.1f} GiB; re-check the 50 GB budget"
    assert everything > 50, \
        "if the full catalog now fits a 50 GB volume, the reason for a short " \
        "DIT_MODEL_LIST is gone and provision.sh's comment needs revisiting"


# ── the three picklists ──────────────────────────────────────────────────────

def test_all_three_picklists_derive_from_the_catalog():
    """Not "they happen to match" but "they are the same object's projection". A future
    edit that reintroduces a literal list in any of the three fails here."""
    from gui import tab_settings, tab_video
    assert wr.SEEDVR_OPTIONS == sm.options()
    assert tab_settings._VIDEO_MODEL_OPTIONS == sm.options()
    assert tab_video._SEEDVR2_METHODS == sm.short_options()


def test_every_recommendation_is_selectable_in_every_list():
    """The motivating bug: recommend_models hands 7B FP8-mixed to every 16 GB card and
    that weight was absent from both video lists, so a 16 GB user was told to use a
    model for images that they could not pick for video."""
    from gui import tab_settings, tab_video
    settings_files = {f for _lbl, f in tab_settings._VIDEO_MODEL_OPTIONS}
    video_files = {f for _lbl, f in tab_video._SEEDVR2_METHODS}
    wizard_files = {f for _lbl, f in wr.SEEDVR_OPTIONS}
    for vram in (0, 4096, 8192, 12288, 16376, 24564, 32768, 49152, 98304):
        rec = wr.recommend_models(vram)
        assert rec.dit_model in wizard_files, f"{rec.dit_model} unreachable in the wizard"
        assert rec.dit_model in settings_files, f"{rec.dit_model} unreachable in Settings"
        assert rec.dit_model in video_files, f"{rec.dit_model} unreachable on the Video tab"


@pytest.mark.parametrize("width,attr", [(WIDTH_SETTINGS, "label"),
                                        (WIDTH_WIZARD, "label"),
                                        (WIDTH_VIDEO, "short")])
def test_labels_fit_their_combobox(width, attr):
    """A label wider than its combobox is truncated on screen, which no contents check
    sees. The Video tab's 22 characters is the tight one."""
    for spec in sm.catalog():
        text = getattr(spec, attr)
        assert len(text) <= width, \
            f"{spec.key}: {attr!r} is {len(text)} chars, combobox is {width}"


def test_labels_lead_with_the_trade_not_the_filename():
    """The tooltip rule: a bare 'Q4' tells a non-technical user nothing, and a raw
    filename tells them less. Every label opens with the family and carries a
    parenthesised reason to choose it."""
    for spec in sm.catalog():
        assert spec.label.startswith(("7B", "3B")), spec.label
        assert "(" in spec.label and spec.label.rstrip().endswith(")"), spec.label
        assert ".safetensors" not in spec.label and ".gguf" not in spec.label, spec.label


def test_label_for_still_resolves_a_known_and_an_unknown_model():
    """wizard.py and the tests use wr.label_for against the derived list."""
    assert wr.label_for(wr.SEEDVR_OPTIONS, sm.DEFAULT_MODEL).startswith("7B FP16")
    assert wr.label_for(wr.SEEDVR_OPTIONS, "not_a_model.safetensors") == "not_a_model.safetensors"


# ── the benchmark key must separate what the weights actually cost ───────────

def test_weights_with_different_footprints_get_different_bench_keys():
    """Listing the other five weights (#26 Part A) is what made this reachable: the
    learned-batch / video_bench key used to be family-only, so all SIX 7B variants shared
    one key while spanning 4.43 to 15.35 GiB of resident weights. A ceiling measured on Q4
    is higher, a learned value legitimately bypasses BATCH_CAP, so replaying it into an
    FP16 run OOMs. Part B (7B Q4 vs 3B FP16) is unmeasurable without this."""
    import video_vram_sizer as sizer
    by_tag = {}
    for spec in sm.catalog():
        by_tag.setdefault(sizer.model_tag(spec.filename), set()).add(spec.size_bytes)
    for tag, sizes in by_tag.items():
        assert len(sizes) == 1, \
            f"bench key {tag!r} covers weights of {sorted(sizes)} bytes: different ceilings"


def test_sharp_variants_share_their_twins_key_and_that_is_deliberate():
    """Same architecture, byte-identical file size, so the same VRAM ceiling. Splitting
    them would orphan rows for no measurement gain."""
    import video_vram_sizer as sizer
    for sharp_key, plain_key in (("7b_sharp_fp16", "7b_fp16"),
                                 ("7b_sharp_fp8_mixed", "7b_fp8_mixed"),
                                 ("7b_sharp_q4", "7b_q4")):
        sharp = next(m for m in sm.catalog() if m.key == sharp_key)
        plain = next(m for m in sm.catalog() if m.key == plain_key)
        assert sharp.size_bytes == plain.size_bytes
        assert sizer.model_tag(sharp.filename) == sizer.model_tag(plain.filename)


def test_the_two_historical_bench_keys_are_byte_identical():
    """Every row already in video_bench / video_batch_learn keeps its key. '7b' is 7B FP16
    (and the unknown-model fallback), '3b' is 3B Q8. Changing either silently orphans a
    user's measured data, some of it paid for on a rented pod."""
    import video_vram_sizer as sizer
    assert sizer.model_tag("seedvr2_ema_7b_fp16.safetensors") == "7b"
    assert sizer.model_tag("seedvr2_ema_3b-Q8_0.gguf") == "3b"
    assert sizer.model_tag("seedvr2_ema_3b_fp16.safetensors") == "3b_fp16"
    for unknown in ("", None, "some_custom_model.safetensors"):
        assert sizer.model_tag(unknown) == "7b", "an unknown model must err to the heaviest"


def test_bench_key_precision_agrees_with_the_catalog():
    """The sizer sniffs precision from the filename and the catalog records it as a field.
    Two encodings of the same fact, so pin them together rather than coupling the modules
    (the sizer stays import-free)."""
    import video_vram_sizer as sizer
    for spec in sm.catalog():
        tag = sizer.model_tag(spec.filename)
        expected = spec.family if spec.precision in ("fp16", "q8") else f"{spec.family}_{spec.precision}"
        if spec.family == "3b" and spec.precision == "fp16":
            expected = "3b_fp16"
        elif spec.family == "7b" and spec.precision == "q8":
            expected = "7b"
        assert tag == expected, f"{spec.key}: sizer says {tag!r}, catalog implies {expected!r}"


# ── the effect, not the list (D5/D6: a control can draw correctly and do nothing) ──

@pytest.fixture(scope="module")
def settings_tab():
    import tkinter.ttk as ttk
    from conftest import make_tk_root
    from gui.tab_settings import SettingsTab

    class _FakeApp:
        def refresh_tab_exclusivity(self): pass
        def mqtt_publish(self, *a, **k): pass
        def sync_settings_defaults(self): pass

    root = make_tk_root()
    tab = SettingsTab(ttk.Notebook(root), _FakeApp())
    root.update_idletasks()
    yield tab
    root.destroy()


def test_the_settings_combobox_really_shows_every_model(settings_tab):
    """Reads the realised widget, not the module constant. The whole entry is about a
    weight being present in the code and absent from the screen."""
    from gui import tab_settings
    settings_tab.video_engine_var.set(
        next(lbl for lbl, val in tab_settings._VIDEO_ENGINE_OPTIONS if val == "seedvr2"))
    settings_tab._sync_model_choices()
    shown = list(settings_tab._model_cb.cget("values"))
    assert shown == [m.label for m in sm.catalog()]


def test_picking_a_newly_listed_model_is_what_gets_saved(settings_tab):
    """The sharp end: a label that displays but maps back to the default on save would
    leave the user's choice silently ignored. Round-trip the two weights that were
    unreachable before this change and matter most (#26 Part B's 7B Q4, and the FP8
    mixed weight the wizard recommends to every 16 GB card)."""
    for filename in ("seedvr2_ema_7b-Q4_K_M.gguf",
                     "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors"):
        settings_tab.video_model_var.set(sm.by_filename(filename).label)
        assert settings_tab._video_section()["dit_model"] == filename


# ── the multi-model sweep (#26 Part A, the ESRGAN-shaped half) ────────────────

def test_resolve_models_defaults_to_one_and_never_to_all():
    """The costly default. Real-ESRGAN defaults to every tier because a tier probe takes
    seconds; a SeedVR2 sweep takes hours per model and, on a pod, is billed. Sweeping ten
    models because a default changed under somebody is a money bug, so "all" must be asked
    for explicitly."""
    import video_benchmark as vb
    vcfg = {"dit_model": "seedvr2_ema_3b-Q8_0.gguf"}
    assert vb.resolve_models(vcfg) == ["seedvr2_ema_3b-Q8_0.gguf"]
    assert vb.resolve_models(vcfg, []) == ["seedvr2_ema_3b-Q8_0.gguf"]
    assert len(vb.resolve_models(vcfg, vb.seedvr2_all_models())) == len(sm.catalog())


def test_resolve_models_drops_unknown_names_and_dedupes():
    """A typo must not reach a pod as a weight it will fail to download halfway through a
    paid sweep, and a name given twice must not be swept twice."""
    import video_benchmark as vb
    vcfg = {"dit_model": sm.DEFAULT_MODEL}
    got = vb.resolve_models(vcfg, ["seedvr2_ema_3b-Q8_0.gguf", "nope.safetensors",
                                   "seedvr2_ema_3b-Q8_0.gguf"])
    assert got == ["seedvr2_ema_3b-Q8_0.gguf"]


def test_resolve_models_returns_catalog_order():
    """Ordering is not cosmetic: the runner sweeps in this order, so it decides which model
    a user gets numbers for first if they Stop early."""
    import video_benchmark as vb
    picked = ["seedvr2_ema_3b-Q4_K_M.gguf", "seedvr2_ema_7b_fp16.safetensors"]
    got = vb.resolve_models({"dit_model": sm.DEFAULT_MODEL}, picked)
    assert got == ["seedvr2_ema_7b_fp16.safetensors", "seedvr2_ema_3b-Q4_K_M.gguf"]


def test_single_model_tokens_are_byte_identical_to_pre_063():
    """A one-model sweep must emit exactly the tokens an older build emitted. They are what
    a reopened window matches its saved rows against and what every BCELL/BPROBE/BCEILING
    echoes, so a decorated token would orphan every existing row."""
    import video_benchmark as vb
    assert vb.mode_token(sm.DEFAULT_MODEL, False, multi=False) == "off"
    assert vb.mode_token(sm.DEFAULT_MODEL, True, multi=False) == "on"


def test_multi_model_tokens_are_distinct_and_round_trip():
    import video_benchmark as vb
    toks = [vb.mode_token(m.filename, c) for m in sm.catalog() for c in (False, True)]
    assert len(toks) == len(set(toks)), "two regimes share a row token"
    for m in sm.catalog():
        for c in (False, True):
            tok = vb.mode_token(m.filename, c)
            assert vb.split_mode_token(tok) == (m.key, "on" if c else "off")


def test_split_mode_token_handles_the_legacy_bare_forms():
    """Rows saved before 0.6.3 carry a bare token; the GUI splits every token through this."""
    import video_benchmark as vb
    assert vb.split_mode_token("off") == (None, "off")
    assert vb.split_mode_token("on") == (None, "on")
    assert vb.split_mode_token("") == (None, "off")


def test_every_model_gets_its_own_bench_key():
    """The per-probe key. Two models sharing one would make a multi-model sweep resume the
    previous model's rungs and publish its seconds as this one's."""
    import video_benchmark as vb
    cfg = {"upscale": {}, "video": {}}
    import batch_video_upscale as bv
    base = bv.resolve_video_cfg(cfg)
    keys = {}
    for spec in sm.catalog():
        vcfg = {**base, "dit_model": spec.filename, "compile": False}
        eff = vb.effective_settings(cfg, vcfg, remote=True)
        keys.setdefault(vb.bench_key(vcfg, eff), []).append(spec.key)
    for key, specs in keys.items():
        sizes = {sm.by_filename(f).size_bytes
                 for f in sm.filenames() if sm.by_filename(f).key in specs}
        assert len(sizes) == 1, f"bench key {key!r} spans weights of {sorted(sizes)} bytes"


def test_the_benchmark_and_the_run_build_the_same_learn_key():
    """The pairing that must never drift. video_benchmark WRITES the learned batch under this
    key and batch_video_upscale READS it; if the two ever disagree a remote sweep's results
    are stored where no run will look, which looks exactly like a benchmark that did nothing.
    Both now carry the model family (#26 Part A)."""
    import video_vram_sizer as sizer
    import batch_video_upscale as bv
    import video_benchmark as vb
    gpu = "NVIDIA GeForce RTX 4090"
    for spec in sm.catalog():
        cfg = {"upscale": {}, "video": {"dit_model": spec.filename}}
        vcfg = bv.resolve_video_cfg(cfg)
        eff = vb.effective_settings(cfg, vcfg, remote=True)
        written = f"{gpu}|{sizer.model_tag(spec.filename)}" + sizer.learn_tag(eff)
        read = (f"{gpu}|{sizer.model_tag(vcfg.get('dit_model'))}"
                + sizer.learn_tag(bv._engine_flags(vcfg)))
        assert written == read, f"{spec.key}: benchmark writes {written!r}, run reads {read!r}"

