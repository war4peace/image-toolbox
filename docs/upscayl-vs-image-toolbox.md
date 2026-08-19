# Upscayl vs Image Toolbox

A feature-by-feature comparison between [Upscayl](https://github.com/upscayl/upscayl)
and **Image Toolbox** (this project).

Written 2026-07-27 against Upscayl's public repository / website; the Image Toolbox
column was refreshed 2026-07-29 for **0.6.0**. Check their repo before
relying on their column: this is a snapshot of one day's reading, and their side can
change without this file noticing.

**This is not a competitive pitch.** The two tools are not chasing the same users,
so there is nothing to win by overstating one or understating the other. The point
of the document is to help someone pick the right tool for their situation, which
is often Upscayl. Where Image Toolbox falls short it is written down plainly
(section 5), and where a limitation is a deliberate design choice that is stated as
explanation, never as a defence.

---

## 1. The short version

The two tools overlap on exactly one thing: *"take a low-resolution image and make
it bigger with an AI model, locally, for free."* Almost everything else is
different, because they were built for different jobs.

**Upscayl is a polished, cross-platform, general-purpose image upscaler.** It runs
Real-ESRGAN class models through **ncnn + Vulkan**, so it works on **any**
Vulkan-capable GPU (NVIDIA, AMD, Intel, Apple Silicon) on **Windows, macOS and
Linux**. It has a large model library, custom-model import, a slick before/after
UI and a CLI. It does one thing and does it well: fixed-ratio image upscaling.

**Image Toolbox is a Windows-only photo/video collection restoration pipeline.**
Upscaling is one of four tools. It runs **SeedVR2** (a diffusion upscaler) and
**Real-ESRGAN** through **PyTorch + CUDA**, so it is **NVIDIA-only**, but it covers
things outside Upscayl's scope: **video upscaling**, **vision-model
image tagging and renaming**, **replacing originals with the processed results**,
**resumable multi-thousand-file batch runs**, **renting a cloud GPU by the second
when the local one is not good enough**, and **home-automation / notification
integration**.

Rules of thumb:

- Want to upscale a handful of images, on a Mac / on AMD / on Linux, right now,
  with minimum ceremony: **Upscayl**.
- Want to run 40,000 family photos and a shelf of old camcorder footage through an
  unattended, resumable, restartable pipeline on an NVIDIA card (or a rented one):
  **Image Toolbox**.

---

## 2. Positioning at a glance

| | Upscayl | Image Toolbox |
|---|---|---|
| **What it is** | AI image upscaler | Photo + video collection restoration toolbox |
| **Scope** | One job, done well | Upscale + tag/rename + video + conciliate |
| **Platforms** | Windows 10+, macOS 12+, Linux | Windows 10/11 only |
| **GPU vendors** | NVIDIA, AMD, Intel, Apple Silicon (any Vulkan GPU) | NVIDIA only (CUDA) |
| **Backend** | ncnn + Vulkan (`upscayl-ncnn`) | PyTorch + CUDA, in-process |
| **UI toolkit** | Electron (React) | Python standard-library tkinter |
| **Install size** | Small (app + model files) | Modest for a remote-only install; large for a local one (GPU stack + model weights) |
| **License** | AGPL-3.0 (backend AGPLv3) | Personal project, source public |
| **Maturity / audience** | Large, mainstream, heavily used (47.6k stars) | Small personal project, "vibecoded", experimental features |
| **Development activity** *(measured 2026-07-27)* | Last release **2024-12-25**; last commit to `main` **2026-03-27**; **10 commits** in the previous 12 months; 55 open issues, the oldest from **April 2023** | Last release **2026-07-26**; ~400 commits in the previous 12 months |
| **Paid tier** | None (a "Upscayl Cloud" waitlist exists in-app) | None. Optional rented RunPod GPU, paid to RunPod, not to the author |

**On that activity row, fairly:** a low commit count is not automatically a fault.
Upscayl is a focused tool that largely does what it set out to do, and finished
software does not need commits; 47.6k users are not using something broken. But it
is decision-relevant, so it belongs in the table: at this cadence, expect no new
models, no new platform or GPU support, and no fix for a bug you hit. The oldest
open issue dating to April 2023 is the practical version of that. Read it as
"stable and quiet", not as "dead", and weigh it against the fact that Image
Toolbox's high commit rate is one hobbyist's spare time and could stop the day he
loses interest. Neither project offers a support guarantee.

(One data caveat: GitHub reports `pushed_at` for the Upscayl repo as 2026-07-21,
which looks current, but that field counts a push to **any** ref. The `main`
history is the honest signal and it ends 2026-03-27.)

---

## 3. Feature matrix

Legend: ✅ has it · ⚠️ partial / with caveats · ❌ does not have it.

### 3.1 Image upscaling core

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Local AI image upscaling | ✅ | ✅ | The shared core. |
| 100% offline processing | ✅ | ✅ | Image Toolbox only leaves the machine if you explicitly pick a remote pod. |
| Real-ESRGAN family models | ✅ | ⚠️ | Upscayl: for images. Image Toolbox: Real-ESRGAN is a **video** method; images always use SeedVR2. |
| Diffusion upscaler (SeedVR2) | ❌ | ✅ | Slower, heavier, generally higher quality on degraded photos. |
| Multiple bundled model choices | ✅ | ⚠️ | Upscayl: Standard, Lite, Remacri, Ultramix, Ultrasharp, Digital Art, High Fidelity. Image Toolbox: SeedVR2 3B Q8 / 7B FP8 / 7B FP16 (size tiers, not styles). |
| Custom / user-supplied models | ✅ | ❌ | Upscayl imports custom ncnn models (PyTorch models must be converted first). |
| Fixed ratio output (2× / 3× / 4×) | ✅ | ❌ | Image Toolbox images target a **resolution**, not a ratio. |
| Chained double pass (up to 16×) | ✅ | ❌ | "Double Upscayl". |
| Resolution target (4K / 2K / 1080p) | ❌ | ✅ | Image Toolbox caps output so results display natively on common screens. |
| Custom output width | ✅ | ❌ | |
| Skip images already near target | ❌ | ✅ | Skip-cutoff, default 66% of target. |
| Tile size control | ✅ | ✅ | Both expose it. Upscayl: one tile-size setting (0 = auto from GPU memory). Image Toolbox: **Settings → SeedVR Settings** has separate VAE tiled *encode* and *decode* toggles, each with its own tile size (default 1024, both off), shared by the Batch Upscaler and the Video Upscaler. |
| TTA mode (quality-over-speed) | ✅ | ❌ | |
| Cannot de-blur / fix focus | ✅ (stated) | ✅ (same limit) | Both are super-resolution, not restoration of lost focus. |

### 3.2 Batch processing and long runs

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Batch a whole folder | ✅ | ✅ | |
| Recursive subfolder walk, output tree mirrored | ❌ | ✅ | Upscayl's batch is folder-in / folder-out. Image Toolbox mirrors the whole source tree. |
| Resume a stopped batch | ❌ | ✅ | SQLite cache in `db/cache.db`; a stopped run continues where it left off. |
| Second pass for files that appeared mid-run | ❌ | ✅ | |
| Corrupt / missing file detection, logged and skipped | ❌ | ✅ | Listed at the end for review. |
| Pause / Resume (freeing the GPU while paused) | ❌ | ✅ | Pause unloads the models, ~16.6 GB returned on a 3090, then reloads on Resume. |
| Stop that finishes the current file cleanly | ⚠️ | ✅ | Upscayl has a Stop; Image Toolbox guarantees no half-written file. |
| Progress bar + ETA | ✅ | ✅ | |
| Taskbar progress + attention flash | ❌ | ✅ | Windows `ITaskbarList3`; run progress on the taskbar button. |
| Degraded-GPU watchdog | ❌ | ✅ | Detects the driver/VRAM slowdown that only a reboot cures, stops cleanly, alerts. |
| Overwrite-previous-output toggle | ✅ | ⚠️ | Image Toolbox instead **never** overwrites: output goes to a separate mirrored tree. |
| Works on mapped network drives | ⚠️ | ✅ | Explicitly supported and used in Image Toolbox. |

### 3.3 Video

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Video upscaling | ❌ | ✅ | Image Toolbox's largest feature after the image upscaler. |
| Targets (1080p / 1440p / 4K, plus 2× / 4× for tiny sources) | ❌ | ✅ | Box-fit, per video, from the actual frame dimensions. |
| Two engines (SeedVR2 / Real-ESRGAN) | ❌ | ✅ | Method switch, quality vs speed and VRAM. |
| Segment-level resume ("pay in installments") | ❌ | ✅ | Split into ~1-minute segments; every finished segment is banked. |
| Per-run minute / dollar caps | ❌ | ✅ | A big job is paid in affordable pieces. |
| Original audio preserved, muxed back | ❌ | ✅ | Plus a duration-drift check. |
| Extract and upscale one scene | ❌ | ✅ | Mark in/out on a live preview, queued like a whole video. |
| Interlaced-source handling (deinterlace) | ❌ | ✅ | MiniDV-era 576i sources otherwise upscale combed, or come out black. |
| Side-by-side real-time playback with sound | ❌ | ✅ | Bundled libVLC. |
| H.265 10-bit deliverable, selectable codec | ❌ | ✅ | |
| Survive losing a cloud GPU mid-run | ❌ | ✅ | Opt-in Auto-resume: reconnect, or wait for the same card and redeploy. |

### 3.4 Beyond upscaling

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Vision-model image tagging (description into EXIF) | ❌ | ✅ | Local or remote Ollama, default `qwen3-vl:8b-instruct`. |
| Automatic descriptive renaming | ❌ | ✅ | `OriginalName_Condensed_Description.ext`. |
| Selectable description language | ❌ | ✅ | |
| One-click Undo of tags/renames | ❌ | ✅ | Every change recorded before anything is modified. |
| Auto-straighten sideways photos (CNN) | ❌ | ✅ | Applied before upscaling **and** before tagging; confident calls only. |
| Replace originals with processed results (Conciliation) | ❌ | ✅ | Archive or delete, content-hash lineage matching, non-destructive preview first. |
| Metadata copied from original | ✅ | ✅ | Was a real gap; closed by roadmap #13 in 0.5.9. Both have an on-by-default copy-metadata toggle. Image Toolbox additionally normalises Orientation (its pipeline has already applied it) and drops the stale embedded thumbnail, and Conciliation backfills anything upscaled before the fix. |

### 3.5 Comparison and preview UI

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Before/after slider | ✅ | ✅ | Both. Image Toolbox's is a floating, resizable window with shared zoom/pan. |
| Adjustable zoom while comparing | ⚠️ | ✅ | Upscayl's lens is a **fixed 4×**. Image Toolbox: mouse wheel, centred on the pointer, fit up to 400% of the upscaled image's native pixels, with drag-pan, and both sides stay locked together. |
| Hover magnifier ("lens view") | ✅ | ✅ | A different interaction, not more zoom: the lens shows one patch as original **and** upscaled at once, side by side, where a wipe shows it as one or the other. Closed by roadmap #14 in 0.6.0. Three differences from Upscayl's: the magnification is the **actual upscale ratio** rather than a hard-coded 4×, so the upscaled panel is exactly 1:1 with the file that was produced; the wheel **zooms the lens** 1×/2×/4×/8× on top of that, growing the panels with the window instead of a fixed stamp; and a click **pins** the lens (Upscayl's is hover-only). Works on the video comparison window too. |
| Re-open a finished batch later and compare | ❌ | ✅ | Upscayl compares the image it has just produced; there is no browser over an output folder from an earlier session. Image Toolbox's **Browse upscaled…** (0.6.0, roadmap #22) opens the output tree at any time: folder tree, paged thumbnail wall, and double-click opens the same comparison window, wipe and lens included. It pairs each upscaled photo back to its original from the mirrored folder structure (so it works on a tree produced months ago, by another install, or after the cache was deleted), follows files Tag & Rename has since renamed, and an opt-in content match handles originals that were moved or renamed. |
| Drag and drop input | ✅ | ❌ | Image Toolbox is folder-driven. |
| Thumbnail wall of results | ❌ | ✅ | Outcome-coloured frames: green comparable, red failed, blue in progress. During a run on the tab, and over any already-upscaled folder in the browser above. |
| Right-click context menu per result | ❌ | ✅ | Open original/upscaled, open folder, compare, copy path. |
| Resolution shown before processing | ✅ | ✅ | |
| Frame-accurate video before/after wipe | ❌ | ✅ | Timestamp-aligned scrubbing and frame stepping. |

### 3.6 Hardware, cost and remote execution

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Runs on AMD / Intel / Apple GPUs | ✅ | ❌ | The single biggest advantage Upscayl has. |
| Runs on NVIDIA | ✅ | ✅ | |
| Runs on integrated GPUs | ⚠️ | ❌ | Upscayl: "many iGPUs do not work", but it can. |
| GPU selection on multi-GPU machines | ✅ | ✅ | Upscayl: GPU ID setting. Image Toolbox: a per-tab "Run on" + GPU picker (0.5.8). |
| Rent a cloud GPU by the second | ❌ | ✅ | RunPod, with live catalog, stock and price; ~0.19 USD per 100 images on an A40. **More remote GPU providers planned** (packet.ai evaluated, see `docs/packet-ai-secondary-gpu.md`). |
| Cost estimate before renting | ❌ | ✅ | Shown before any pod is created. |
| Spending guard (balance floor, session cap) | ❌ | ✅ | `funds_guard.py`. |
| Per-card benchmark tool | ❌ | ✅ | Measures the real batch ceiling and s/frame per target; calibrates estimates. |
| Crowdsourced benchmark corpus | ❌ | ✅ | Pulled from GitHub at launch; contribute back via a pre-filled issue. |
| VRAM floor | ⚠️ | ⚠️ | Upscayl: modest. Image Toolbox: 16 GB minimum locally, and SeedVR2 video wants far more. |

### 3.7 Integration, automation and operations

| Feature | Upscayl | Image Toolbox | Notes |
|---|:--:|:--:|---|
| CLI / headless | ✅ | ✅ | Upscayl: `upscayl-ncnn`. Image Toolbox: every runner is a standalone script. |
| Desktop notification on completion | ✅ | ✅ | |
| Discord / Telegram / ntfy notifications | ❌ | ✅ | Any combination, each independent and fail-safe. |
| MQTT / Home Assistant integration | ❌ | ✅ | Live task state, progress, ETA, last-run summary, plus ready-made HA dashboards. |
| Live system telemetry (CPU/RAM/VRAM/temp/power/clock) | ⚠️ | ✅ | Upscayl shows system info; Image Toolbox samples continuously. |
| Per-run usage graphs | ❌ | ✅ | Four capacity-pinned matplotlib charts with a crosshair readout. |
| In-app update checker | ✅ | ✅ | Both. |
| Code-signed Windows installer | ❌ | ❌ | **Neither.** Both trigger the SmartScreen "unknown publisher" prompt and both tell the user to click through it in their README. Not a differentiator. |
| Crash logging to file | ⚠️ | ✅ | Upscayl has a copy-logs utility; Image Toolbox writes `logs/crash_*.log` plus a native dialog. |
| First-start hardware-aware setup wizard | ❌ | ✅ | Detects the GPU and recommends the model tiers that fit its VRAM. |
| Anonymous usage telemetry (opt-out) | ✅ | ❌ | Image Toolbox collects nothing, ever. |
| Localised UI (many languages) | ✅ | ❌ | Image Toolbox's UI is English only (the *descriptions it writes* are multilingual). |
| Light/dark theme | ✅ | ❌ | Native Windows (`vista`) look, light only. Investigated and dropped: the native `ttk` themes cannot be recoloured, so dark mode means leaving the native look in *both* modes. See [`dropped-ideas.md`](dropped-ideas.md#lightdark-theme-2026-07-28). |
| Tooltips on every control | ⚠️ | ✅ | ~160 plain-language tooltips; money- and data-affecting ones lead with the consequence. |

### 3.8 Formats

| | Upscayl | Image Toolbox |
|---|---|---|
| Image input | PNG, JPG/JPEG, WebP | Common still formats (JPEG, PNG, WebP, BMP, TIFF...) |
| Image output | PNG, JPG, WebP (lossy/lossless compression settings) | Same extension as the source, written atomically |
| Video | none | Common container/codec input; H.265 10-bit output by default |

---

## 4. What each does that the other simply cannot

**Only Upscayl:**

1. Runs on macOS and Linux.
2. Runs on AMD, Intel and Apple Silicon GPUs.
3. Imports arbitrary custom ncnn models, so the community model ecosystem is open to it.
4. Fixed 2×/3×/4× and 16× chained upscaling, and a custom output width.
5. A localised, themed, drag-and-drop desktop UI.
6. Small download, trivial install, works within a minute of first launch.

**Only Image Toolbox:**

1. Video upscaling at all, with audio, resume, deinterlacing and scene extraction.
2. Vision-model tagging and descriptive renaming, with Undo.
3. Conciliation: putting the processed files back in place of the originals, safely.
4. Multi-thousand-file resumable runs with a watchdog, pause that frees the GPU, and corrupt-file triage.
5. Renting a cloud GPU by the second, with cost estimates, spending guards and benchmark-calibrated predictions.
6. Home Assistant / MQTT, Discord / Telegram / ntfy, telemetry graphs.
7. Auto-straightening sideways photos before processing.

---

## 5. Honest weaknesses of Image Toolbox against Upscayl

Written down deliberately, because a comparison that only flatters the home team
is useless.

- **NVIDIA-only, Windows-only.** Upscayl's Vulkan/ncnn backend is the more
  portable engineering choice. This is not a small gap; it is most of the
  potential user base.
- **Far heavier.** A local install pulls a full CUDA PyTorch stack and multi-GB
  model weights, against Upscayl's small download, and SeedVR2 needs 16 GB of VRAM
  where Upscayl runs on a laptop iGPU. (A remote-only install is small, but then
  every run needs a rented GPU.)
- **Much slower per image.** A diffusion pass is not comparable to a single
  Real-ESRGAN forward pass. Image Toolbox's answer is "rent a big card", which
  costs money. **This is a deliberate choice, not an oversight:** the image
  upscaler is diffusion-only (SeedVR2) because photo restoration is what the tool
  is for, and quality is the point. Real-ESRGAN *is* implemented here (0.5.6) and
  is deliberately offered for **video only**, where a per-frame diffusion pass is
  often unaffordable in time or money. Whether to expose it for stills as a "fast
  mode" remains open, but the slowness is the price of the chosen output quality,
  knowingly paid.
- **No custom models.** Upscayl's model ecosystem, and the ability to drop in a
  community model tuned for anime, text or faces, has no equivalent here.
- **No fixed-ratio image upscaling.** If you want exactly 4x on an image, Image
  Toolbox has no way to ask for that (video does; images do not). **Also
  deliberate, and it follows from the product's purpose:** the image upscaler
  targets **fixed output resolutions** (4K / 2K / 1080p) so a revived photo maps
  1:1 onto the screens people actually view photos on, a TV or a monitor, rather
  than landing on whatever odd size a ratio multiplied it into. A ratio and a
  target answer different questions ("make it N times bigger" vs "make it fit this
  display"), and this tool answers the second on purpose. It is a real limitation
  for anyone who wants the first; it is not an incidental gap.
- **Smaller, riskier project.** One non-professional author, several features
  flagged experimental, no localisation (English only, and
  [deliberately so](dropped-ideas.md#ui-localization--multi-language-interface-2026-07-27):
  the interface is roughly ten times the text Upscayl translates, on a project with one
  maintainer, and tooltips that carry money and data-loss warnings are the worst possible
  thing to machine-translate unreviewed).
- **No drag-and-drop, no theming.** tkinter buys dependency-lightness at the cost
  of polish.

---

## 6. Can they be used together?

Yes, and it is a reasonable workflow. They are not really competitors:

- Use **Upscayl** for one-off images, for anything that needs a specialist model
  (anime, line art, text), and on any machine that is not a Windows NVIDIA box.
- Use **Image Toolbox** for the bulk archive job: walk the whole collection,
  upscale what is worth upscaling, describe and rename it, do the videos, then
  put everything back in place.

Image Toolbox's Conciliation step does not care which tool produced the processed
files for images matched by name, so an Upscayl-produced output tree can in
principle be conciliated back into the original tree by mirrored-name matching
(there is no content-hash lineage for files this app did not create, so the
name fallback is what applies, and videos, being lineage-only, will not match).
Preview first, as always.

---

## 7. Sources

- Upscayl repository and README: <https://github.com/upscayl/upscayl>
- Upscayl website and feature page: <https://www.upscayl.io/>
- Upscayl UI strings (the most reliable list of its actual settings):
  `renderer/locales/en.json` in the repository above
- Image Toolbox: this repository's `README.md`, `CLAUDE.md`, and the docs under
  `docs/`
