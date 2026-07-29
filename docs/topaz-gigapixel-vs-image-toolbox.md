# Topaz Gigapixel vs Image Toolbox

A feature-by-feature comparison between
[Topaz Gigapixel](https://www.topazlabs.com/topaz-gigapixel) and **Image Toolbox**
(this project), on the image side only. The video comparison against Topaz Video
lives in [`topaz-video-vs-image-toolbox.md`](topaz-video-vs-image-toolbox.md).

Written 2026-07-28 against Topaz's public product page and `docs.topazlabs.com`
(app version **1.3.1**, released 2026-06-11) and Image Toolbox 0.5.9-experimental.
Check Topaz's own pages before relying on their column: their release cadence is
fast, their pricing changed materially in the last year, and this is a snapshot of
one day's reading.

**This is not a competitive pitch.** Gigapixel is a mature commercial product from
a company with a real engineering team, two billion processed images behind it, and
paying customers who expect it to work. Image Toolbox is one person's hobby project
given away for free. Pretending those are peers would be silly, and would make the
document useless for its actual purpose: helping someone decide which one to point
at their photo collection. Where Image Toolbox loses, it is written down plainly
(section 5). Where a limitation is a deliberate design choice, that is stated as
explanation, never as a defence.

One framing note before the tables. **These two tools are less alike than they
look.** Gigapixel is an *image quality* application: its job is to make one image
as good as it can possibly be, and everything in it (nine model families, four
correction sliders, prompt-driven regeneration, face recovery) serves that. Image
Toolbox is a *collection logistics* application that happens to contain an
upscaler: its job is to get forty thousand files through a pipeline unattended and
put them back where they came from. A comparison that scores them on the same axis
will mislead you.

---

## 1. The short version

**Topaz Gigapixel gives you far more control over the result.** Nine model
families against one, including subject-specific models for text, art and CG, a
generative tier that can invent plausible detail where none survives, per-image
correction for noise / blur / compression, dedicated face recovery, and automatic
per-image model selection. It also runs on almost any modern machine including AMD
and Intel GPUs, Apple Silicon and Windows-on-ARM laptops, and its NeuroStream
technology drops the VRAM its heaviest models need from tens of gigabytes to
roughly three. If your question is "how much can I do to this one photo", the
answer is Gigapixel and it is not close.

**Which one produces the nicer image is a separate question, and this document
does not answer it.** No side-by-side test was run for this comparison, and photo
upscaling is not a domain with a clean scoreboard: a model that invents crisp skin
texture will beat one that stays soft for some viewers and lose badly for others,
and the "right" answer changes per photo and per purpose. What can be said without
measuring is structural: Gigapixel can adapt its treatment to each image and Image
Toolbox applies one model to the whole run, so on **mixed** material Gigapixel has
more ways to be right. On a run of ordinary degraded family photos, which is what
SeedVR2 is aimed at, judge them yourself on your own pictures. Gigapixel has a
trial and Image Toolbox is free.

**Image Toolbox is a collection pipeline that costs nothing.** Its upscaler is one
tool of four, and it is built around problems Gigapixel does not address at all:
walking a 40,000-file tree and mirroring it, resuming a batch that stopped three
days ago, describing and renaming every photo with a local vision model, putting
the processed files back in place of the originals safely, renting a datacenter GPU
by the second when the local one is not enough, and reporting the whole thing to
Home Assistant.

Rules of thumb:

- Working photo by photo, want to **tune each result** (pick a model per subject,
  fix a face, dial back noise or JPEG blocking), and willing to pay a
  subscription: **Gigapixel**.
- Have a **large collection** and want it walked, upscaled, described, renamed and
  reconciled unattended, for free: **Image Toolbox**.
- Not on Windows: **Gigapixel** (Image Toolbox will not run at all).
- On Windows but without an NVIDIA card: **Gigapixel** for local processing, since
  it runs on AMD and Intel GPUs down to integrated graphics. Image Toolbox does
  still run: a **Remote-only** install skips the local GPU stack entirely and does
  every run on a rented pod. That is a real option, and the collection features
  (tagging, conciliation, resume, notifications) all work locally regardless of
  the card. It just means every image costs money and needs a network round trip.

---

## 2. Positioning at a glance

| | Topaz Gigapixel | Image Toolbox |
|---|---|---|
| **What it is** | Professional AI image upscaler / restorer | Photo + video collection restoration toolbox |
| **Scope** | One job, done extremely well | Upscale + tag/rename + video + conciliate |
| **Platforms** | Windows 10/11 (x86 and Snapdragon ARM), macOS 13+ (Apple Silicon and Intel) | Windows 10/11 only |
| **GPU vendors** | NVIDIA, AMD, Intel (including Intel UHD 600 / Iris Xe integrated), Apple Silicon | NVIDIA only (CUDA) for local processing. A **Remote-only** install needs no local GPU at all and runs everything on a rented pod |
| **VRAM floor** | 6 GB dedicated (8 GB for generative models); NeuroStream runs 30-56 GB models in ~3 GB | 16 GB recommended locally for SeedVR2 |
| **Backend** | Proprietary; NeuroServer local model server | PyTorch + CUDA, in-process |
| **UI** | Native commercial desktop app, plus Photoshop and Lightroom Classic plugins | Python standard-library tkinter |
| **Disk footprint** | ~55 GB on Windows (48 GB of that is model data in ProgramData) | Modest for remote-only; large for a local install |
| **Price** | **Subscription only.** Gigapixel Personal 149 USD/year (or 19-29 USD/month); Pro 499 USD/year; Topaz Studio bundle 399 USD/year | Free. Optional rented RunPod GPU, paid to RunPod, not to the author |
| **Perpetual licence** | **Discontinued 2025-10-03.** Existing perpetual owners keep their version but get no further updates | N/A (no licence to buy) |
| **Cloud** | Optional cloud rendering included in the subscription (2 concurrent images Personal, 4 Pro) | Optional rented GPU, billed by the second by RunPod |
| **Maturity / audience** | Mature commercial product, "over 2 billion images processed", named enterprise customers | Small personal project, "vibecoded", several features flagged experimental |
| **Support** | Commercial support, active community forum, documented refund policy | Best-effort, via GitHub issues. The app's "Report an issue" link pre-fills version, OS, Python, GPU and the newest crash log. No SLA |
| **Telemetry** | Commercial product, account-bound licensing | Collects nothing, ever, and needs no account |

**On the price row, fairly:** subscription-only is a real objection for a lot of
people and it is worth being precise rather than snide about it. Topaz ended
perpetual licences on 2025-10-03; the products were 99 / 199 / 299 USD one-time
until then. For someone restoring a family archive over one long weekend, a single
month of Gigapixel (29 USD) is cheap and probably the right call. For someone who
wants the tool available for the next ten years to occasionally revive a photo,
recurring cost is the whole argument. Neither position is unreasonable, and Image
Toolbox's answer to it (be free, be local, never phone home) is a values choice
rather than a technical achievement.

---

## 3. Feature matrix

Legend: ✅ has it · ⚠️ partial / with caveats · ❌ does not have it.

### 3.1 Upscaling core and models

| Feature | Gigapixel | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Local AI image upscaling | ✅ | ✅ | The shared core. |
| 100% offline processing | ✅ | ✅ | Both. Gigapixel's cloud rendering is opt-in per job; Image Toolbox only leaves the machine if you pick a remote pod. |
| Number of upscaling models | ✅ | ⚠️ | Gigapixel: Standard, Standard Max, High Fidelity 3, Low Resolution, Text & Shapes, Art & CG, Recover 2/3, Redefine, Wonder 1/2/3. Image Toolbox: SeedVR2 in three size tiers (3B Q8 / 7B FP8-mixed / 7B FP16), which are speed/VRAM tiers of **one** model, not different styles. |
| Subject-specialised models (text, art/CG, faces, low-res) | ✅ | ❌ | A genuine capability gap: line art, screenshots and CG have different failure modes than photos, and Gigapixel has a model per case. |
| Generative / diffusion upscaling | ✅ | ✅ | Gigapixel: Wonder 1/2/3, Redefine, Standard Max, Recover. Image Toolbox: SeedVR2 (diffusion) for every image. |
| Prompt-guided regeneration | ✅ | ❌ | Redefine takes a text description plus creativity (Low/Medium/High/Max) and a texture slider. |
| Face recovery | ✅ | ❌ | Face Recovery 3 with a strength slider, on faces of any size. Image Toolbox has nothing equivalent. |
| Noise suppression control | ✅ | ❌ | 1-100 slider. |
| Deblur control | ✅ | ❌ | 1-100 slider. |
| Compression-artifact repair | ✅ | ❌ | "Fix Compression", 1-100. Relevant for exactly the old JPEGs both tools target. |
| Automatic per-image model choice | ✅ | ❌ | Auto Mode / Autopilot analyses each image and picks the model and settings. Image Toolbox uses one configured model for the whole run. |
| Fixed ratio output (up to 6x) | ✅ | ❌ | Also custom multipliers to two decimals, and downscaling from 0.9x to 0.2x. |
| Custom width / height / longest edge | ✅ | ❌ | **By design, not an omission.** Image Toolbox does not offer a free-form dimension because it targets fixed **vertical-edge** resolutions instead: 1080p, 2K and 4K are named after the vertical edge because that is how the displays people view photos on are named. A photo is fitted into that box (3840 wide **or** 2160 tall, first edge to reach it wins) in its final, straightened orientation. Asking for an arbitrary width would produce a file that matches no screen, which is the opposite of the goal. |
| Resolution target (4K / 2K / 1080p) | ❌ | ✅ | Image Toolbox caps output so a revived photo maps onto the screens people view photos on. Applied after auto-straighten, so a sideways photo is fitted in the orientation it will actually be viewed in. |
| Maximum output size | ✅ | ⚠️ | Gigapixel: 32,000 px on the longest side (~1 gigapixel) or 2 GB, whichever comes first. Image Toolbox: bounded by the chosen target, so 4K is the ceiling. |
| Skip images already near the target | ❌ | ✅ | Skip-cutoff, default 66% of target. Meaningless for Gigapixel's ratio model; essential when walking a mixed collection. |
| VRAM-frugal execution of large models | ✅ | ⚠️ | NeuroStream cuts VRAM by up to 95% (models that wanted 30-56 GB run in ~3 GB) at a stated 2-8% speed cost. Image Toolbox's equivalent levers are cruder: model tier, block swapping, tiled VAE encode/decode. |
| Tile size control | ⚠️ | ✅ | Image Toolbox exposes separate tiled VAE encode and decode toggles with their own tile sizes (default 1024, both off). |
| Cannot restore detail that was never captured | ✅ (generative models invent it, and say so) | ✅ (same limit) | Both are super-resolution. Gigapixel's generative tier is explicit that it *adds* detail; whether that is a feature or a falsification depends on your purpose (see section 5). |

### 3.2 Batch processing and long runs

| Feature | Gigapixel | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Batch a folder of images | ✅ | ✅ | Gigapixel: drag files or folders in, batch size "limited only by your system's available resources". |
| Recursive subfolder walk, output tree mirrored | ❌ | ✅ | Not documented in Gigapixel; Image Toolbox mirrors the entire source tree via `os.path.relpath`. This is the single biggest workflow difference for an archive. |
| Resume a stopped batch | ❌ | ✅ | Not documented in Gigapixel. Image Toolbox keeps a SQLite cache in `db/cache.db`; a stopped run continues where it left off, across reboots. |
| Second pass for files that appeared mid-run | ❌ | ✅ | |
| Corrupt / missing file detection, logged and skipped | ❌ | ✅ | Listed at the end for review instead of aborting the run. |
| Pause / Resume that frees the GPU | ❌ | ✅ | Pause unloads DiT + VAE + the straighten CNN (~16.6 GB returned on a 3090) so the card is usable for something else, then reloads on Resume with the queue intact. |
| Stop that finishes the current file cleanly | ⚠️ | ✅ | Image Toolbox guarantees no half-written file (temp + atomic rename). |
| Progress bar + ETA | ✅ | ✅ | |
| Taskbar progress + attention flash | ❌ | ✅ | Windows `ITaskbarList3`. |
| Degraded-GPU watchdog | ❌ | ✅ | Detects the driver/VRAM slowdown that only a reboot cures, stops cleanly after the current image, alerts. |
| Never modifies the source file | ✅ | ✅ | Both write to a separate output. Image Toolbox makes it a structural promise: output goes to a mirrored tree and there is no overwrite mode at all. |
| Works on mapped network drives | ⚠️ | ✅ | Explicitly supported and routinely used in Image Toolbox. |

### 3.3 Beyond upscaling

| Feature | Gigapixel | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Vision-model image tagging (description into EXIF) | ❌ | ✅ | Local or remote Ollama, default `qwen3-vl:8b-instruct`. |
| Automatic descriptive renaming | ❌ | ✅ | `OriginalName_Condensed_Description.ext`. |
| Selectable description language | ❌ | ✅ | |
| One-click Undo of tags / renames | ❌ | ✅ | Every change recorded before anything is modified. |
| Auto-straighten sideways photos (CNN) | ❌ | ✅ | Runs before upscaling **and** before tagging; confident calls only, ambiguous ones left alone and logged. |
| Replace originals with processed results | ❌ | ✅ | Conciliation: archive or delete, content-hash lineage matching, non-destructive preview first. |
| Video upscaling | ❌ | ✅ | Topaz sells that separately as Topaz Video; see the companion document. |
| Metadata preserved in the output | ⚠️ | ✅ | Gigapixel writes the output with an export format choice (Preserve input format / JPEG / PNG / TIFF) and carries metadata across, though users have long reported field loss (lens model, white balance, flash bias on JPEG; near-total loss on TIFF). Image Toolbox copies the whole block since 0.5.9 (roadmap #13), with Orientation normalised and the stale thumbnail dropped, and Conciliation backfills anything upscaled before that. |
| RAW / DNG input | ✅ | ❌ | Gigapixel accepts RAW and DNG (exported as TIFF since 7.0.4). Image Toolbox is JPEG/PNG/WebP/BMP/TIFF only. |

### 3.4 Comparison and preview UI

| Feature | Gigapixel | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Before/after comparison view | ✅ | ✅ | Image Toolbox's is a floating, resizable window with a vertical wipe and shared zoom/pan. |
| Side-by-side and split view modes | ✅ | ⚠️ | Gigapixel offers several comparison layouts; Image Toolbox has one wipe. |
| Adjustable zoom while comparing | ✅ | ✅ | Image Toolbox: wheel zoom centred on the pointer, fit up to 400% of the upscaled native pixels, drag-pan, both sides locked to the same region. |
| Live preview before committing the whole image | ✅ | ❌ | Gigapixel auditions the selected model before you commit. Image Toolbox only shows the result after the file is written, though the film strip and the before/after comparison work **mid-run**, so a small test batch answers the same question one step later. |
| Drag and drop input | ✅ | ❌ | Image Toolbox is folder-driven. |
| Thumbnail wall of the running batch | ❌ | ✅ | Outcome-coloured frames: green comparable, red failed, blue in progress. |
| Right-click context menu per result | ❌ | ✅ | Open original/upscaled, open folder, compare, copy path. |
| Crop / composition tools | ✅ | ❌ | Including the marketed "6x lossless zoom" crop workflow. |

### 3.5 Integration and automation

| Feature | Gigapixel | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Photoshop plugin | ✅ | ❌ | Via the Automate menu, max 30,000 px. |
| Lightroom Classic plugin | ✅ | ❌ | As an External Editor (cloud rendering unavailable in that path). |
| CLI / headless | ✅ | ✅ | Gigapixel has shipped a CLI since v7 (`-i`, `-o`, `-m`, plus `--cr`, `--tx`, `--dn`, `--sh`), though Topaz has pulled most of its official CLI documentation and the community fills the gap. Image Toolbox: every runner is a standalone script by design, and the GUI drives them over the same seam. |
| Rent a cloud GPU by the second | ❌ | ✅ | RunPod, live catalog with stock and price, ~0.19 USD per 100 images on an A40. |
| Cloud rendering included in the price | ✅ | ❌ | Gigapixel's cloud rendering is "unlimited" on a subscription, with 2 (Personal) or 4 (Pro) concurrent images. This is genuinely better value than renting a pod for anyone already subscribed. |
| Cost estimate before spending | N/A | ✅ | Gigapixel's cloud is included, so there is nothing to estimate. Image Toolbox shows the estimate before any pod is created. |
| Spending guard | N/A | ✅ | `funds_guard.py`: balance floor and session cap. |
| Per-card benchmark tool | ❌ | ✅ | Measures the real ceiling and throughput per target; calibrates estimates. |
| Crowdsourced benchmark corpus | ❌ | ✅ | Pulled from GitHub at launch, contributed back via a pre-filled issue. |
| Discord / Telegram / ntfy notifications | ❌ | ✅ | Any combination, each independent and fail-safe. |
| MQTT / Home Assistant integration | ❌ | ✅ | Live task state, progress, ETA, last-run summary, plus ready-made HA dashboards. |
| Live system telemetry (CPU/RAM/VRAM/temp/power/clock) | ❌ | ✅ | Sampled continuously, published to MQTT, and graphable per run. |
| In-app update checker | ✅ | ✅ | |
| Code-signed installer | ✅ | ❌ | Gigapixel is a signed commercial product. Image Toolbox trips the SmartScreen "unknown publisher" prompt and tells you so in the README. A real difference, not a nitpick. |
| First-start hardware-aware setup wizard | ⚠️ | ✅ | Image Toolbox detects the GPU and recommends model tiers that fit its VRAM. |
| Localised UI | ✅ | ❌ | Image Toolbox's interface is English only, [deliberately](dropped-ideas.md#ui-localization--multi-language-interface-2026-07-27). The *descriptions it writes* are multilingual. |
| Light / dark theme | ✅ | ❌ | Investigated and dropped: the native `ttk` themes cannot be recoloured. See [`dropped-ideas.md`](dropped-ideas.md#lightdark-theme-2026-07-28). |
| Tooltips on every control | ⚠️ | ✅ | ~160 plain-language tooltips; money- and data-affecting ones lead with the consequence. |
| Account required | ✅ | ❌ | Gigapixel is licence-bound. Image Toolbox has no account, no login, no activation. |

### 3.6 Formats

| | Gigapixel | Image Toolbox |
|---|---|---|
| Input | JPG, PNG, TIFF, plus RAW and DNG | JPEG, PNG, WebP, BMP, TIFF |
| Output | Preserve input format, JPEG, PNG, TIFF (RAW/DNG in becomes TIFF out; DNG export removed in 7.0.4) | Same extension as the source, written atomically |
| Max dimension | 32,000 px longest side / ~1 gigapixel / 2 GB | Bounded by the selected target (4K max) |

---

## 4. What each does that the other simply cannot

**Only Gigapixel:**

1. Runs on macOS, on AMD and Intel GPUs, on Apple Silicon and on Windows-on-ARM.
2. Runs its heaviest models on a 6-8 GB card, thanks to NeuroStream.
3. Subject-specific models: text and shapes, art and CG, low-resolution, faces.
4. Prompt-guided regeneration (Redefine) and creativity/texture control.
5. Per-image corrective sliders for noise, blur and compression artifacts.
6. Dedicated face recovery at any face size.
7. Fixed-ratio and exact-dimension output up to 32,000 px, plus downscaling.
8. Autopilot: a per-image model and setting choice made automatically.
9. Photoshop and Lightroom Classic plugin integration.
10. Cloud rendering included in the subscription rather than rented separately.

**Only Image Toolbox:**

1. Walking a recursive tree of tens of thousands of files and mirroring the output structure.
2. Resuming a stopped batch, with corrupt-file triage and a rescan pass.
3. Vision-model tagging and descriptive renaming, with one-click Undo.
4. Conciliation: putting the processed files back in place of the originals, safely, with a non-destructive preview.
5. Auto-straightening sideways photos before processing.
6. Renting a specific datacenter GPU by the second, with cost estimates, spending guards and benchmark-calibrated predictions.
7. Home Assistant / MQTT, Discord / Telegram / ntfy, per-run telemetry graphs.
8. A degraded-GPU watchdog and a pause that hands the whole card back.
9. Video upscaling (Topaz sells that as a separate product).
10. Costing nothing and requiring no account.

---

## 5. Honest weaknesses of Image Toolbox against Gigapixel

Written down deliberately, because a comparison that only flatters the home team is
useless. This is the longest section in the document, and that is the correct
outcome.

- **Much less control over the result.** One model family against nine, no
  subject-specialised models, no face recovery, no corrective sliders for noise
  / blur / compression, no per-image automatic model choice. On a batch of mixed
  material (a screenshot, a scanned print, a CG render, a crowd photo) Gigapixel
  adapts per image and Image Toolbox applies the same treatment to everything.
  Note what this does and does not claim: it is a statement about *options*, not
  a verdict on output quality. Nobody has run the two side by side for this
  document, and on a single ordinary photo SeedVR2 may well be the one you
  prefer. But when the material varies, having a model per case is a real
  advantage, and when a specific defect needs fixing (a face, JPEG blocking,
  motion blur) Image Toolbox has no lever at all.
- **Windows-only, and NVIDIA-only to run locally.** Gigapixel covers Windows x86,
  Windows ARM, macOS on Apple Silicon and Intel, and NVIDIA, AMD and Intel GPUs
  down to integrated graphics. Image Toolbox runs on a fraction of that hardware.
  The Windows requirement is absolute (tkinter GUI, PowerShell bootstrap, Windows
  paths); the NVIDIA requirement is not, because a **Remote-only** install does
  the GPU work on a rented pod. But "you can still use it, you just pay per
  image" is a worse answer than "it runs on the card you already have", so this
  stays in the weaknesses column.
- **Far higher VRAM floor.** Gigapixel's minimum is 6 GB and NeuroStream lets
  models that once needed 30-56 GB run in about 3 GB, for a 2-8% speed cost.
  SeedVR2 wants 16 GB to be comfortable, and Image Toolbox's answer to a small
  card is "rent a big one", which costs money. NeuroStream is a serious piece of
  engineering with no counterpart here.
- ~~**No metadata in the output, at all.**~~ **Closed** by roadmap #13 (0.5.9): the
  upscaled image now carries the original's capture date, camera, lens, GPS and
  copyright, and Conciliation puts those fields back into anything upscaled before
  the fix, at the last moment both files exist. This was the worst gap in the
  document while it stood.
- **No RAW support.** Anyone working from camera originals rather than JPEGs is
  simply not served.
- **No live preview.** Gigapixel lets you audition a model on an image before
  committing to it; Image Toolbox only shows you the result after the file is
  written. The cost of that is highest on a long run, where a setting you would
  have rejected in a preview can burn hours of GPU time or a real rented-pod
  bill. What softens it is not that the mistake is cheap, but that it is **cheap
  to catch and safe to undo**: the film strip frames each finished image as it
  lands and double-clicking one opens the before/after comparison **mid-run**, so
  the first few images tell you what the settings are doing; Stop finishes the
  current image cleanly; and the source is never touched, so nothing is lost by
  re-running. In practice you point the tool at a handful of representative
  photos first, look, adjust, then let the real queue go, which is a slower loop
  than a preview but reaches the same place. The one sharp edge worth knowing:
  a re-run **skips any image whose output already exists**, so redoing images at
  a new setting means deleting those outputs first.
- **No fixed-ratio or exact-dimension output.** This one is **deliberate and
  follows from the product's purpose**: the image upscaler targets fixed
  **vertical-edge** resolutions (1080p / 2K / 4K, named after the vertical edge
  exactly as displays are) so a revived photo maps 1:1 onto the screens people
  actually view photos on, rather than landing on whatever odd size a ratio
  produced. A ratio and a target answer different questions. It is still a real
  limitation for anyone who wants the first, and it is not an incidental gap.
- **Slower per image, with fewer escape hatches.** A SeedVR2 diffusion pass is
  not comparable to Gigapixel's optimised model server, and Gigapixel's cloud
  rendering is bundled where Image Toolbox's remote GPU is billed by RunPod.
- **No plugins.** No Photoshop, no Lightroom. For a photographer with an existing
  workflow, that alone decides it.
- **Unsigned installer, and best-effort support only.** One non-professional
  author, several features flagged experimental, and a SmartScreen warning on
  install. Support is not nothing: the main window carries a **"Report an issue"**
  link that opens a pre-filled GitHub issue with the app version, OS, Python and
  GPU already filled in and the newest crash log named for attachment, and issues
  do get answered. But it is one person doing it when time allows, with no SLA,
  no ticketing, no phone number and no obligation, against a company with paid
  support staff. Treat it as goodwill rather than a guarantee.

**Where the balance genuinely tips the other way**, and it is worth naming so this
section is not read as capitulation: for a large collection, quality per image is
not the only variable. A tool that cannot walk a nested tree, cannot resume after a
three-day interruption, cannot describe and rename what it processed and cannot put
the results back where they came from will lose to one that can, even if every
individual image it produces is better. That is the trade Image Toolbox is making
on purpose.

**One point where the difference is philosophical, not technical:** Gigapixel's
generative models (Wonder, Redefine) *invent* detail. That is exactly right for a
print, a poster or a client deliverable, and Topaz is open about it. For a family
archive, where the photo is a record of something that happened, invented faces and
invented textures are a different proposition, and the fact that the invention is
convincing is what makes it a problem rather than what solves it. SeedVR2 is also
generative and is not innocent of this, but it is applied uniformly and without a
creativity dial, which narrows the blast radius. This is not an argument that
Gigapixel is wrong: it is a reason to know which mode you are in.

---

## 6. Can they be used together?

Yes, and for a serious archive project it is arguably the best answer.

- Use **Image Toolbox** to do the logistics: walk the collection, work out what is
  worth processing, do the bulk pass, describe and rename everything, handle the
  videos, and conciliate the results back into place.
- Use **Gigapixel** on the photos that deserve individual attention: the portraits,
  the ones with faces, the badly compressed ones, the ones going to print.

Image Toolbox's Conciliation matches images by content-hash lineage first, then
falls back to mirrored-name matching, so a Gigapixel output tree that mirrors the
source layout can in principle be conciliated back into the original tree by name
(there is no lineage for files this app did not create). Preview first, as always.

The reverse pairing also works: run Image Toolbox's Tag & Rename over a folder of
Gigapixel outputs to get descriptions and filenames, since tagging does not care
which tool produced the pixels.

---

## 7. Sources

- Topaz Gigapixel product page: <https://www.topazlabs.com/topaz-gigapixel>
- Topaz Gigapixel documentation (models, upscale settings, face recovery, batch
  processing, system requirements): <https://docs.topazlabs.com/topaz-gigapixel>
- Topaz Labs NeuroStream announcement:
  <https://www.topazlabs.com/news/topaz-labs-introduces-topaz-neurostream-breakthrough-tech-for-running-large-ai-models-locally>
- Topaz Gigapixel v1.3.x release notes, Topaz Community:
  <https://community.topazlabs.com/t/topaz-gigapixel-v1-3-1/103355>
- Perpetual-licence discontinuation (2025-10-03), CG Channel:
  <https://www.cgchannel.com/2025/09/topaz-labs-to-end-perpetual-licenses-of-its-software/>
- Image Toolbox: this repository's `README.md`, `CLAUDE.md`, and the docs under
  `docs/`
- Companion documents: [`topaz-video-vs-image-toolbox.md`](topaz-video-vs-image-toolbox.md),
  [`upscayl-vs-image-toolbox.md`](upscayl-vs-image-toolbox.md)
