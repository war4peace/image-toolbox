# First-start Wizard (0.4.6)

A one-time onboarding wizard shown the first time a new user launches the app.
It detects the local GPU and recommends the SeedVR2 upscale model and the Ollama
vision model that best fit the card's VRAM, so a non-technical user gets a sane,
fast configuration without knowing what "7B fp16" or "qwen2.5vl" mean.

Status: IMPLEMENTED (0.4.6-experimental). The wizard, its tiers, the Ollama
one-click pull, the remote-tab router, the Settings re-run button, the bootstrap
pre-pull removal, and the Tag & Rename pull-on-Start safety net are all built and
tested (full suite 330). Remaining before release: the module-table / feature-list
/ README updates on the release checklist.

## Decisions (agreed)

1. **Ollama model: recommend + offer to pull.** SeedVR2 weights auto-download
   lazily on the first upscale, so switching `upscale.dit_model` before any run is
   free. Ollama models are the opposite: nothing auto-fetches them, they must be
   `ollama pull`ed. So the wizard checks `/api/tags` for the recommended model and,
   if absent, offers a one-click pull with a progress bar (Ollama must be running).
2. **Remote-only installs skip GPU detection.** `get_install_mode() == "remote"`
   means there is no local GPU stack, so model-by-VRAM is meaningless. Those users
   instead get the existing onboarding path (SSH key setup, model-volume
   provisioning); per-run model choice already happens via the live GPU picker.
3. **Suggest + pre-select, never hard-gate.** SeedVR2 block-swaps/offloads to
   system RAM, so a small card CAN run 7B fp16 (just slowly, and on a 3090 that is
   the exact path into the GPU-degradation bug). The wizard pre-selects the
   sweet-spot model for the detected VRAM but shows every option with a note, so
   the user can knowingly pick a heavier/lighter model.

## What already exists (reuse, do not rebuild)

- **GPU detection:** `system_telemetry.sample_gpu()` returns
  `(vram_used_mb, vram_total_mb, temp_c)`; `system_telemetry.gpu_name()` returns
  the card name. Both `nvidia-smi`, both fail safe to `None`, no new dependency.
- **Install mode:** `gui.common.get_install_mode()` returns `local|remote|both`.
- **Model catalog:** the vendored `seedvr2/src/utils/model_registry.py`
  `MODEL_REGISTRY` is the source of truth for available DiT files and their
  precision/size (3B/7B, fp16/fp8/GGUF, plus 7B "sharp" variants).
- **Settings model pickers:** `gui/tab_settings.py` already has a labelled video
  model picklist (`7B FP16` / `7B Sharp` / `3B Q8` / `3B FP16`) and an Ollama model
  combobox populated from `/api/tags`. The wizard writes the same config keys these
  save: `upscale.dit_model`, `video.dit_model`, `ollama.model`.
- **Config write path:** `config_store.save(cfg, APP_ROOT)` (keeps secrets in the
  untracked overlay). The wizard must go through this, not hand-write `config.json`.

## Trigger and the first-run flag

There is currently **no** first-run flag anywhere (`gui_settings.json` holds only
window geometry). Add `wizard_done: true` to `gui_settings.json` (correct home:
GUI-only state, not tracked config).

- On `App` startup, if `wizard_done` is not set, show the wizard as a modal
  `Toplevel` after the main window builds.
- The wizard always writes `wizard_done = True` on finish OR skip, so it never
  reappears. A Settings entry ("Re-run first-start wizard...") can clear the flag
  for anyone who wants it again.
- Fail safe: any error building/detecting inside the wizard just sets the flag and
  closes, so a broken wizard can never block the app from launching.

## Recommendation tiers (calibrated)

The recommendation targets the largest model that runs **mostly resident** (little
offload) on the card, so it is fast and avoids the degradation path. Base tier on
`vram_total_mb`; the 4K target is the worst case (2K/1080p need less, so a card is
never under-served by sizing for 4K). Boundaries below are the author's calibrated
values.

Single tier table (both models keyed on the same detected VRAM):

| Detected VRAM | SeedVR2 DiT (`upscale.dit_model` / `video.dit_model`) | Ollama (`ollama.model`) |
|---|---|---|
| 8 GB (min supported) | `seedvr2_ema_3b-Q8_0.gguf` | `gemma3:4b` |
| 10-12 GB | `seedvr2_ema_3b-Q8_0.gguf` | `gemma3:4b` |
| 16 GB | `seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors` | `minicpm-v:latest` |
| 24 GB (3090/4090) | `seedvr2_ema_7b_fp16.safetensors` (current default) | `qwen2.5vl:7b` |
| 32 GB+ | `seedvr2_ema_7b_fp16.safetensors` (or `_sharp_fp16`) | `qwen2.5vl:7b` |

Notes:
- The two lowest tiers share the same models (3B Q8 + gemma3:4b): an 8 GB card and
  a 12 GB card both want the lightweight pair.
