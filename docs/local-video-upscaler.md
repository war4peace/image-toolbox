# Local Video Upscaling (design)

Design notes for **local video upscaling**: running the Video Upscaler's SeedVR2
work **on the user's own GPU**, in-process, instead of (or as an alternative to)
a rented RunPod pod. UI: a **local / remote selector** on the existing **Video
Upscaler** tab, mirroring the image tabs' "Run on" picker.

> Status: **PLANNED (0.5.0-experimental).** This doc is the design of record and
> the decision reversal it depends on. Nothing is built yet. The first code step
> is a `LocalVideoEngine` spike (see section 12).

---

## Contents

- [1. Why this reverses a documented decision](#1-why-this-reverses-a-documented-decision)
- [2. Scope](#2-scope)
- [3. The two objections, as guards not gates](#3-the-two-objections-as-guards-not-gates)
- [4. Reuse: the engine seam already exists](#4-reuse-the-engine-seam-already-exists)
- [5. `LocalVideoEngine`](#5-localvideoengine)
- [6. Custom targets: BOTH explicit resolution AND ratio](#6-custom-targets-both-explicit-resolution-and-ratio)
- [7. No VRAM gate on local: the floor becomes advisory](#7-no-vram-gate-on-local-the-floor-becomes-advisory)
- [8. Local benchmark harness (test till it breaks)](#8-local-benchmark-harness-test-till-it-breaks)
- [9. Local time estimate (no cost)](#9-local-time-estimate-no-cost)
- [10. Install-mode gate and the tab toggle](#10-install-mode-gate-and-the-tab-toggle)
- [11. Phase 2 (planned, not now): non-SeedVR fixed-ratio engine](#11-phase-2-planned-not-now-non-seedvr-fixed-ratio-engine)
- [12. Build order](#12-build-order)
- [13. Open questions](#13-open-questions)
- [14. Benchmark log (as-measured, local)](#14-benchmark-log-as-measured-local)
- [15. As built: the predictive VRAM sizer](#15-as-built-the-predictive-vram-sizer-scriptsvideo_vram_sizerpy)
- [16. Planned: per-card VRAM benchmark suite (user-runnable)](#16-planned-per-card-vram-benchmark-suite-user-runnable)
- [17. As built: the mid-segment thrash watchdog](#17-as-built-the-mid-segment-thrash-watchdog-local_video_workerpy--localvideoengine)
- [18. As built: the runner wiring + the Local/Remote tab toggle](#18-as-built-the-runner-wiring--the-localremote-tab-toggle)
- [19. As built: the local time estimate (history-driven, honest)](#19-as-built-the-local-time-estimate-history-driven-honest)
- [20. As built: the per-card VRAM benchmark suite](#20-as-built-the-per-card-vram-benchmark-suite)
- [21. As built: dynamic ratio targets + the per-GPU feasibility guard](#21-as-built-dynamic-ratio-targets--the-per-gpu-feasibility-guard)
- [22. Planned: extending the benchmark to remote pods](#22-planned-extending-the-benchmark-to-remote-pods-050-experimental)

---

## 1. Why this reverses a documented decision

`docs/video-upscaler.md` (section 1) states the Video Upscaler is **"RunPod-only
by design"** and **"never offers a local path"**, for two reasons: local SeedVR2
video is too slow (a diffusion pass per frame), and it is exposed to the
GPU-degradation bug that motivated remote upscaling (#1).

That decision was taken on **one machine's data point** (the developer's RTX 3090,
24 GB), at a time when remote was the only proven path. It is a developer-bias
constraint, not a product one. Two things reverse it:

- **The value proposition is different, not worse.** Remote video is **expensive**:
  on the order of **$1/hour of GPU time for a bit under one minute of upscaled
  footage**. Local video is **almost free**. Local is not "a worse remote": it is a
  **free-and-slow(-and-possibly-thrashy)** tier next to an **expensive-and-fast**
  one. Users with a capable GPU should get to choose; limiting a 5090 owner because
  the developer had a 3090 is the wrong call.
- **The two original objections are guard-and-measure problems, not blockers**
  (section 3), and we already shipped **local batch *image* upscaling** over the
  exact same degradation bug.

**Direction can adjust on new findings.** The app has matured; we now have the
injected-engine seam, the watchdog, two-granularity resume, and real benchmark
tooling. The "never local" language in `docs/video-upscaler.md` is superseded by
this document.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 2. Scope

- **In:** local SeedVR2 video upscaling, in-process, on a Local/Both install, as an
  alternative to remote for the same per-video flow (probe -> split -> upscale each
  segment -> reassemble -> mux -> drift check). A **local / remote toggle** on the
  Video tab. **Custom targets** beyond the three presets (section 6). A **local
  benchmark harness** (section 8). **Loud** degradation/OOM guards (section 7).
- **Out (this phase):** the non-SeedVR fixed-ratio 2x/4x engine (section 11, a
  separate later phase); any change to the remote path's behaviour.
- **Never touch the source.** Unchanged from the remote path: all work happens on
  temp copies and a separate output tree.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 3. The two objections, as guards not gates

**Degradation bug (loud guard, no gate).** The slowdown-until-reboot bug is real
but **N=1** (the developer's specific GPU/OS/driver/workflow history could all
contribute). We shipped local *image* upscaling over it by **detecting and
notifying**, not by refusing to run. Local video does the same:

- Wire the existing **slow-segment watchdog** (`batch_video_upscale.py`,
  `VideoSlowWatch`, anchors to the running-minimum s/MP) into the local path.
- Detect hard **OOM** (`runner_common.is_oom_error`) and treat it as an episode.
- On an episode: **notify loudly** (every configured backend + taskbar), **auto-stop
  after the current segment**, and rely on **two-granularity resume** so the user
  reboots and continues from the first unfinished segment. Same contract the image
  watchdog already provides, adapted to "segment" as the unit.

**Speed (measure and show, no gate).** Diffusion per frame is slow on consumer
cards, but a remote RTX 5090 was already used for 1080p; a **local** 5090 is the
same silicon. The mitigation is **transparency up front**, not exclusion: show an
honest local time estimate before Start (section 9), and let the user decide.
Short-clip benchmarking (section 8) makes the estimate real per card.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 4. Reuse: the engine seam already exists

Local video is **not** a from-scratch feature. `batch_video_upscale.py` already
runs on an **injected engine** (`run_batch(engine, ...)`) and ships two engine
shapes today: the real `RemoteVideoEngine` and a `PassthroughVideoEngine` (no-pod
testing). **Everything except the GPU step already runs locally**: walk, split,
CFR-normalize, forced-keyframe re-encode, deinterlace, concat, audio-mux,
duration-drift, two-granularity resume, installment caps, the slow-segment
watchdog, notifications, taskbar.

So local video reduces to:

1. A new **`LocalVideoEngine`** (section 5): the one genuinely new piece.
2. **Custom targets** (section 6): new UI + a small amount of plumbing on top of
   the existing box-fit math.
3. **Local benchmark harness** (section 8): `pod/bench_video.py` run locally,
   driven from the GUI.
4. **Local time estimate:** a cost-free variant of `video_estimate.py`.
5. **Un-gate the tab** (section 10): a local/remote toggle; remove the hard
   "remote-only" block for Local/Both installs.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 5. `LocalVideoEngine`

A drop-in for the injected engine interface (`process_segment(...)`,
`last_segment_seconds`, `last_phase`, `telemetry()`, `device_name`, `close()`),
wrapping the **in-process SeedVR2 streaming path** (`chunk_size>0`), the same path
the pod worker uses (`pod/worker.py --mode video`), **not** the still image path.

- Loads DiT/VAE **once** and caches them, reusing `upscale_engine.py`'s load-once
  pattern (the resident-worker model on the pod; here the process is the worker).
- Uses the **streaming** (`chunk_size>0`) inference so RAM stays bounded (SeedVR2's
  load-all path holds every output frame uncompressed; the streaming path is why
  the remote worker doesn't OOM host RAM). The local engine inherits that bound.
- **Offload knobs are the local VRAM lever.** SeedVR2 offloads (the wizard's
  principle: any card can run any model, just slower). On a small-VRAM card the
  engine runs at batch=1 with heavier offload; on a big card it uses a larger
  temporal window/batch. The local benchmark (section 8) finds the safe batch per
  (card, output-MP), feeding the existing `video_batch_learn` adaptive-batch table.
- `telemetry()` returns `None` by design: unlike the remote engine (which polls the
  pod and streams `RTELEM`), local GPU telemetry is sampled by the **GUI itself**.
  During a local run the Video tab drives `App.sample_telemetry` on a short cadence
  (`_start_local_telemetry`), which feeds the tab's "Local Unit" row and the per-run
  usage graph (#9), so the engine does not duplicate it.
- Only available on Local/Both installs (needs torch + SeedVR2 locally, section 10).

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 6. Custom targets: BOTH explicit resolution AND ratio

The current pipeline has **only three preset boxes** (`TARGET_BOX`/`TARGET_RES`:
1080p / 1440p / 4K) and cannot express anything else. Prototyping ("test till it
breaks, then pull back", section 8) is impossible inside three fixed presets, and
users with unusual sources want finer control. Local mode adds a **Custom** target
with **two interchangeable input modes** (user toggles; ratio reduces to a
resolution internally):

- **Explicit output resolution:** enter an output short-side in px (or WxH); the
  existing **box-fit** math (`video_estimate.fit_scale` / `output_dims`, which
  already generalizes to any box) fits the source to it, aspect preserved. This is
  the precise mode for sweeping a card to its OOM ceiling.
- **Upscale ratio:** enter a multiplier (2x / 4x / custom) applied to the source
  dimensions. Matches the ComfyUI mental model and the future fixed-ratio engine
  (section 11). Note the **VRAM ceiling moves per source** in this mode (a 2x of a
  1280x800 clip is far larger than a 2x of a 320x240 clip), so the estimate and the
  OOM guard, not a fixed floor, are what protect the user.

Both modes coexist with the three presets (presets stay for one-click common
cases). The preset boxes remain landscape-fit boxes; a custom explicit resolution
is likewise treated as a box.

**Built (0.5.0): the RATIO half + the feasibility guard shipped; see section 21.**
The 2x/4x ratio targets and a per-GPU feasibility filter are as-built; the arbitrary
explicit-resolution entry field is still the remaining part of this section.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 7. No VRAM gate on local: the floor becomes advisory

Remote uses `video_estimate.VRAM_FLOOR = {1080p: 32, 1440p: 80, 4K: 90}` to **refuse
to offer** a card that cannot serve a target *at a usable batch*. That floor is a
**remote card-picking** input and must **not** gate local:

- **Local never hard-refuses on VRAM.** SeedVR2 offloads; a 24 GB card genuinely
  produces small outputs at batch=1 (the developer's proven ComfyUI case: 640x480
  -> 1280x960, ~1.23 MP). Refusing it would earn exactly the "we didn't test enough"
  ridicule from technical users who *can* make it work.
- **The floor becomes advisory:** a target above the card's advisory floor shows a
  **warning** ("this may be slow or run out of memory on this GPU"), never a block.
- **Targets must be allowed to EXCEED the calculated maximums.** The custom target
  (section 6) deliberately lets the developer/user push past `VRAM_FLOOR` so the
  ceiling can be **found empirically** (test till it breaks) rather than guessed
  conservatively. The OOM guard (section 3) catches the break cleanly and resumes.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 8. Local benchmark harness (test till it breaks)

An in-app, automatable benchmark that establishes the **real** per-card ceilings and
rates, so tiers come from measurement, not the developer's single guess. This is
essentially `pod/bench_video.py` run **locally** through `LocalVideoEngine`.

- **Input:** a short clip (5-10 s). `video_pipeline` already extracts clips (the
  segment-picker path / `extract_clip`), so the harness can cut a short segment from
  any source, or use a bundled sample.
- **Sweep:** for a chosen source resolution, upscale the clip at an **ascending
  series of targets** (custom resolutions / ratios) until it **OOMs or degrades**,
  then record the last good target as the card's ceiling for that source class.
- **Log:** seconds/frame, seconds/output-MP, peak VRAM, batch used, and
  OOM/degradation outcome, per (card, source-res, target). Write the rate to
  `db.gpu_perf` (task `video-mp-<target>` already exists) and the safe batch to
  `video_batch_learn`, so **the app's own estimate self-improves** and future runs
  start at a safe batch.
- **Automate:** a "Benchmark this GPU" action that runs the sweep unattended and
  writes a report, so a non-developer user can calibrate their own card.

The developer's **prototyping stage** uses this harness to build the first real
local tier table across sources (roughly 320x240 to 1280x800) and destinations,
before any tier is hard-coded as a default recommendation.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 9. Local time estimate (no cost)

`video_estimate.py` already computes **duration** from `db.gpu_perf` history and a
seeded rate table (`seconds_per_mp`, `estimate_queue`). Local needs a **cost-free
variant**: same duration math, no `price_per_hour`, no spin-up-pod term (model load
is the only warm-up). Show the estimated wall-clock before Start so nobody launches
a 20-hour local job blind. Seed it from the local benchmark (section 8) and refine
per card from real runs.

**Built (0.5.0): see section 19 for the as-built history-driven local estimator.**

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 10. Install-mode gate and the tab toggle

- **Local video is Local/Both only** (needs torch + SeedVR2 locally), exactly like
  local *image* upscaling. A **Remote-only** install keeps today's remote-only Video
  tab unchanged.
- **Un-gate the tab:** `docs/video-upscaler.md` 15.1 currently says "Remote-only. No
  local path; the tab is blocked with guidance until remote is ready." Replace the
  hard block with a **local / remote toggle** (mirroring the image tabs' "Run on
  remote pod" checkbox). On a Local/Both install the toggle defaults sensibly and
  either path is selectable; on Remote-only, local is disabled with the same
  guidance as today.

**Built (0.5.0): see section 18 for the as-built runner wiring + tab toggle.**

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 11. As built (0.5.6): non-SeedVR fixed-ratio engine (Real-ESRGAN)

A second local engine: a **non-SeedVR, fixed-ratio 2x/4x** upscaler (Real-ESRGAN-class),
chosen **per queued video** next to SeedVR2, so one queue can mix methods.

- **Why it matters for a broad audience:** fast, low-VRAM, deterministic, runs on almost
  any GPU, where local SeedVR2 is slow *everywhere*. For many users "fast, good-enough,
  runs on my card" beats "slow, generative, best." It also structurally avoids SeedVR2's
  temporal jitter (no temporal VAE) and greatly reduces text/logo distortion (mild,
  deterministic prior), at the cost of no temporal-consistency mechanism (possible
  inter-frame shimmer on noisy sources).
- **Loader = spandrel** (chaiNNer's MIT model loader), NOT basicsr (the unmaintained
  install-breaker). Reuses the Local/Both torch stack; added to `bootstrap.ps1` Local/Both.
- **Measured (RTX 3090, fp16):** the *compact* `realesr-general-x4v3` (SRVGGNetCompact) is
  the value tier: 1080p x4 at ~5.4 fps in ~1.5 GB, fits an 8 GB card untiled. `RealESRGAN_x4plus`
  (RRDBNet) is the *quality* option: ~16x slower, ~8 GB for a single 480p frame (tiling
  mandatory). So the shipped **default is the compact model**; x4plus is opt-in.

### 11.1 As-built shape

- **`scripts/fixed_ratio_engine.py`**: `FixedRatioVideoEngine`, a drop-in for the injected
  engine seam (section 4). Per segment: ffmpeg raw-rgb24 decode -> batched, tiled per-frame
  spandrel upscale on the GPU -> ffmpeg yuv420p encode (concat-`-c copy`-compatible). Honors
  the box-fit `resolution` (model emits src*scale, then a scale filter fits the target box).
  OOM back-off shrinks the frame-batch then the tile; kills both ffmpeg procs on Stop/error.
- **`scripts/esrgan_models.py`**: torch-free catalog + lazy, **SHA-256-verified** download
  (via `net_ssl`) to `models/ESRGAN/`. Weights are grouped into **tiers** (Compact / Quality);
  the GUI shows ONE Method entry per tier (`catalog()` returns tier representatives). Within a
  tier the engine **auto-picks the best-scale weight for the target ratio** (`resolve_for_ratio`,
  resolved at Prepare and stored): a **2x** target uses a native **x2** weight rather than
  computing 4x and downscaling. Shipped: `realesr-general-x4v3` (compact x4, default),
  `RealESRGAN_x4plus` (quality x4), `RealESRGAN_x2plus` (quality x2, auto for 2x targets).
  Measured win: 1080p -> 4K (a 2x target) on a 3090 dropped from **4.82 s/frame** (x4plus,
  4x-then-downscale) to **1.30 s/frame** (x2plus, native) at identical output. Adding a weight
  is one verified row (+ a TIERS entry if it is a new tier).
- **Per-job engine (`db.py`)**: `video_outputs` gained nullable `engine`/`model` columns
  (NULL = the legacy SeedVR2 default, so pre-#11 rows + output paths are unchanged). The PK
  is unchanged; the one accepted limitation is that the SAME file at the SAME target can't be
  queued under two engines at once (a different file or ratio is fine). Non-default engines
  tag the output filename (`_realesrgan`) so results never collide on disk.
- **Runner dispatch (`batch_video_upscale.py`)**: `LocalEngineRouter` builds the engine per
  job from its (engine, model) and keeps ONE resident at a time (closes + reloads on a change
  to bound VRAM); `run_queue(resolve_engine=...)` picks it per job. Remote / passthrough /
  auto-resume are unchanged (single injected engine). `prepare_job(engine=, model=)` stamps
  the job; the headless `--engine` / `--fixed-ratio-model` flags set the default.
- **Video Upscaler tab**: the add-to-queue row now has a **Method** combobox (engine+model:
  4 SeedVR2 variants always, +2 Real-ESRGAN in Local mode only) and a **Target** combobox
  whose options DEPEND on the method (engine-aware feasibility). The queue list shows a
  **Method** column. So a user builds a mixed queue and presses Start once.
- **Settings** hold only the **default** method + model (pre-selects the tab's comboboxes);
  the real choice is per video. Since 0.5.8 that is ONE "Model:" picklist under "Method:",
  listing the SeedVR2 weights or the Real-ESRGAN models depending on the method selected
  above it. The two picks are stored separately (`video.dit_model` /
  `video.fixed_ratio_model`), so flipping Method never loses the other engine's choice;
  the combobox is just a view onto the active one (`_model_store_var` /
  `_sync_model_choices` in `gui/tab_settings.py`). This replaced the old pair: a standalone
  SeedVR2 "Model:" row higher up in the section plus a "Real-ESRGAN model:" row that was
  greyed out whenever the method was SeedVR2.

### 11.2 No cap for Real-ESRGAN (limits are benchmark-derived, later)

SeedVR2's output-MP feasibility ceiling (`max_output_mp`, `_job_exceeds_gpu`) is a SeedVR2
notion (its VRAM scales hard with output size). Real-ESRGAN is fixed-ratio, low-VRAM and
tiles, so **no VRAM cap is enforced on it**: the tab never greys a fixed_ratio target, the
Start gate never counts it infeasible, and the runner never defers it. Its real resolution /
length limits are meant to come from a **real-footage benchmark** later (section 8's harness,
extended with an `engine` dimension), not an assumed ceiling.

### 11.3 Exact-ratio targets only (no resize of a generated frame)

Because Real-ESRGAN is fixed-ratio, upscaling to a target whose scale is NOT a native model
scale would need an ffmpeg resize of the *generated* frame: a downscale (e.g. 2x model then
shrink 4320p -> 1440p) or an upscale (e.g. 4x model then stretch), both of which throw away or
soften detail the model produced. To avoid that quality loss, the Target combobox offers a
fixed_ratio target ONLY when its box-fit scale equals a native model scale of the selected tier
(`esrgan_models.tier_scales`; Quality = {2, 4}, Compact = {4}), within a 1% tolerance for odd
source dims. So the generated frame is written at its native size, never resized. Consequences:
1080p -> **4K** is offered (4K IS exactly 2x of 1080p) but 1080p -> **1440p** (1.33x) is NOT
(that path stays SeedVR2-only); the ratio targets (2X / 4X) always qualify; and the Compact
tier (x4 only) offers nothing for a 1080p source (its x4 exceeds the 4K cap, and 4K is only
2x), so a 1080p source uses the Quality tier's native x2.

### 11.4 Remote Real-ESRGAN (shipped 0.5.6)

The engine above began local-only and gained its remote half in the same version, so the
Method combobox offers Real-ESRGAN in both modes: a lightweight `pod/worker.py --mode esrgan`
loads `FixedRatioVideoEngine` on a cheap, low-VRAM, **no-volume** pod which self-downloads the
~65 MB hash-pinned weight via `esrgan_models.ensure_model`. It rides on a general queue change
(**per-item GPU binding + grouped multi-pod Start**), so one mixed queue routes each item to
its own (method, GPU) pod, one pod up at a time. The design (motivation, the GPU-combobox
semantic flip, the pendulum Auto-resume, the DB/UI ripple, the build order) and the as-built
notes live in `docs/video-upscaler.md` section 18, with 18.8 recording what shipped against
what was designed, since it is fundamentally remote-pod orchestration.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 12. Build order

1. **This design doc** + the decision reversal (done: this file; the "never local"
   passages in `docs/video-upscaler.md` are updated to point here; a
   `docs/future-features.md` milestone is added).
2. **`LocalVideoEngine` spike:** prove the in-process streaming path (`chunk_size>0`)
   end-to-end on the local GPU against one short clip. No UI yet. Unblocks everything.
3. **Local benchmark harness** (section 8) on top of the spike; developer runs the
   prototyping sweep to build the first real tier table.
4. **Custom targets** (section 6, both modes) + **advisory-only VRAM** (section 7).
5. **Tab toggle + local time estimate + loud guards** (sections 3, 9, 10).
6. **Phase 2** (section 11): the non-SeedVR fixed-ratio engine (Real-ESRGAN). **Shipped
   0.5.6** as a per-video method choice; see section 11 as-built.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 13. Open questions

- **Tier defaults vs. free-for-all.** After the prototyping sweep, do we ship
  **recommended** local targets per detected VRAM (wizard-style suggestion, still
  overridable) or leave local fully open with only the advisory warning? Leaning
  suggestion-with-override, matching the first-start wizard's philosophy.
- **Degradation recovery UX.** After an auto-stop episode, prompt "reboot and resume"
  explicitly, or just leave the queue resumable and notify? The image path leaves it
  resumable; video runs are longer, so a clearer prompt may be warranted.
- **Engine parity with the proof point.** The developer's ComfyUI proof (640x480 ->
  1280x960 on a 24 GB 3090) used **SeedVR2**, the **same** engine the app vendors
  (numz build), so those VRAM/feasibility observations transfer directly. The local
  benchmark still runs against the vendored engine to get exact per-card numbers, but
  there is no cross-version uncertainty to account for.
- **Concurrency with local image tools.** A local video run owns the GPU in-process;
  the existing GPU-overlap warning (which today treats video as pod-only, so it never
  contends) must be extended to grey out local image upscaling / local tag while a
  **local** video run is active.

### 13.1 Future research: decouple the VAE-decode batch from the DiT batch

**The single highest-value idea to raise the 24 GB ceiling without quality loss.**
Benchmarking (section 14) shows the batch ceiling on a 24 GB card is set by **VAE
decode memory**, not by DiT: bs17 upscales fine through DiT, then OOMs in Phase 3
(decode). Decode memory scales with `batch x output_pixels`. The textbook fix,
`decode_tiled` (spatial VAE tiling), is **rejected on quality grounds** (it adds
per-frame seam artifacts; a standing project preference, see the image path).

The seam-free alternative: **decode in smaller TEMPORAL sub-groups than the DiT batch.**
Keep a large temporal window through DiT (which is what buys motion continuity, the
thing the user cares about) but run the VAE decode over fewer frames at a time, so decode
peak memory is bounded **without** spatial tiling (no seams). This decouples "continuity
window" (DiT batch + overlap) from "decode memory" (decode sub-batch), which today are
the same number and therefore collide on 24 GB. It would let a 24 GB card run, say, a
bs17/ov6 DiT window while decoding 5 frames at a time.

Needs SeedVR2-internal changes (the decode loop currently uses the same `batch_size`),
so it is a research item, not a config knob. If it works it is the path to overlap-6 +
1080p on a 24 GB card at full quality. Investigate against the vendored engine's
`generation_phases` / VAE decode path.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 14. Benchmark log (as-measured, local)

Real measurements from the `LocalVideoEngine` spike (`scripts/spike_local_video.py`),
recorded for future reference. This is the empirical basis for the local tier table
(sections 6-8): it replaces guessed VRAM floors with what the hardware actually does.

**Resident vs phased is per-mode (video threshold is separate and higher).** The engine keeps
DiT+VAE resident in VRAM on cards at/above a threshold, else it PHASES them (offloads the DiT to
system RAM before the VAE decode, freeing ~14 GB exactly when the decode needs it, quality-neutral,
just a PCIe round trip). A video temporal-window decode is far heavier than a single image, so the
**video** path uses its own `video.vram_resident_threshold_gb` (default **90**), distinct from the
image `upscale.vram_resident_threshold_gb` (default **40**). At 90 the 40-80 GB cards (A100/H100/
A6000/L40) phase for video, where resident would thrash near the ceiling; PRO 6000 (96 GB) and up
stay resident. Measured on a PRO 6000 (96 GB, 7B): 4K OOMs resident but FITS phased (at 99% / batch
5 / ~123 s/frame, so "fits but impractical" without a bigger card); a 6.22 MP target thrashes
resident (99% / batch 5 / 54.6 s/frame) and has headroom phased. `0` = always phase. NB this is a
per-CARD choice, not per-target: a resident card can still thrash/OOM on a high target, so the 4K
feasibility gate is complementary, not replaced by it. The video builders inject the value into the
key `upscale_engine._resident_offload_device` reads, so the pod engine (and the benchmark, so its
ceilings match the run) see the video number.

**Rig / method.** RTX 3090 (24 GB), PyTorch 2.11 CUDA, **SDPA** attention (SageAttention
/ FlashAttention not installed), **CPU offload** (resident=False: 24 GB < the 40 GB
resident threshold, so DiT+VAE park in system RAM between passes), `opencv` writer, no
10-bit. Test clip: first 5 s of `benchmark-videos/Pisici.AVI` = **149 frames, 320x240**.
Target **1080p** box-fits to **1440x1080** (4.5x, **1.56 MP/frame**). Reproduce:

    .venv\Scripts\python.exe scripts\spike_local_video.py --resolution 1080 \
        --batch <B> --overlap <O> [--dit-model <file>]

| Model | Req b/ov | RAN b/ov | Peak VRAM alloc/reserved | Time (149f) | s/frame · s/MP | Result |
|-------|----------|----------|--------------------------|-------------|----------------|--------|
| 7B fp16 | AUTO / 0 | **5 / 0** | 17.1 / 20.8 GB | 375.9 s | 2.52 · 1.62 | PASS (healthy baseline) |
| 7B fp16 | 33 / 6 | thrash | (spilled >24 GB) | KILLED | n/a | **THRASH** (sysmem fallback was ON) |
| 7B fp16 | 13 / 6 | **5 / 4** | 22.7 / 26.8 GB* | 1845 s† | 12.39 · 7.96 | bs13,bs9 OOM -> fell to bs5 |
| 7B FP8-mixed | 17 / 6 | **5 / 4** | 22.7 / 26.7 GB | 1954 s† | 13.12 · 8.43 | bs17,13,9 OOM (all in decode) -> bs5 |
| 3B fp16 | 9 / 6 | **5 / 4** | 22.6 / 26.7 GB | 1420 s† | 9.53 · 6.13 | bs9 OOM (decode) -> bs5 |

*reserved 26.8 GB EXCEEDS the 24 GB card = sysmem fallback still partially used on the
failed higher-batch attempts (a stale per-app profile can override the global
"No Sysmem Fallback"; see below). †the 1845 s includes the wasted bs13+bs9 attempts
(each re-runs the full VAE encode before the DiT OOM); the clean bs5 pass alone is ~376 s.

**Findings so far:**
- **The batch ceiling is set by VAE DECODE memory, not DiT** (measured on FP8: bs17 and
  bs13 pass DiT then OOM in **Phase 3 decode**). Decode peak = `batch x output_pixels`.
  See section 13.1 for the seam-free way to lift this wall.
- **The decode wall is essentially DiT-precision-INDEPENDENT.** The VAE
  (`ema_vae_fp16.safetensors`) is the same file for every DiT, and in the offload regime
  the DiT is parked on CPU during decode, so **7B FP8 lands at the same bs5/ov4 as 7B
  fp16** (bs9/ov6 still OOMs on FP8). Lowering DiT precision does NOT buy a bigger batch
  on 24 GB. The remaining quality-preserving lever for a bigger batch is a smaller MODEL
  footprint overall (3B: less total VRAM churn) or a lower target, pending 13.1.
- **ALL THREE models cap at bs5/ov4 @ 1080p on 24 GB** (7B fp16, 7B FP8-mixed, 3B fp16 all
  fall to bs5; peak alloc ~22.6-22.7 GB is the wall). **3B did NOT push bs9/ov6 into reach.**
  So the decode wall is confirmed model-INDEPENDENT: model choice buys speed (3B's DiT is
  lighter), not a bigger batch. **Overlap-6 at 1080p on 24 GB is BLOCKED for every model.**

**24 GB tier conclusion (as-measured):** at **1080p** a 24 GB card is limited to **bs5 /
overlap 4** regardless of model. To get **overlap 6** on 24 GB at full quality there are only
two levers: (a) **lower the target resolution** so the decode tensor shrinks and a bigger batch
fits (a 720p / 960-short-side target should let bs9-13 fit -> overlap 6; NOT yet benchmarked,
the obvious next run), or (b) the **section 13.1 decode-decoupling** research (overlap-6 AND
1080p, but needs engine work). `decode_tiled` would also work but is quality-rejected. This
gives the tier UX its shape: on 24 GB, offer the user a **continuity-vs-resolution** choice
(e.g. "1080p @ ov4" OR "720p @ ov6"), not both, until 13.1 lands. Model choice on 24 GB is a
**speed** decision (3B fastest), not a batch/continuity one. **(Confirmed by 14.1: at
1280x960 all three models run bs17/ov6 clean.)**

### 14.1 Flat 2x (640x480 -> 1280x960): overlap-6 IS reachable on 24 GB

The motivating real-world case for old 640x480 footage. Genuine 2x, **1280x960 output
= 1.23 MP** (vs 1080p's 1440x1080 = 1.56 MP). Source: 640x480 clip (lanczos-derived from
the 320x240 asset, so timing/VRAM are valid, quality is not native-representative).
Started at bs17/ov6:

| Model | Req b/ov | RAN b/ov | Peak alloc/reserved | Time (149f) | s/frame · s/MP | Result |
|-------|----------|----------|---------------------|-------------|----------------|--------|
| 7B fp16 | 17 / 6 | **17 / 6** | 20.9 / 25.5 GB | 725.5 s | 4.87 · 3.96 | PASS clean, **no fallback** |
| 7B FP8-mixed | 17 / 6 | **17 / 6** | 20.9 / 25.5 GB | 698.9 s | 4.69 · 3.82 | PASS clean |
| 3B fp16 | 17 / 6 | **17 / 6** | 20.9 / 25.5 GB | 617.9 s | 4.15 · 3.37 | PASS clean (fastest) |

**Findings (the important one for 24 GB):**
- **Overlap-6 IS achievable on 24 GB at 1280x960** (2x of 640x480): all three models ran the
  full **bs17/ov6** window, no OOM, healthy ~5 s/frame (NOT the bs33 thrash). The earlier
  "overlap-6 blocked" verdict was **1080p-specific**, not a 24 GB verdict.
- **The batch ceiling is EXTREMELY resolution-sensitive near the 24 GB wall.** Dropping the
  output just 21% (1.56 -> 1.23 MP, i.e. 1440x1080 -> 1280x960) flips the ceiling from **bs5
  to bs17+** (3.4x the batch). The wall sits between these two output sizes. Practical rule
  for 24 GB: **outputs up to ~1280x960 get a large window (overlap-6, great continuity);
  1080p-class outputs are pinned to bs5/ov4.**
- **Model choice looks speed-only HERE, but is NOT near the ceiling (corrected by 14.2).**
  All three peak at an identical 20.9 GB *at bs17* and 3B is ~15% faster (617 vs 725 s). But
  bs17 is well below 7B's ceiling; push to bs29 (14.2) and 3B fits where 7B collapses. So the
  "model-independent" claim holds only with margin, not at the edge.
- **Caveat - the bs17 ceiling here is "at the edge".** Reserved hit **25.5 GB, above the
  24 GB physical**, on the SUCCESSFUL runs: the allocator pool spilled ~1.5 GB into sysmem
  harmlessly (live alloc 20.9 GB fits; runs stayed healthy, not thrashing). But a STRICT
  "No Sysmem Fallback" config might turn bs17/1280x960 into a clean OOM. Verify the per-app
  fallback policy for the venv python before the productized VRAM model trusts this ceiling;
  treat 1280x960/bs17 as the edge, not comfortable headroom. (Also confirms the global
  no-fallback setting is not fully applying to this process.)

**Revised 24 GB tier picture:** the continuity-vs-resolution crossover is right at
**~1280x960 vs 1440x1080**. Old low-res footage (<= 1280x960 output) gets overlap-6 for free;
only 1080p-class targets force the ov4 compromise (or await section 13.1).
- **VAE decode is the bottleneck, not DiT** (decode ~6-8 s/batch vs DiT ~3.2 s/batch at
  bs5). Suspect the PyTorch-2.11 Conv3d/cuDNN "VAE 3x memory" workaround; investigate
  `decode_tiled` for local.
- **Windows sysmem-fallback must be OFF** ("Prefer No Sysmem Fallback") or an over-large
  batch **thrashes silently instead of OOMing** (~250x slowdown; one decode batch took
  25 min at bs33). Driver updates RESET this; a global setting can be overridden by a
  per-app `python.exe` profile. With it truly off, an overcommit OOMs cleanly and the
  engine's retry auto-steps the batch down.
- **No persistent degradation observed** across these runs despite ~33 h uptime (the bug
  is a separate, reboot-only failure; the thrash above is batch-induced and curable).

### 14.2 Pushing to bs29: model DOES matter, and cascade-recovery is DESTRUCTIVE

Same 2x/1280x960 case, started at **bs29/ov6** (the fallback ladder covers 29->25->21->17->...):

| Model | Req b/ov | RAN b/ov | Peak alloc/reserved | Time (149f) | s/frame | Result |
|-------|----------|----------|---------------------|-------------|---------|--------|
| 7B fp16 | 29 / 6 | **5 / 4** | 20.9 / 25.5 GB | 1865 s | 12.52 | OOM cascade 29->5 (COLLAPSED) |
| 7B FP8-mixed | 29 / 6 | **5 / 4** | 20.1 / 24.3 GB | 1771 s | 11.88 | OOM cascade 29->5 (COLLAPSED) |
| 3B fp16 | 29 / 6 | **29 / 6** | 21.1 / 25.6 GB | 583 s | 3.91 | **PASS clean, first try** |

Two findings, both corrections/additions to 14.1:

- **Model choice DOES buy batch headroom near the ceiling (corrects 14.1's "speed-only").**
  **3B fits bs29/ov6 on the first attempt** (21.1 GB, clean); **7B (both precisions) cannot
  fit bs29** and OOM. At bs17 all three fit identically only because bs17 is below every
  model's ceiling; at the edge the 7B's larger DiT footprint (a transient during the DiT
  phase, even offloaded) pushes it over first. So on 24 GB, **3B is the model that unlocks
  the largest temporal window** (bs29/ov6 vs the 7B's ~bs17), which matters for continuity,
  not just speed.

- **Cascading OOM-recovery from too-high a start is DESTRUCTIVE (VRAM fragmentation).** 7B
  fp16 ran **bs17 clean when STARTED at bs17** (14.1, 20.9 GB), yet here **bs17 OOMs when
  REACHED via 29->25->21->17** and the run collapses all the way to bs5. Same batch, same
  resolution, same (fresh) process: the only difference is the failed higher attempts before
  it. `torch.cuda.empty_cache()` between retries does NOT undo the caching-allocator
  fragmentation the failed bs29/25/21 attempts leave behind, so a batch that fits from a
  clean start fails after a cascade. **Consequence: "start high, fail fast" UNDER-reports the
  true ceiling.** This is NOT session-wide degradation: 3B ran LAST in the chain and cleanly
  used 21.1 GB, so the card still had headroom; the 7B collapse is within-run fragmentation.

**Productization consequences (important):**
1. The batch sizer must be a **predictive VRAM model that starts at/near the right batch**,
   NOT an optimistic-high start that recovers by cascading down (which fragments and
   under-shoots). This reinforces section 7 / the shared-sizer plan.
2. If a retry is ever needed, it likely needs a **hard CUDA context reset** (or a fresh
   subprocess per attempt), not just `empty_cache()`, to clear fragmentation.
3. The predictive model must be **per-model** (3B and 7B have different ceilings), keyed on
   `batch x output-MP` PLUS a model-footprint term.

**Fresh-boot re-run (planned) will confirm the fragmentation hypothesis:** on a clean session,
7B started DIRECTLY at bs17 should run clean, while 7B started at bs29 should still cascade to
bs5 (fragmentation is per-run, boot-independent). 3B bs29 should remain clean.

### 14.3 Fresh boot + STRICT no-sysmem-fallback: the honest ceiling

Re-ran on a clean session, "Prefer No Sysmem Fallback" set globally AND per-app for the venv
python. Idle desktop overhead **~3 GB** (2977 MiB), so effective headroom ~21 GB, not 24.

| Fresh run | RAN b/ov | Peak alloc/reserved | Time (149f) | s/frame | Result |
|-----------|----------|---------------------|-------------|---------|--------|
| 7B fp16 bs17 **direct** | **17 / 6** | 20.9 / 25.5 GB | 753 s | 5.06 | **CLEAN** |
| 7B fp16 bs29 | 5 / 4 | 20.5 / 24.3 GB | 1795 s | 12.05 | cascade -> bs5 |
| 3B fp16 bs29 | 5 / 4 | 20.1 / 24.3 GB | 1508 s | 10.12 | cascade -> bs5 |
| 3B fp16 bs33 | 5 / 4 | 21.0 / 25.5 GB | 1709 s | 11.47 | cascade -> bs5 |

**Confirmed:**
- **Fragmentation/cascade destruction is REAL and boot-independent.** 7B bs17 **direct** = clean;
  7B bs29 still cascades to bs5. Per-process fragmentation, exactly as predicted. => the sizer
  MUST be predictive, and cascade-recovery is destructive (14.2 stands).

**Corrected (two earlier claims were sysmem-aided artifacts):**
- **"3B fits bs29/ov6" (14.2) was FALSE under honest conditions.** That pre-restart success had
  reserved 25.6 GB (>24 physical) = it leaned on sysmem spill. On the fresh strict session
  (no spill + ~3 GB desktop), **3B bs29 OOMs and cascades to bs5**, same as 7B. So 3B does NOT
  truly clear bs29; the "3B unlocks a bigger window" claim is **retracted** pending an honest
  DIRECT test of 3B at intermediate batches (bs21/bs25), which the matrix did not run (it tested
  3B only at bs29/bs33, both above the ceiling, so both cascaded).
- **"Prefer No Sysmem Fallback" is a SOFT preference, not a hard cutoff.** Even fresh + strict,
  the clean bs17 run still shows reserved 25.5 GB (>24): a *small* overcommit (~1.5 GB) is still
  tolerated via a little spill, while a *large* overcommit (bs29, several GB) hard-OOMs. This
  explains every reserved>24 reading. Consequence: bs17 "fits" only with a sliver of soft spill,
  so treat it as the edge; a truly spill-free config would likely sit a notch lower.

**Honest 24 GB ceiling (desktop running):** the only CONFIRMED clean DIRECT run is **7B bs17/ov6**
(~5 s/frame), and it rides ~1.5 GB of soft spill. 3B's honest direct ceiling is **untested** (only
seen cascading from too-high starts). So the real-use ceiling at 1280x960 is **~bs17/ov6**, model
dependence unconfirmed, and **overlap-6 IS still reachable there** (bs17 > 7). The earlier bs29
optimism was sysmem-inflated.

**Tested lever - `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`: NOT AVAILABLE on Windows.**
The idea was to attack the contiguity failure (the VAE `concat_splits` needs a fresh contiguous
block; fragmentation blocks it) with growable segments. **Result: PyTorch 2.11 emits
`UserWarning: expandable_segments not supported on this platform` and ignores the setting**
(confirmed via the env var on this Win11 / CUDA build). So the allocator-level fix is **off the
table on Windows**; we cannot lean on a smarter allocator to undo fragmentation.

**This HARDENS the productization requirements (no allocator escape hatch on Windows):**
1. The batch sizer MUST be **predictive** - pick a batch that fits on the FIRST try, per model,
   keyed on `batch x output-MP` + a model-footprint term + the live free-VRAM headroom (which
   includes the ~3 GB desktop). Never start high and cascade.
2. If a feasibility probe / retry is ever needed, it needs a **fresh subprocess (new CUDA
   context) per attempt** - `empty_cache()` does NOT clear fragmentation and `expandable_segments`
   is unavailable, so an in-process retry is destructive (14.2/14.3). A subprocess pays a model
   reload but guarantees a clean allocator.
3. Because "Prefer No Sysmem Fallback" is soft (14.3), the sizer should target **~2 GB below**
   the measured physical ceiling so it does not depend on the tolerated sliver of spill.

### 14.4 `torch.compile` ON vs OFF on 24 GB: a net loss at real targets

Re-benchmarked the 3090 with the 0.5.1 dual-mode sweep (both compile regimes in one run,
stored under separate `bench_key`s: `7b` = OFF, `7b|c` = ON). Comparing each target's **best
achievable throughput** (min s/frame over the successful batches) and **max feasible batch**:

| Target (output) | OFF best s/frame | OFF max batch | ON best s/frame | ON max batch | Speed result | Batch ratio |
|-----------------|------------------|---------------|-----------------|--------------|--------------|-------------|
| 540x720         | 0.379 (b125)     | 125           | **0.368** (b61) | 61           | ON +3.0%     | 0.49x       |
| 960x720         | 0.691 (b69)      | 69            | **0.657** (b37) | 37           | ON +4.9%     | 0.54x       |
| 810x1080        | **0.897** (b53)  | 53            | 1.116 (b29)     | 29           | OFF +24%     | 0.55x       |
| 1280x960        | **2.189** (b17)  | 17            | 5.474 (b5)      | 5            | OFF +150%    | 0.29x       |
| 1440x1080       | **7.873** (b5)   | 5             | OOM at b5       | n/a          | OFF (ON OOMs)| n/a         |
| 1600x1200       | **10.899** (b5)  | 5             | OOM at b5       | n/a          | OFF (ON OOMs)| n/a         |
| 1920x1080       | **12.244** (b5)  | 5             | OOM at b5       | n/a          | OFF (ON OOMs)| n/a         |

**Finding:** compile wins **only at sub-1080p outputs** (+3-5%), and there it wins *despite*
running at half the batch, so the per-frame speedup is genuinely covering the throughput lost
to the smaller batch. At every real **1080p-class target** it loses: 24% slower at 810x1080,
2.5x slower at 1280x960, and a **hard OOM even at batch 5** above 1440x1080 (it cannot run at
all while OFF completes). The crossover sits exactly at the boundary of the targets the tool
is for.

**Mechanism (consistent with the PRO 6000 4K finding):** compile roughly **doubles activation
VRAM**, which halves the batch ceiling (0.49-0.55x across the low/mid targets, worse above).
At low output-MP the ceiling is high enough that the per-frame win survives; at high output-MP
the collapsed batch ceiling dominates and compile is a net loss. The reported s/frame is
**steady-state** (the schema separates `peak_*_steady` from the cold pass), so it *excludes*
compile's cold-start and per-geometry recompile cost: the true low-res win is below the 3-5%
shown.

**Consequence (as built):** `wizard_recommend.recommend_compile` moved the "recommended" line
from **>=24 GB to >=32 GB**; the 24 GB tier is now **"optional"** with copy stating it's a
wash-to-loss at real targets. The wizard intro no longer flatly claims "20-40% faster." This is
copy-only: compile stays a runtime-gated default (`gate_local_compile`), selectable in
Settings > Video for anyone who wants to benchmark it on their own card. The trade is a
**VRAM-headroom** story, so it may flip on a card large enough to absorb the batch halving at
real targets (>=32 GB) even though activations remain the wall at 4K.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 15. As built: the predictive VRAM sizer (`scripts/video_vram_sizer.py`)

The batch/overlap picker for the LOCAL path, built to the requirements above. It picks the
largest window predicted to fit on the FIRST try; it never starts high and cascades (which
fragments VRAM irrecoverably on Windows, 14.2/14.3). Design = **tier-anchored +
self-calibrating** (the "option b" chosen after 14.3 showed the pod's 5090 curve does not
transfer to the 3090, so a hand-fit curve can't be the source of truth):

- **Seed** per (VRAM tier, output-MP): `_SEED_BY_TIER`. The **24 GB tier is the measured
  anchor** (<= 1.35 MP -> bs17/ov6; 1080p-class -> bs5/ov4, from 14.1/14.3 and the user's
  decision). Every OTHER tier is a deliberately CONSERVATIVE (biased-low) guess: a too-small
  seed only shrinks the window, a too-big one causes the first-run OOM we are avoiding.
- **Free-VRAM step-DOWN** (`_fit_to_free`, never up): budgets against LIVE free VRAM
  (`free_vram_gb` -> torch `mem_get_info`, then nvidia-smi), so the ~3 GB desktop overhead or a
  busy card trims the batch instead of OOMing. The step-down uses a 3090-calibrated working-set
  estimate `peak ~= 14.85 + 0.289 * batch * out_MP` (fit on the two clean 3090 anchors) - a
  STARTING estimate only, since the curve is per-architecture.
- **Learned override** (supersedes the seed): after each segment `record_result` stores the
  batch that actually ran clean in `db.video_batch_learn`, keyed by a **model-qualified gpu id
  (`"<gpu>|<model_tag>"`)** + the 0.5 MP bucket - so 7B and 3B keep separate rows, remote rows
  are untouched, and the sizer converges to each real card+model over time (exactly like the
  remote path self-improves `db.gpu_perf`). `get_learned_batch` is still free-VRAM-clamped.
- **Overlap** is the quality-floored `auto_overlap` (>= 6, the measured seam-free minimum),
  capped at 15 (the seam is a local transition; batch/6 ran away to 80 at batch 480, redundant
  compute for no visible gain), clamped below the batch (so bs5 -> ov4, bs9+ -> ov6, bs480 -> ov15).

Wired into `LocalVideoEngine.__init__(conn=, gpu_id=)` and `process_segment`: on AUTO
(`batch<=0`) the sizer picks the window from the output size; the OOM-retry loop remains a
backstop, and whatever batch runs clean is recorded for learning. Unit-tested tkinter/GPU-free
(seed picks, step-down, learned override, model-key isolation, overlap floor).

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 16. Planned: per-card VRAM benchmark suite (user-runnable)

The sizer's seeds are honest only for 24 GB; every other card starts from a conservative guess
that learning nudges one segment at a time. A **benchmark suite the user runs once on their own
card** closes that gap directly: it determines the **maximum safe (batch, overlap)** for a
**configurable source -> target resolution pair** and writes the result straight into the
learned store, so real runs then start at the true ceiling instead of crawling up from the
conservative seed.

- **Input:** a source resolution and a target (or an explicit output resolution / ratio, matching
  the custom-target modes of section 6), plus the model. A short bundled/derived clip at the
  chosen source size is enough (the wall is set by OUTPUT size, section 14, so the source content
  is irrelevant to the ceiling).
- **Method:** for that (model, output-MP), probe batches **upward from a safe floor** in 4n+1
  steps, **each attempt in a FRESH SUBPROCESS** (mandatory: an in-process sweep would fragment
  VRAM and under-report, 14.2/14.3; `expandable_segments` can't save it on Windows, 14.3). The
  last batch that completes clean, minus a safety notch, is the card's ceiling; overlap follows
  `auto_overlap`. Record peak VRAM + s/frame per step for the estimate.
- **Output:** write `(gpu|model, mp_bucket) -> batch` to `db.video_batch_learn` (the sizer's
  learned store) and the timing to `db.gpu_perf` (the local time estimate), plus a short report.
  A "Benchmark this GPU" action makes it one click for a non-technical user; the developer uses
  the same tool to build the shipped seed tables for cards other than the 3090.
- **Why a suite, not the AUTO path:** AUTO must be safe on the first try (no cascade), so it can
  only ever learn *downward* from a seed or *upward* one careful segment at a time. The benchmark
  suite is the sanctioned place to push UPWARD to failure (test-till-it-breaks, section 8) exactly
  because each probe is an isolated subprocess, so a failed probe can't poison the next.
- **Status:** BUILT (0.5.0) -- see section 20 for the as-built suite. Tracked in
  `docs/future-features.md` #7.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 17. As built: the mid-segment thrash watchdog (`local_video_worker.py` + `LocalVideoEngine`)

The predictive sizer (section 15) makes thrash rare, but it is a SAFETY NET for the residual
cases: an unmeasured card whose seed is too high, or a machine where "No Sysmem Fallback" is off
(then an over-budget batch **thrashes** instead of OOMing -- a healthy VAE decode step is seconds,
a thrashing one was ~25 min, section 14). The existing per-segment slow-watchdog only checks
AFTER a segment, which is useless when one segment crawls for hours; this catches it DURING.

**Why a subprocess (not a thread).** A thrashing segment is stuck inside a synchronous in-process
`process_video` CUDA call, and Python cannot safely interrupt a thread running a CUDA C-extension
(14.2/14.3). So `use_subprocess=True` (the product path) runs **each attempt in a fresh child
process** (`local_video_worker.py`, one `UpscaleEngine.process_video` call) that the parent can
KILL. Three payoffs, not just the watchdog:
  * **killable** -> the watchdog can abort a crawling segment promptly;
  * **fresh CUDA context per attempt** -> no fragmentation carryover, so an OOM retry at a smaller
    batch starts clean instead of inheriting the failed attempt's fragmented pool (the destructive
    cascade of 14.2/14.3 is gone);
  * it is the **same subprocess-per-attempt** the benchmark suite (section 16) needs.
The cost is a **model reload per attempt** (~12 s in offload mode); a resident-killable-worker
optimization can reclaim it later. Left off (`use_subprocess=False`, the spike default) the engine
runs in-process, reusing one cached model.

**Mechanism.** The parent reads the child's stdout: **every line is a liveness heartbeat**, so a
gap longer than **`thrash_stall_seconds`** (config `video.thrash_stall_seconds`, default 300, floor
30) means the GPU is thrashing/hung -> **kill the process tree** (`taskkill /T /F` on Windows, the
only reliable tree-kill) -> raise **`ThrashDetected`**. A CUDA OOM (the worker's `@@LVW-OOM@@`
marker / exit 42) is turned into a normal OOM error so the caller retries **smaller in a NEW clean
process**. Pipeline progress lines are forwarded to the parent's stdout for GUI-log parity, and the
parent stays **GPU-free** (device name + free VRAM via nvidia-smi, `free_vram_gb(prefer_smi=True)`,
so it never holds a CUDA context the child needs).

**`ThrashDetected` is a DEGRADATION episode, not an OOM:** the retry loop does NOT step down from it
(retrying a thrash would just thrash again). It propagates so the runner surfaces it loudly and
stops the run; the unfinished segment resumes on the next run (post-reboot, per the segment resume
cache) -- the same contract as the image watchdog's auto-stop.

**Config:** `video.thrash_stall_seconds` (default 300). A 4K target with a legitimately long decode
step may need a higher value; the 30 s floor guards against a false trip. Wired into
`LocalVideoEngine(use_subprocess=, thrash_stall_seconds=)`; the runner (tab-toggle step) passes the
config value. Watchdog timing tested GPU-free with a fake worker (clean / OOM / stall-kill paths).

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 18. As built: the runner wiring + the Local/Remote tab toggle

This is the step that makes everything above (the engine, the sizer, the watchdog) actually
reachable from the GUI. Nothing new in the pipeline: the same walk / split / reassemble / mux /
resume orchestration runs; only the **injected engine** changes and a **mode selector** picks it.

**Runner (`batch_video_upscale.py`).**
  * **`--local` flag** (mutually exclusive with `--passthrough`): constructs a
    `LocalVideoEngine` and runs `run_queue` with no pod. `_local_seedvr2_paths(cfg)` resolves the
    vendored `seedvr2/` repo + `models/SEEDVR2/` weights off the app root (the SAME `seedvr2`
    config keys the Batch Upscaler reads), and the engine is fed `_worker_cfg()` (the upscale
    config overlaid with the video quality/speed knobs, `dit_model` included), `conn`, `gpu_id=None`
    (the engine self-identifies the card via nvidia-smi so the learned store keys per-card), plus
    `use_subprocess` + `thrash_stall_seconds` from config.
  * **Two new config keys** in `resolve_video_cfg`: `video.thrash_stall_seconds` (default 300) and
    `video.local_use_subprocess` (default **on** = the product path: subprocess-per-attempt with the
    thrash watchdog + fresh CUDA context; off = the in-process spike path).
  * **`ThrashDetected` handling in `run_queue`:** caught as its own branch (like the mid-segment
    Stop), it marks the job `partial` (finished segments resume next run), logs a loud
    degradation message, emits a `VRESULT` fail event, and **stops the whole run** WITHOUT bumping
    the source's `fail_count` (a degraded GPU is not the source's fault, and the next job would just
    thrash again -- reboot to clear it). `_stop_notice` maps the `"gpu thrash"` reason to a **red,
    resume-hinted** summary notification. `auto_resume` is forced off for `--local` (there is no pod
    to lose). Guarded import so the remote/passthrough trees load without the local stack.

**GUI (`gui/tab_video.py`).** A **"Run on:" combobox** ("Local GPU" / "Remote: RunPod", the shared
`gui.common.RUN_ON_*` labels every tool tab uses) at the top of the tab, with the GPU picker on the
same row, gated by the install mode: a single-mode install gets a pinned, greyed-out selector
offering only what it can run (**Remote-only** = Remote, tooltip: re-run setup as Local/Both;
**Local-only** = Local), **Both** offers both and remembers the last choice
(`gui_settings.video_mode`). The mode re-skins the tab without duplicating it:
  * **Readiness:** Local checks only ffmpeg + a local NVIDIA GPU (nvidia-smi via
    `system_telemetry.sample_gpu` / `gpu_name`), not the RunPod key/SSH/volume. It states
    only where the batch size comes from ("Batch size comes from this card's benchmark." /
    "... picked from VRAM and refined as it runs; press Benchmark GPU to calibrate it.");
    the card name + VRAM it used to lead with were dropped once the GPU picker moved onto
    the same row and showed both.
  * **GPU display:** Local detects THIS machine's card (a cheap nvidia-smi, auto-run on entry,
    unlike the remote list which hits the API and stays behind `↻`) and shows it as the single
    choice, shaped like a remote GPU dict (`price=None` marks it free) so `_selected_gpu` / the
    estimate / Start read it unchanged.
  * **Estimate:** history-driven, see section 19. No cost, no funds guard, no GPU-override env.
  * **Auto-resume:** pod-only, so it is greyed out in Local mode.
  * **Start:** Local launches `batch_video_upscale.py <src> <out> --local` after a light,
    cost-free confirm that warns the run can be slow and may degrade (stops loudly if so).

**Tested** (GPU-free): `resolve_video_cfg` local defaults/overrides, `_local_seedvr2_paths`
(default + env-expanded config), `_stop_notice("gpu thrash")`, and a `run_queue` thrash scenario
proving the run stops after the episode, leaves the job `partial` with `fail_count == 0`, and never
touches the following job (`tests/test_video_local.py`). The full 173-test video suite still passes.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 19. As built: the local time estimate (history-driven, honest)

The estimate reuses the remote estimator's **per-output-megapixel** model (so it is
aspect-correct and generalises to any target), with two differences: **no cost** and **no
pod spin-up** term (a local model load is the only warm-up, and it is small next to a
multi-hour queue). The design choice that matters: it is **history-driven and refuses to
fabricate**. We have no honest per-card rate table for consumer GPUs (only the dev's own
3090, and even that varies ~2.5x per output-MP between a bs5 1080p run and a bs17 2x run,
because per-frame cost depends on source size + batch, not output-MP alone). So:

  * **`estimate_queue_local(jobs, gpu_id, conn)`** (in `video_estimate.py`) sums
    `frames x output_MP x seconds_per_MP` per job, no price, no spin-up. It returns `None`
    (the GUI then shows only the work SIZE) rather than guess when a target has no rate.
  * **`local_seconds_per_mp`** prefers the user's OWN measured history
    (`db.gpu_perf`, task `video-mp-<target>`, keyed by the nvidia-smi card name) ->
    `calibrated=True`; else a `LOCAL_RATES` **seed** (currently EMPTY -- the per-card
    benchmark suite, section 16, is what will fill it with rigorously-measured rates) ->
    `calibrated=False`; else nothing. A seeded (un-measured) estimate is shown flagged
    **"(rough)"**.
  * **Self-calibration is now wired for local.** `batch_video_upscale.process_job` recorded
    each segment's real output-MP vs. seconds into `db.gpu_perf` ONLY under the remote
    `IMGTBX_GPU_OVERRIDE` key; it now falls back to **`engine.gpu_id`** (the local card
    name) when there is no override, so a LOCAL run feeds the SAME store the local estimate
    reads back. Passthrough has no `gpu_id`, so it never pollutes the store. One segment of a
    real video is thousands of output-MP -- far past the 300-MP trust floor -- so the
    estimate becomes **calibrated after the first segment**: the first run is covered by the
    in-run live ETA (the progress bar), and every run after shows a real measured time.
  * **GUI** (`_update_local_estimate`): calibrated -> `"N job(s) . ~H:MM:SS . M segments .
    runs on your GPU (no cost)"`; seeded -> the same with `~H:MM:SS (rough)`; neither ->
    the work-size line + "the first segment calibrates it". The local GPU choice's `id` is
    set to the nvidia-smi card name so it matches the perf key the runner records under.

**Tested** (GPU-free, `tests/test_video_local.py`): `None` for an unseeded card with no
history; a measured-history estimate is `calibrated=True` with the exact expected seconds;
a monkeypatched `LOCAL_RATES` seed is `calibrated=False` ("(rough)"); and a queue is `None`
if ANY target lacks a rate. The estimate suite (26 tests) and full video suite still pass.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 20. As built: the per-card VRAM benchmark suite

The suite closes the loop for BOTH earlier systems: it feeds the sizer (section 15) a real
per-card ceiling and the estimate (section 19) a real rate, replacing the conservative seed
with measurement. It is the sanctioned place to push UPWARD to failure, safe to do so ONLY
because each probe is an isolated subprocess (a failed probe's fragmented VRAM dies with its
process instead of poisoning the next, 14.2/14.3).

**Flow.** A modal (`gui/video_benchmark.py`, "Benchmark GPU…" button; Local or Remote mode)
detects the card, loads any prior results, shows a rough runtime estimate, and drives
`scripts/video_benchmark.py`. For each selected target the runner searches for the batch
ceiling on a short clip, via **`LocalVideoEngine.probe_batch`** (local: a fixed-batch,
no-step-down wrapper over the fresh-subprocess `local_video_worker.py`) or
**`RemoteVideoEngine.probe_batch`** (remote: the pod's resident engine, section 22).

The search is a **VRAM-aware geometric climb**, not a linear rung-by-rung sweep (the latter is
untenable now the cap is **3000**: a PRO 6000 at 0.4 MP never reached the old 513 wall, using
only 42/96 GB). Two parts:

- **Floor (VRAM-dependent):** `sizer.vram_floor_batch(total_gb, mp)` inverts the working-set
  model to a starting rung that a big card obviously clears, so the sweep opens near the
  interesting region instead of crawling from 5 (e.g. ~337 for 0.4 MP on 96 GB). Conservative
  by construction (the 3090-offload model over-predicts on a resident big card), which is fine
  because the climb reaches the wall from here in a few doublings.
- **Ceiling (measured):** `next_batch` DOUBLES the largest ok batch until an OOM/thrash
  overshoots the wall, then **binary-refines downward** between the last ok and the failure to
  pin the exact max-fit 4n+1 rung. This is why the runner does NOT stop at the first OOM (the
  overshoot is intentional); the cell ends when `next_batch` returns None. A ~1500 ceiling is
  found in ~a dozen probes instead of ~370. `cell_done` exposes the same terminal test to the
  GUI (floor-independent) so it can render saved vs partial.

**Warm-up (remote only).** `torch.compile` is on by default and compiles per RESOLUTION, so the
FIRST probe of each cell is cold (compile + kernel autotune) and its s/frame is inflated. Before
a cell's measured probes the runner runs `WARMUP_PROBES` (2) discarded **bs9** upscales at the
cell's resolution (`_warmup_cell`), so the cold cost is paid on 9 throwaway frames instead of the
first (large, floor-sized) measured probe. It is REMOTE only: the local path reloads the model in
a fresh subprocess per probe, so every probe is cold by design and there is no warm state to
build. Warmups are best-effort (never fail a run) and are NOT persisted, so they can't pollute the
ceiling or the throughput timing.

**Persistence + resume.** Every probe is written to **`db.video_bench`** the instant it
finishes (`gpu_id, model, out_w, out_h, batch -> outcome/frames/seconds/peak`). So a **Stop**
(stdin `q`, which kills the in-flight probe's subprocess and discards only that partial) is
graceful, and re-opening **resumes the climb/refine** from the recorded probes (`next_batch`
reconstructs the search state, floor-independent, from what's persisted). The pure search logic
(`build_plan`, `vram_floor_batch`, `next_batch`, `cell_done`, `cell_ceiling`,
`throughput_optimal_batch`, `estimate_runtime`) is separated from the GPU driving and
unit-tested.

**Outputs.** Per finished cell, `_record_cell_result` copies the ceiling into the sizer's
learned store (`db.put_learned_batch`, `gpu|model` + MP bucket) and the ceiling probe's timing
into the estimate store (`ve.record_benchmark_rate` -> `db.gpu_perf`, low trust floor since a
benchmark probe is a clean controlled measurement). The sizer now **honours a learned value
above the AUTO cap** (a measurement beats the safety cap); the seed path stays capped. The
local estimate's read floor dropped to **40 MP** (`LOCAL_MIN_MP`) so one short benchmark clip
registers.

**Completion notification.** When a sweep ends, `_notify_benchmark` fans out one alert through
the shared `notifications` layer (Discord / Telegram / ntfy, whatever is configured in Settings),
exactly like a real run: green **"Video benchmark complete"** or, on a Stop / funds-guard trip,
orange **"Video benchmark stopped"** with a resume hint, plus one field per target giving the
saved batch (and the max fit when it differs). This matters most for a **remote** sweep, which can
run for hours on a rented pod. Fail-safe and a no-op when no backend is configured.

**Terminal summary table.** On finish, `_log_summary_table` also prints a compact fixed-width
table to the log, mirroring the GUI Results table (`target -> output / MP / max fit / saved batch /
overlap / best s-per-frame / peak VRAM / runtime / status`), read from the persisted probes (the
same source of truth the GUI renders from). So a user reading the log, headless or scrolled back
through a long per-probe sweep, gets the same at-a-glance verdict without hunting the individual
probe lines. Fail-safe: any error just skips the table.

**The benchmark sources** (`benchmark_clip.py`) are a fixed set of **five Creative-Commons
videos**, SHA-256 pinned in `benchmark_clip.SOURCES` and downloaded **on demand** (lazily, only
the ones a run touches) into `samples/videos/`: Big Buck Bunny at 640x360 / 1280x720 / 1920x1080
(16:9; the 1080p one is delivered as a `.zip` whose **extracted** mp4 is what the pin verifies)
plus two Wikimedia clips at 320x240 (4:3) and 240x320 (3:4) for the aspects 16:9 Big Buck Bunny
can't provide. Every cell is fed its **native, aspect-matched** source with NO rescale, and the
engine upscales it to the cell's target (`resolution` = the output short side) -- a **real**
input->output ratio (2x-5x depending on the cell), not a synthetic "half the output"
placeholder, so every user benchmarks identical footage and the numbers are comparable
machine-to-machine. Content is irrelevant to the ceiling (set by OUTPUT size, section 14), so:
the first X frames are cut per probe and the short source is **looped** (`-stream_loop`) when a
batch exceeds its frame count; a fetch failure falls back to an ffmpeg `testsrc2` clip at the
native size with a loud warning (those numbers aren't comparable). Downloads use certifi trust
(`net_ssl`) and a Wikimedia-policy User-Agent; an unpinned/mismatched download is refused.

**VRAM-contention guard + tag.** Other GPU apps (a 3D tool, a slicer, a browser) holding VRAM
would make a sweep OOM early and record an artificially LOW ceiling that then caps every real
run. Two protections: (1) the modal samples used VRAM on open and again at Start and, above
**2.5 GB** occupied, shows a prominent (non-blocking) warning -- *"Close all non-essential
applications for best benchmarking results…"*; (2) each probe records the **free VRAM at probe
start** (`video_bench.free_vram`), so `next_batch` treats a failure that happened with materially
less headroom than is free NOW (`STALE_MARGIN_GB` = 1.5) as a contention artifact and RE-PROBES
it (it also doesn't cap the sweep below itself) -- a later clean run cleanly supersedes a
contended one instead of being permanently capped by it. The regular LOCAL upscale confirm also
now advises closing non-essential apps + minimising machine use.

**Tested** (GPU-free, `tests/test_video_benchmark.py`, 37 tests): the sweep logic
(series/plan/cell/next-batch/ceiling/estimate), stale-contended-failure re-probe (incl. below a
trustworthy failure), the `video_bench` round-trip + clear + `free_vram` tag, the clip download
integrity (unpinned refused, hash verified/mismatch-deleted via a `file://` URL), an
**end-to-end sweep with a fake engine** (records 5/9/13 ok + 17 oom, learns 13), a **resume**
(pre-seeded 5/9, continues at 13), the **completion notification** (complete vs stopped
title/colour, per-target fields, no-op without a backend), and the **terminal summary table**
(max-fit vs saved-batch distinction, saved probe's peak, summed runtime, unbenchmarked row).
Full suite passes.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 21. As built: dynamic ratio targets + the per-GPU feasibility guard

Two problems in one: a 24 GB card would happily let you pick 4K (guaranteed OOM), and a
low-res source had no sane target between the presets. Both are now solved by a single
notion, the **max feasible OUTPUT megapixels** of the selected GPU, plus **2x/4x ratio
targets** computed from each source.

**Target model.** `video_estimate` gained ratio target tokens (`2X`/`4X`, product decision:
those two only) alongside the presets. `fit_scale` -- the one function the pipeline already
routes resolution through -- returns the ratio for a ratio token and the box-fit scale for a
preset, so `output_dims` / `fit_short_side` / `output_megapixels` / `classify_upscale` (and
therefore `process_job`'s `--resolution`) handle ratios with no other change. A target is
stored as its token (`2X` / `1080p` / ...); its concrete output is derived per source. Ratio
outputs are capped at 4K, so a ratio is a low-res-source feature (a 4K source still reads as
"nothing to upscale to").

**Feasibility.** `max_output_mp(total_vram_gb, gpu_id, conn)` returns the largest output-MP a
card is believed to reach: a per-VRAM-tier SEED (24 GB = 1080p / ~2.1 MP, measured on the
3090, the user's call to allow the tight 1080p; smaller tiers conservative), RAISED by
anything the card's own benchmark/learned data proves (`db.max_feasible_output_mp`, never
lowered). `source_eligible_targets` enumerates the ratios + presets that are a real upscale
(deduped by output dims: a 960x540 source's "2x" IS 1080p, so the preset label wins);
`feasible_targets` filters that to `<= max_output_mp`; `target_label` shows each as a concrete
resolution (`2x (640x480)`, `1080p (1440x1080)`).

**GUI.** The Target combobox lists only feasible targets for the currently selected GPU
(local: the detected card, benchmark-aware; remote: the picked pod), labelled with their
output size. The queue **greys** any job the selected GPU can't reach (re-evaluated when the
GPU selection changes), and **Start is refused** if nothing in the queue fits; when some jobs
fit and others don't, the run proceeds on the feasible ones and reports how many were skipped.

**Runner.** The GUI passes the card's `IMGTBX_MAX_OUTPUT_MP`; `run_queue` DEFERS (leaves
`pending`, logs once) any job whose output-MP exceeds it, rather than attempting and
OOM-failing it -- so a mixed queue runs what fits and keeps the rest for a bigger card. The
remote cost estimator approximates a ratio target's rate with the 1080p preset's s/MP, so the
GPU picker still ranks cards for a ratio-target queue.

**Examples (as tested).** A 320x240 source offers `2x (640x480)` + `4x (1280x960)` + `1080p`
on a 24 GB card, `+ 1440p` on a 32 GB card, and only `2x` on a 12 GB card. The dedupe, the
benchmark-raises-the-cap path, and the runner defer are unit-tested
(`tests/test_video_targets.py`, 9 tests). Full suite (522) passes.

**Not yet:** the arbitrary explicit-resolution entry (the other half of section 6), and a
VRAM-aware remote GPU-list pre-filter for ratio targets (the queue-grey + Start guard cover
correctness meanwhile).

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 22. Planned: extending the benchmark to remote pods (0.5.0-experimental)

Sections 20/21 benchmark the LOCAL card. This extends the SAME suite to a rented RunPod GPU,
so a user can measure the optimal batch (temporal window) for a card they intend to upscale
ON, before paying for a real run. It is NOT a local-vs-remote comparison: Windows and Linux,
resident vs offload, and the pod's `cudaMallocAsync` allocator all differ enough that
cross-machine GPU-to-GPU numbers would be meaningless. The remote benchmark's only job is to
learn "the best batch for THIS card, upscaling on a pod", and feed it back to remote runs.

### 22.1 Method: mirror production, in-process on the pod (no probe isolation)

The local suite isolates each probe in a fresh subprocess (20), because a failed probe's
fragmented VRAM poisons the next one on Windows (14.2/14.3). That model does NOT carry to the
pod, and it must not: a resident worker loads the 7B model once, and restarting it per probe
would reload from the network volume (a minute-plus each) tens of times per sweep, all billed.
So the remote benchmark runs each probe **in-process in the resident worker**, exactly the way
a real segment runs (`process_video`), with `reset_peak_memory_stats` -> one window ->
`max_memory_reserved` -> `empty_cache` between probes. This is what `pod/bench_video.py`'s
inner loop already did (its Phase-1 measurement tool); the extension turns that loop into a
worker endpoint the existing sweep orchestration can drive. On Linux + `cudaMallocAsync`, freed
memory returns to the driver pool between probes, so an in-process upward sweep measures a
representative ceiling; and since the goal is "what a real pod run does", mirroring the
production path is the RIGHT measurement anyway, not a compromise.

### 22.2 Reuse: the engine seam again

`scripts/video_benchmark.py`'s `run_benchmark` already talks to the engine ONLY through
`engine.probe_batch(clip, out, resolution=, batch=, frames=, should_stop=)` (20). So the
extension is an **engine swap**, mirroring how "local video" was mostly swapping the injected
engine (section 4):

- **`pod/worker.py` gains `POST /video/probe`** (async, like `/video/submit`): upload a short
  clip + `resolution` + `batch` + `overlap`, the worker runs ONE window in-process with **no
  OOM step-down** (the sweep must SEE the OOM to find the ceiling, so the production recovery
  is deliberately bypassed here), and reports `{outcome: ok|oom|error, seconds, frames,
  peak_alloc_gb, peak_reserved_gb}` via `/video/status`. No `/video/fetch` (the upscaled
  output is discarded; only the measurement matters).
- **`remote_video_engine.py` gains `RemoteVideoEngine.probe_batch`** with the IDENTICAL
  signature + return contract as `LocalVideoEngine.probe_batch` (never raises on OOM; returns
  the outcome dict). It submits to `/video/probe`, polls, and returns the dict.
- **`run_benchmark` becomes engine-agnostic**: a `--remote` path constructs the remote engine
  (via a deployed pod, below) instead of `LocalVideoEngine`; the sweep, `next_batch`, resume,
  `drop_collapsed`, `throughput_optimal_batch`, persistence and `@@TBX@@` events are reused
  unchanged.

The clip is prepared **locally** (`benchmark_clip.ensure_source_clip`: the pinned native source,
downloaded + cached once, cut to the first X frames) and uploaded per probe. It is small relative to the probe's GPU time (a few MB even at a big batch), so a
per-probe upload is cheaper than the machinery to synthesise it pod-side, and it keeps
`run_benchmark` identical across local/remote.

### 22.3 Keying: write where the remote run reads (the linchpin)

The remote RUN's adaptive batch tuner already CONSUMES learned batches, with no change needed:
`process_job` reads `db.get_learned_batch_ge(IMGTBX_GPU_OVERRIDE, mp_bucket)` (the RunPod GPU
id from the picker, `_mp_bucket` on the fine 0.05-MP grid) to seed the first segment. So the
remote benchmark simply has to WRITE the learned batch under that same key:

- **Learned batch:** `db.put_learned_batch(gpu_id, mp_bucket, saved)` with `gpu_id =
  IMGTBX_GPU_OVERRIDE` (the RunPod id) and **NO `|model_tag` suffix** -- the remote run reads
  the PLAIN id, whereas the local sizer reads `f"{gpu_id}|{tag}"`. `_record_cell_result` picks
  the key by mode. Matching the run's *current* (un-model-qualified) behaviour is a deliberate
  scaffolding decision: benchmarking with 7B then running 3B would cross-feed, exactly as the
  run already behaves today; model-qualifying BOTH sides is a separate later cleanup, not part
  of this.
- **`gpu_id` for the sweep:** local uses `_query_gpu_name()`; remote uses `IMGTBX_GPU_OVERRIDE`
  (the pod's card is not the local card). The per-probe resume rows (`db.video_bench`, keyed
  `gpu_id, model, out_w, out_h, batch`) keep their own `model` column and are unaffected.
- **Local vs remote never collide:** the id namespaces are disjoint (local nvidia-smi name vs
  RunPod id, noted in `db.py`), so a card benchmarked both ways keeps two independent buckets,
  which is correct (resident-remote and offload-local ceilings genuinely differ).

**Known gap (rate, not batch):** `_record_cell_result` also records a rate into `db.gpu_perf`.
The remote ESTIMATE reads that with a 300-MP trust floor (`seconds_per_mp`), which one short benchmark probe won't clear, so the estimate won't immediately pick up remote benchmark timing (the local path dropped its floor to 40 MP for exactly this reason). The BATCH (the actual "optimal setting" this feature exists to learn) is unaffected (the run's tuner has no such floor). Lowering the remote rate floor for benchmark-origin data is a documented follow-up, not scaffolding.

### 22.4 Pod lifecycle, cost, and the UI flow

The benchmark needs a deployed pod, wired the same way a run is (`remote_run.RemoteSession`,
`mode="video"`):

- **GPU choice comes from the picker.** On the Video tab: pick **Remote**, refresh the GPU
  list, select the card, press **Benchmark GPU…**. The window opens with that selection, and
  Start deploys a pod for THAT card (`IMGTBX_GPU_OVERRIDE` = the picked id). If the card is out
  of stock at deploy time the run fails cleanly and the message lands in the benchmark log
  (`RemoteSession` already emits "Pod start failed …"; no GPU substitution, 0.4.0).
- **Cost = live accrual + funds guard**, no separate cap (a sweep runs to OOM so its runtime
  is unpredictable; a cap would cut a sweep mid-cell). The same `funds_guard` + dead-man's
  switch + `$/h` readout a run uses; teardown on completion, Stop, and failure via the run's
  `close_session` semantics.
- **GUI delta (small).** Today the "Benchmark GPU…" button is Local-only (hidden in Remote by
  `_apply_mode_ui`) and `_open_benchmark` passes no GPU. The change: show the button in Remote
  too, pass `_selected_gpu()` into `BenchmarkWindow`, and give the window a remote branch that
  skips the local `system_telemetry` detect (using the picked card's name + VRAM), swaps the
  local "other apps hold VRAM" warning for pod-deploy/cost status, and gates feasibility on the
  picked card's VRAM (a rented 5090/H100 reaches 4K, which the local gate would forbid).
- **Telemetry row (+ MQTT), for parity with the tabs.** The window carries a live `TelemetryRow`
  just like the Upscaler/Video tabs: LOCAL shows this machine's CPU/RAM/GPU (registered with the
  App's sampler, so it also publishes `system/*`); REMOTE shows the POD's readout, fed by the
  runner streaming `RTELEM` events (`bv._start_remote_telemetry`, the same sampler a real run
  uses) into `App.apply_remote_telemetry`, which also publishes `system/remote/*`. The remote row
  and its retained MQTT topics are zeroed when the sweep ends / the pod is torn down
  (`clear_remote_telemetry`), so Home Assistant sees no stale readings.

### 22.5 Build order

1. `pod/worker.py`: `POST /video/probe` (+ its status/outcome), in-process, no step-down.
2. `remote_video_engine.py`: `RemoteVideoEngine.probe_batch` (submit/poll, outcome dict).
3. `scripts/video_benchmark.py`: `--remote` / engine-agnostic `run_benchmark`; mode-aware
   `gpu_id` + learned-batch key in `_record_cell_result`.
4. Remote deploy/teardown + funds guard wiring in the remote benchmark path (reuse
   `RemoteSession` + the run's session helpers).
5. GUI: button visibility + pass the selected GPU + `BenchmarkWindow` remote branch.
6. Tests (GPU-free): the remote key selection, the engine-agnostic sweep against a fake remote
   engine, `probe_batch` outcome mapping.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

Public/shared benchmark aggregation (results surfaced in the GPU picker as expected
performance) stays a later phase; this section is the scaffolding that makes a remote card
benchmarkable and its result consumed by the user's own remote runs.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>

## 23. As built: benchmarking the Real-ESRGAN method (local + remote, 0.5.6)

The per-card benchmark grew a second METHOD alongside SeedVR2, so the fixed-ratio Real-ESRGAN
engine (#11, remote #18 B) is measurable on the same card the same way. A Real-ESRGAN benchmark
is fundamentally different from a SeedVR2 one and the code reflects that:

- **No batch sweep.** A fixed-ratio GAN runs at batch 1 (batching only raises VRAM, never
  throughput, docs 14 / `fixed_ratio_engine`), so there is no VRAM ceiling to climb. Each cell is
  a SINGLE probe measuring **s/frame + peak VRAM**. No `torch.compile` modes either. The clip is
  sized by DURATION (`DEFAULT_ESR_SECONDS`, 10 s) from each source's native fps (`_esr_frames`,
  looping a shorter source), not a fixed frame count: ~250-300 frames averages out per-frame noise
  without a long benchmark (~3-7 min/cell at the quality tier). Tunable headless via `--esr-seconds`.
- **Cells are (ratio, native source), across the input sizes a real run spans.** The output is
  `source x ratio` with NO rescale. `2X`: 320x240 -> 640x480, 640x480 -> 1280x960, 1280x720 ->
  2560x1440, 1920x1080 -> 3840x2160 (4K). `4X`: 320x240 -> 1280x960, 640x480 -> 2560x1920 (1920p
  tall). A source can appear at BOTH ratios (`ESRGAN_CELL_SOURCES`); the 320x240 source reuses the
  shared SeedVR2 `p4x3` clip (given an `fps` for the duration sizing). Sources are pinned Wikimedia
  clips fetched on demand. Ratios are gated to a tier's NATIVE scales (`esrgan_targets`): compact =
  `4X` only, quality = `2X` + `4X`, so a probed frame is never ffmpeg-resized (2x uses the native
  `RealESRGAN_x2plus`, not x4-then-downscale).
- **Storage reuses `video_bench`.** Probes are keyed by `model = esrgan-<tier>` (`esrgan_bench_model`),
  `out_w`/`out_h` = the cell, `batch = 1`. No schema change. The GUI renders the batch columns as
  `—` for these rows and shows s/frame + Peak VRAM instead.
- **The estimator closes the loop.** Each probe records the per-tier rate via
  `video_estimate.record_esrgan_rate` (a NEW `esrgan-mp-<tier>` gpu_perf namespace, since a GAN is
  ~100x cheaper than diffusion so it must NOT read the SeedVR2 RATES table). `estimate_job` /
  `estimate_queue*` route a `fixed_ratio` job to `esrgan_seconds_per_mp(gpu, tier)`; exact-tier
  only (compact and quality differ ~16x, never proxied), and an unmeasured card estimates as
  "unknown" (None) rather than a wrong guess. This is what filled the "fixed_ratio RATES" gap the
  #18 B as-built deferred.
- **Unified method: one run/pod sweeps ALL tiers.** `run_esrgan_benchmark(ratios, tiers, ...)`
  takes a LIST of tiers and concatenates each tier's plan (`build_esrgan_multi_plan`), recording
  each cell under its OWN tier's `esrgan-<tier>` model. The point is REMOTE: a fixed-ratio worker
  swaps its model per job, so compact + quality both run on ONE volume-free esrgan pod, deployed
  once before the loop and torn down once after (no terminate/redeploy between tiers). Headless:
  `--engine esrgan --ratios 2X,4X --esrgan-tiers compact,quality` (omit `--esrgan-tiers` for all).
- **Local vs remote, one code path.** Both engines share `process_segment`, so `_run_esrgan_cell`
  drives either. LOCAL builds a `FixedRatioVideoEngine` per cell (a per-tier per-ratio weight,
  lazy-downloaded + hash-verified). REMOTE reuses the resident pod engine across every cell/tier
  (the worker already reports `seconds` + peak VRAM), plus the SeedVR2 remote benchmark's
  deploy/telemetry/funds/teardown.
- **GUI.** The Benchmark window has a persistent **Method** combobox: SeedVR2, or a SINGLE unified
  **Real-ESRGAN** entry (tier=None) that benchmarks both tiers at once. Switching rebuilds the
  checkbox area (SeedVR2 ladder/presets vs the 2X/4X ratio checkmarks) and the results table, which
  for ESRGAN is NOT a full grid: rows are the valid `(cell, tier)` pairs (a 2X cell has only a
  quality row, a 4X cell has both), keyed via `_row_pairs`. The #0 column shows the tier
  (COMPACT/QUALITY) instead of a compile mode.

<div align="right"><a href="#local-video-upscaling-design">↑ Back to top</a></div>
