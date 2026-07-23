# Tag & Rename — vision model notes

Design + as-built notes for the **Tag & Rename** tool: how it uses a local Ollama
vision model, and the model + context-window choices that back the shipped
defaults. The raw measurements live in
[`tag-rename-benchmarks.csv`](tag-rename-benchmarks.csv) (updated by hand as new
models are tested; the `.xlsx` copy was dropped, the CSV is the single source).

## What the tool asks the model to do

Tag & Rename sends each photo to Ollama and asks for exactly two lines: a 20-40
word natural-language description (written into EXIF) and a condensed 4-5 word
English title (used for the filename). It is a **describe-and-title** job, not OCR.
Two properties of the request shape which models work well:

- The image is **downscaled to a max longest edge** (`tagging.max_image_px`,
  default 1280 px; the source file is never touched) before it is sent. A full-res
  photo emits so many vision tokens it OOMs a small-VRAM GPU into an HTTP 400.
- The reply is **capped** (`num_predict: 120`), so the model never needs a large
  output budget.

Because both the input and the output are small, the tool does **not** need a large
context window, which matters for the model choice below.

## The `ollama_num_ctx` cap (why the newer models are fast)

Newer vision models ship a very large **native context**: qwen3-vl declares 256K.
Ollama sizes its KV cache off that declared context, so on load it grabs almost the
whole card. Measured on a 24 GB RTX 3090, an uncapped `qwen3-vl:8b-instruct` (a
6.1 GB q4 model) pinned VRAM at **98%** and ran at **9:11 / 100 images** — because
it was spilling into system RAM and thrashing (the driver's sysmem fallback), not
because the model is inherently slow.

The fix is `tagging.ollama_num_ctx` (config.json, default **8192**), passed as
`options.num_ctx` on every `/api/generate` call in
[`scripts/tag_and_rename.py`](../scripts/tag_and_rename.py). Since the image is
pre-downscaled and the reply is capped at 120 tokens, 8192 is comfortable headroom
and loses nothing for describe-and-title use. Set `0` to disable the cap (let the
model/Ollama default stand). Capping cut the same 8b run from **9:11 to 2:37** and
VRAM from **98% to 43%**, with no quality change. `4096` is likely still safe and
frees a little more VRAM.

> Re-testing note: Ollama caches a loaded model partly by its options, so after
> changing `ollama_num_ctx`, let the model unload (or `ollama stop <model>`) before
> the next run, or the first run still reflects the previous allocation.

## Benchmark results (100 images, RTX 3090, `num_ctx=8192`)

Quality is a subjective 1-5 read of the descriptions + filenames. Full comments are
in the CSV.

| Model | Runtime | VRAM | Quality | Notes |
|---|---|---|---|---|
| **qwen3-vl:8b-instruct** | 2:37 | 43% | **5** | Clearest, most detailed; correct filenames. New default. |
| qwen3-vl:4b-instruct | 1:42 | 32% | 4 | Solid; matches the old 7B default's quality, faster. |
| qwen3-vl:2b-instruct | 1:26 | 25% | 3 | On-subject but miscounts people, some ALL_CAPS filenames. |
| qwen2.5vl:7b (previous default) | 2:37 | 39% | 4 | Good baseline; strictly beaten by 8b-instruct. |
| ministral-3:8b | 2:15 | 44% | 3 | Hallucinates pet states ("emaciated and injured" for a dog licking its leg), miscounts, calls the same dogs "cats" in another shot, truncated `_and` filenames. A 3 relative to its size class: weak reliability at an 8B cost. Dominated by qwen3-vl:4b (faster, cleaner, higher-effective-quality). |
| gemma3:4b | 2:25 | 24% | 3 | **Hallucinates whole scenes** (fused two clowns + a wall plaque into "a woman holding fortress pamphlets"). |
| minicpm-v4.6 | 2:16 | 34% | 2 | **Leaks its reasoning** into the description and filename. |
| qwen2.5vl:3b | 2:07 | 47% | 1 | Broken: `_Unknown_Image` filenames + `@@@@` descriptions, reproducibly. |

Key reads:

- **qwen3-vl:8b-instruct strictly dominates the old qwen2.5vl:7b default:** same
  runtime, ~same VRAM, higher quality. With the context cap there is no
  speed-vs-quality trade to weigh.
- Use the **`instruct`** variants, never `thinking`. The tool feeds the model's raw
  output straight into EXIF and the filename, so a reasoning chain would both slow
  tagging and leak reasoning text into the output. minicpm-v4.6's quality-2 result
  is exactly this failure mode.
- Between the two quality-3 lightweight options, **qwen3-vl:2b beats gemma3:4b**:
  it is faster and its errors stay on-subject (a wrong headcount is a minor
  annoyance; gemma3 inventing the whole scene makes the filename worthless).

## Recommended tiers (wired into the wizard)

`scripts/gui/wizard_recommend.py` maps detected VRAM to a vision model. The shipped
tiers follow the benchmark:

| Tier | Ollama vision model | ~VRAM (of 24 GB) |
|---|---|---|
| 24 GB+ (default) | `qwen3-vl:8b-instruct` | ~10 GB |
| 16 GB | `qwen3-vl:4b-instruct` | ~7.7 GB |
| 8-12 GB | `qwen3-vl:2b-instruct` | ~6 GB |

The recommendation is a suggestion, not a gate: every option stays selectable in
Settings → Tag & Rename and in the wizard's model picklist.

## Remote Tag & Rename

Remote runs pull the vision model onto the RunPod **network volume** during
provisioning. To keep provisioning a **one-time** job, `pod/provision.sh` caches
**all three tiers** by default (`OLLAMA_MODEL_LIST`, default
`qwen3-vl:8b-instruct qwen3-vl:4b-instruct qwen3-vl:2b-instruct`, ~11 GB total)
**plus** the single configured `ollama.model` (`$OLLAMA_MODEL`, de-duplicated so a
custom-configured model is never missed). Remote Tag & Rename runs on a fixed cheap
16-20 GB tag card that fits any of the three, so once the volume is provisioned you
can switch the vision model in Settings with **no re-provision**.

The configured model is stashed by `runpod_provision._load_config` as
`rpc["_ollama_model"]` for `cmd_setup_volume` / `cmd_provision` (previously the
provision was hardcoded to `qwen2.5vl:7b` and ignored config). Pulls are additive
and `ollama pull` is a no-op for a model already present; a single failed pull (a
bad/renamed tag) is logged and skipped, the rest still cache.

**Re-provisioning is incremental** (`provision.sh`), so switching a model or taking
a minor update no longer means a fresh volume and a full ~27 GB re-download:

- The **venv** is skipped unless a stamp (a hash of the resolved requirements +
  constraints + explicit packages + the image's torch version) changes — the full
  ~30-package pip install was the biggest waste. `FORCE_VENV=1` rebuilds it anyway.
- The **cached ollama runtime** on the volume is reused instead of re-installing.
- **SeedVR2 weights**: a fresh provision caches all three DiT tiers by default
  (`DIT_MODEL_LIST` = 3B Q8 / 7B FP8-mixed / 7B FP16, ~26 GB) plus the configured
  `upscale.dit_model`, so a lighter upscale model can be picked in Settings with no
  start-of-run download. Valid files skip, and **stale weights are pruned** (a DiT
  from a previous choice that is no longer in the set is removed once the configured
  DiT is confirmed present). `SEEDVR2_PRUNE=0` keeps them.
- **Obsolete Ollama models are pruned**: anything on the volume outside the desired
  set (e.g. a `qwen2.5vl:7b` from an older provision) is `ollama rm`'d to reclaim
  storage. `OLLAMA_PRUNE=0` keeps every installed model.
- `FORCE_ENGINE=1` re-downloads the SeedVR2 engine code (otherwise kept if present).

So you can safely re-run Settings → Remote → "Provision models…" to switch the
default vision tiers or refresh the volume; it keeps what is valid and only fetches
(and prunes) what changed.

The `ollama_num_ctx` cap applies identically on remote runs: `tag_and_rename.py`
runs locally and repoints its Ollama URL at the pod over an ssh tunnel, so the same
`options.num_ctx` is sent to the pod's Ollama.