- Tag & Rename and upscaling never run at the same time, so the Ollama tier assumes
  the whole card is free during tagging, not just what SeedVR leaves.
- SeedVR "sharp" and GGUF quantized options stay visible as secondary picks (the
  full picklist is always shown, per decision 3); only the pre-selection is tiered.

## Wizard flow

1. **Welcome** (all install modes): one line on what the wizard does + Skip.
2. **GPU detection** (local/both only): show detected card + VRAM, or a clear
   "No NVIDIA GPU detected" if `sample_gpu()` is `None` (offer the manual path).
3. **Model recommendation** (local/both, GPU present): pre-select the tiered
   SeedVR2 + Ollama models, show the full list with the "you can pick heavier, it
   will be slower" note. Writes `upscale.dit_model` / `video.dit_model` /
   `ollama.model`.
4. **Ollama model fetch** (if the recommended model is not in `/api/tags`): offer
   one-click pull with progress; skippable (Tag & Rename will warn until pulled).
   A small state machine: checking -> present | missing | no_ollama | unreachable,
   and missing --Download--> pulling -> pulled | error. Navigation stays enabled
   during a pull (it continues server-side; the `_ui()` guard keeps a late callback
   off a closed wizard).
5. **Remote path:** rather than duplicate the RunPod tab's stateful SSH/volume UI,
   the remote step ROUTES to it: an "Open the RunPod tab" button selects that tab
   and closes the wizard. For remote-only it is the main path (replacing steps 2-4);
   for a **both** install it is an optional final step. Remote-only installs do NOT
   have their model config rewritten (the GPU/model steps are skipped, so the
   shipped 7B FP16 + qwen2.5vl:7b defaults are left for the big pod GPU the user
   picks per-run).
6. **Finish:** write config via `config_store.save` (through `gui.common.save_config`),
   set `wizard_done`.

The wizard does NOT touch the Resolution Target: it stays at the 4K default (the
tiers already size for 4K, so no separate choice is needed here).

## Files touched (as built)

- `scripts/gui/wizard_recommend.py` (new): the pure, tkinter-free tier logic
  (`recommend_models`, `vram_mb_to_gb`, the SEEDVR/OLLAMA option lists, `label_for`).
- `scripts/gui/wizard.py` (new): the `FirstStartWizard` `Toplevel`, its steps, the
  off-thread GPU detection and Ollama pull, the remote-tab router, `should_show`.
- `scripts/gui/common.py`: `wizard_completed` / `mark_wizard_completed` (the
  `wizard_done` flag), plus the Ollama helpers `ollama_model_present`
  (+ `_ollama_tag_matches`) and the streaming `ollama_pull`.
- `scripts/gui/app.py`: `after(600, self._maybe_show_wizard)` startup hook + the
  fail-safe `_maybe_show_wizard`.
- `scripts/gui/tab_settings.py`: the "Setup" section with the
  "Re-run first-start wizard" button (`_rerun_wizard`).
- `tests/test_wizard_recommend.py` (10) + `tests/test_wizard_ollama.py` (5): pin the
  tier logic and the Ollama presence matcher (stdlib only, no GPU / server / tkinter).
- Docs: this file + a feature-list entry in `CLAUDE.md` / `README.md` at release.

## Follow-up: stopped the GPU-blind model pre-download (DONE)

The **hardcoded `ollama pull qwen2.5vl:7b`** block was removed from `bootstrap.ps1`
(it ran during first-launch setup, before any GPU-aware recommendation, pushing a
weak-GPU machine to download a model it cannot run well). Ollama is still installed;
bootstrap now just notes that the first-start Wizard recommends and downloads a
fitting model. The closing message was updated to match.

The SeedVR2 weights needed no bootstrap change: they already download lazily on the
first upscale from whatever `upscale.dit_model` holds, and the wizard runs on first
app start (before any upscale), so the correctly-tiered (often much smaller than 7B
fp16) model is what gets fetched.

### Safety net: pull-on-Start in Tag & Rename (DONE)

Because the wizard's pull can be skipped or fail, a LOCAL Tag & Rename run now
verifies the configured Ollama model is installed the moment the user clicks Start
(Ollama does not auto-pull, so a missing model would just fail the run):
`TagTab._ensure_ollama_model()` checks `/api/tags` and, if the model is missing,
offers a modal `OllamaPullDialog` (in `gui/dialogs.py`, reusing `common.ollama_pull`)
before launching. It is fail-open when the server can't be reached (the runner
reports Ollama problems itself) and covered by `tests/test_tag_ensure_ollama.py`.

## Resolved decisions (were open questions)

1. VRAM tier boundaries: calibrated (see the tier table above).
2. Default Resolution Target: NOT set by the wizard; stays at 4K.
3. Remote setup on a **both** install: offered as an optional final step.
