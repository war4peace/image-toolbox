# Topaz Video vs Image Toolbox

A feature-by-feature comparison between [Topaz Video](https://www.topazlabs.com/topaz-video)
(formerly Topaz Video AI) and the **Video Upscaler** in **Image Toolbox** (this
project). The image-side comparison against Topaz Gigapixel lives in
[`topaz-gigapixel-vs-image-toolbox.md`](topaz-gigapixel-vs-image-toolbox.md).

Written 2026-07-28 against Topaz's public product page and `docs.topazlabs.com`;
the Image Toolbox column was refreshed 2026-07-29 for **0.6.0**. Check
Topaz's own pages before relying on their column: their model lineup changes several
times a year and their pricing changed materially in the last one. This is a snapshot
of one day's reading.

**This is not a competitive pitch.** Topaz Video is the reference product in its
category, used in real post-production pipelines, with plugins for After Effects
and DaVinci Resolve and a Pro tier that scales across ten GPUs. Image Toolbox's
Video Upscaler is an **experimental** feature of a hobby project, roughly a year
old, and it does one of the nine things Topaz Video does. Where Image Toolbox
loses, it is written down plainly (section 5). Where a limitation is a deliberate
design choice, that is stated as explanation, never as a defence.

The framing note matters even more here than on the image side. **Topaz Video is a
video enhancement suite; Image Toolbox's Video Upscaler is an upscaling batch
job.** Topaz denoises, sharpens, stabilises, deinterlaces, interpolates frames,
slows footage down, removes motion blur, manages grain and converts SDR to HDR.
Image Toolbox upscales, and everything else it does is about *getting a large queue
through unattended without losing work or money*. Scoring them on one axis will
mislead you.

---

## 1. The short version

**Topaz Video does vastly more, and that part is not a matter of opinion.**
Nineteen-odd models including the Starlight diffusion family, **nine** distinct
processing operations against Image Toolbox's one, twenty-three output encoders
including ProRes and FFV1, multi-GPU rendering in the Pro tier, plugins for After
Effects and Resolve, and it runs on AMD and Intel GPUs and on Apple Silicon. If
the question is "what can I do to this footage" or "how do I get this into my
edit", the answer is Topaz Video.

That scope gap is the honest headline here, and it is bigger than any argument
about upscaling quality: old footage usually needs **denoising and stabilising**
at least as much as it needs more pixels, and Image Toolbox cannot do either.
Which of the two upscales a given clip more attractively was not tested for this
document and would vary by source anyway.

**Image Toolbox's Video Upscaler is a queue runner for a shelf of old footage.**
It upscales with SeedVR2 (diffusion) or Real-ESRGAN (a GAN), and the engineering
around it addresses a different problem: a queue of dozens of camcorder tapes that
takes days of GPU time, on hardware you may not own. It splits each video into
~1-minute segments and banks every finished one, so a stopped run resumes at the
segment rather than the video; it can rent a specific RunPod GPU by the second,
estimate the cost first, cap the spend per run, and survive **losing that rented
GPU mid-run** by waiting for the identical card to come back into stock and
redeploying it.

Rules of thumb:

- Want to **work on the footage** rather than just enlarge it, or need a
  professional codec, or work on a Mac: **Topaz Video**.
- Have a pile of old home videos, an NVIDIA card or a willingness to rent one, and
  want it ground through unattended for free with strict cost control:
  **Image Toolbox**.
- Need denoise, stabilisation, frame interpolation or slow motion at all:
  **Topaz Video**. Image Toolbox does not do any of those.

---

## 2. Positioning at a glance

| | Topaz Video | Image Toolbox (Video Upscaler) |
|---|---|---|
| **What it is** | Professional AI video enhancement suite | One tool of four in a photo/video collection pipeline |
| **Scope** | Upscale, denoise, sharpen, restore, stabilise, slow motion, frame interpolation, motion deblur, grain, SDR to HDR | Upscale only |
| **Status** | Mature commercial product | **Experimental**, roughly one year old |
| **Platforms** | Windows 10/11, macOS 13+ (Apple Silicon only; Intel Macs no longer supported) | Windows 10/11 only |
| **GPU vendors** | NVIDIA, AMD, Intel (6 GB VRAM min); Starlight local rendering is Windows + NVIDIA only | NVIDIA only (CUDA) for local runs. A **Remote-only** install needs no local GPU and does every run on a rented pod |
| **RAM** | 16 GB min, 32 GB recommended; Starlight wants 32-36 GB | 16 GB practical minimum |
| **Disk footprint** | ~45 GB | Modest for remote-only; large for a local install |
| **Multi-GPU** | ✅ Pro tier: tiles split across 2, 3, 4+ cards, near-linear scaling | ❌ One card per run (mixed queues are roadmap #12) |
| **Price** | **Subscription only.** Video Personal 299 USD/year (or 39-59 USD/month), 25 cloud credits/month; Pro 699 USD/year, 100 credits/month; Studio bundle 399 USD/year, 300 credits/month | Free. Optional rented RunPod GPU, paid to RunPod, not to the author |
| **Perpetual licence** | **Discontinued 2025-10-03** (was 299 USD). Existing owners keep their version, get no further updates | N/A |
| **Cloud** | Topaz's own cloud rendering, paid in credits | Rent any RunPod GPU by the second, billed by RunPod |
| **Plugins** | After Effects, DaVinci Resolve | None |
| **Support** | Commercial support, active forum | Best-effort, via GitHub issues. The app's "Report an issue" link pre-fills version, OS, Python, GPU and the newest crash log. No SLA |

**On the price row, fairly:** 299 USD/year is not outrageous for a tool doing work
that would otherwise be impossible, and the 25 monthly cloud credits have real
value. It is a subscription though, and Topaz ended perpetual licences on
2025-10-03, so the cost recurs for as long as you want the tool. Image Toolbox's
cost model is different rather than strictly cheaper: free locally, but a long
remote video run on a rented card is real money paid to RunPod, and the app spends
a lot of its complexity budget on making that money predictable and capped.

---

## 3. Feature matrix

Legend: ✅ has it · ⚠️ partial / with caveats · ❌ does not have it.

### 3.1 Processing operations

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| AI video upscaling | ✅ | ✅ | The only overlap. |
| 100% local processing available | ✅ | ✅ | Both. Topaz's cloud is opt-in per job; Image Toolbox only leaves the machine if you pick a remote pod. |
| Denoise | ✅ | ❌ | Nyx / Nyx XL, plus per-model noise controls. |
| Sharpen | ✅ | ❌ | Theia, plus per-model sliders. |
| Stabilisation | ✅ | ❌ | Themis. |
| Frame interpolation / framerate conversion | ✅ | ❌ | Chronos, Apollo, Aion. |
| Slow motion (up to 8x) | ✅ | ❌ | Apollo. |
| Motion deblur | ✅ | ❌ | |
| Grain management (add / reduce) | ✅ | ❌ | "Add Noise" counters over-smoothing. |
| SDR to HDR conversion | ✅ | ❌ | |
| Face recovery in video | ✅ | ❌ | Iris. |
| Deinterlacing of interlaced sources | ✅ | ✅ | Image Toolbox detects interlacing (idet when `field_order` is unknown) and forces a `bwdif` deinterlace. Not a nicety: MiniDV-era 576i sources otherwise upscale combed, and NVENC has no interlaced-HEVC path, which had produced an all-black deliverable. |
| Crop / trim / rotate | ✅ | ⚠️ | Topaz has an edit toolbar. Image Toolbox has a segment extractor: mark in/out on a live preview and queue that range as a clip. |

### 3.2 Models and quality control

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Number of models | ✅ | ⚠️ | Topaz: Starlight Precise 2.5, Starlight Sharp, Starlight HQ, Starlight Mini, Proteus, Iris, Nyx, Nyx XL, Rhea, Rhea XL, Artemis (six variants), Gaia (two), Theia, Apollo, Chronos, Aion, Themis, Hyperion, SDR-to-HDR, plus Pro-exclusive models. Image Toolbox: SeedVR2 (three size tiers) and Real-ESRGAN (two tiers). |
| Diffusion upscaling | ✅ | ✅ | Topaz: the Starlight family. Image Toolbox: SeedVR2. |
| GAN / fast upscaling | ✅ | ✅ | Topaz: the classic models. Image Toolbox: Real-ESRGAN Compact / Quality, added 0.5.6 as the fast, VRAM-light alternative. |
| Choose the engine per job | ✅ | ✅ | Image Toolbox: a Method switch per queued job; the queue groups by (engine, GPU) and runs one pod per group. |
| Per-model parameter tuning | ✅ | ❌ | Topaz exposes detail recovery, dehalo, anti-alias, add-noise, focus fix, revert-compression and more per model. Image Toolbox exposes a target, a batch size and tiling. |
| Automatic per-clip model choice | ✅ | ❌ | |
| Live preview before committing | ✅ | ❌ | Topaz previews multiple clips at once, with pan and zoom. Image Toolbox has a segment extractor preview for picking a range, not for judging a model. |
| Fixed ratio (2x / 4x) | ✅ | ✅ | Image Toolbox: via the Real-ESRGAN method (native scales only, no fake 4x-then-downscale). |
| Resolution target (1080p / 1440p / 4K) | ✅ | ✅ | Image Toolbox uses box-fit, per video, from the actual frame dimensions. |
| Maximum output resolution | ✅ | ⚠️ | Topaz: 16K containerwise, 4K/8K on Personal, up to 24K on Pro. Image Toolbox: 4K. |
| Documented inherent limits | ⚠️ | ✅ | Image Toolbox documents two limits of SeedVR2 as architectural rather than tunable: **temporal jitter** of fine detail on slow pans and slow motion (from the 4x causal temporal VAE), and **text / plate / logo distortion** (generative SR with no OCR). See `docs/video-upscaler.md`. Topaz's generative models have the same class of problem; it is less prominently documented. |

### 3.3 Long runs, queues and failure

This is the section Image Toolbox was actually built for.

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Batch queue of multiple videos | ✅ | ✅ | Both process sequentially. |
| Recursive folder walk of a source tree | ⚠️ | ✅ | Image Toolbox walks the tree and mirrors the output structure. |
| Per-clip settings within one queue | ✅ | ✅ | Image Toolbox: each job carries its own engine, target and GPU. |
| Pause / resume an export | ⚠️ | ✅ | Topaz has pause/resume, with a long trail of community reports of resumed jobs losing hours of rendered work or restarting from an earlier point. Image Toolbox has no pause on the video tab (deliberately, see `docs/dropped-ideas.md`), but Stop plus Start resumes at the first unfinished **segment**, which achieves the same thing with a much simpler failure mode. |
| Segment-level resume | ❌ | ✅ | Each video is split into ~1-minute segments; every finished segment is banked in SQLite. A run stopped after three days resumes mid-video, not at the start of it. |
| Per-run minute / dollar caps | ❌ | ✅ | A run ends cleanly after the current segment when the cap is hit; the rest stay pending. A big job is paid in affordable installments. |
| Survive losing the GPU mid-run | ❌ | ✅ | Opt-in Auto-resume: distinguish a bad source from a lost pod, reconnect a blipped pod, or wait unbounded (0 USD billed) for the identical card to return to stock and redeploy it. |
| Reconcile "already done" from the output folder | ❌ | ✅ | The scan adopts outputs present on disk but absent from this install's cache, so a second machine sharing the same source and destination does not offer to redo them. |
| Black-output guard | ❌ | ✅ | Aborts a video whose first segment comes out black while the source is not, before it is streamed anywhere. |
| Duration-drift check after mux | ❌ | ✅ | |
| Degraded-GPU watchdog | ❌ | ✅ | Detects the driver/VRAM slowdown that only a reboot cures. |
| Adaptive batch sizing with OOM back-off | ⚠️ | ✅ | Image Toolbox sizes the batch predictively from the card's VRAM, backs off on OOM, and carries the corrected size forward to the same video's later segments. |
| Never modifies the source | ✅ | ✅ | Both. |

### 3.4 Cost, hardware and remote execution

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Runs on AMD / Intel GPUs | ✅ | ⚠️ | Topaz: 6 GB VRAM floor, though Starlight local rendering is Windows + NVIDIA only. Image Toolbox cannot use an AMD or Intel card for the upscaling itself, but a Windows machine with one can still run the whole pipeline in **Remote-only** mode against a rented pod. |
| Runs on Apple Silicon | ✅ | ❌ | Intel Macs are no longer supported by Topaz either. |
| Multi-GPU rendering | ✅ | ❌ | Pro tier: 54m55s on 1 GPU, 27m26s on 2, 14m42s on 4, for the same 8K job. Image Toolbox runs one card per run; a mixed local+remote queue is roadmap **#12**. |
| Cloud rendering | ✅ | ✅ | Different models entirely. Topaz: its own service, paid in credits, 100 GB max input, outputs deleted after 7 days, Starlight capped at 9,000 frames (~5 min at 30 fps) per job, no stabilisation / SDR-to-HDR / Rhea XL / crop in the cloud. Image Toolbox: rent a raw RunPod GPU by the second and run the same pipeline on it, no frame cap, no feature restrictions, your files on a pod you control that is torn down afterwards. |
| Pick the exact GPU model | ❌ | ✅ | Live RunPod catalog with stock and price, filtered to cards that can actually run the selected job, cheapest first. No silent substitution, ever. |
| Cost estimate before spending | ⚠️ | ✅ | Topaz publishes credit costs per model/resolution. Image Toolbox estimates duration and dollars for the whole queue before any pod is created, seeded from a benchmark and refined by your own history. |
| Spending guard | ❌ | ✅ | `funds_guard.py`: refuse to start if it would drop the balance below a floor; auto-stop when a session cap is crossed. |
| Dead-man's switch on the rented machine | N/A | ✅ | Max-runtime plus idle-timeout on the pod, so a dropped connection cannot leave a billed GPU running. |
| Per-card benchmark tool | ❌ | ✅ | Sweeps each target to the card's measured VRAM ceiling (local or on a rented pod), with both torch.compile modes, and calibrates the AUTO batch, the offered targets and the estimate. Resumable. |
| Crowdsourced benchmark corpus | ❌ | ✅ | A card someone else measured is not re-swept locally, which matters when a sweep is billed. Pulled from GitHub at launch; contributed back via a pre-filled issue. |

### 3.5 Output, formats and integration

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Input formats | ✅ | ⚠️ | Topaz: 23 video extensions plus image sequences (PNG/TIF/JPG/DPX/EXR, min 5 frames). Image Toolbox: mp4, avi, mov, mkv, m4v, wmv, mpg, mpeg and similar. |
| Output encoders | ✅ | ⚠️ | Topaz: 23, including ProRes (5 variants), H.264, H.265 Main/Main10, VP9, AV1, FFV1 (4:2:0/4:2:2/4:4:4), QuickTime V210/R210/Animation, and image sequences. Image Toolbox: H.265 via `hevc_nvenc` or `libx265` with an H.264 fallback, `yuv420p`, with a 10-bit option. The encoder is auto-picked by capability, not chosen from a list. |
| Archival / intermediate codecs (ProRes, FFV1) | ✅ | ❌ | A hard blocker for anyone feeding an edit or an archive. |
| Bit depths | ✅ | ⚠️ | Topaz: 8, 10, 12, 16, 32. Image Toolbox: 8-bit, or 10-bit when enabled. |
| Containers | ✅ | ⚠️ | Topaz: MOV, MP4, MKV, WEBM, AVI. Image Toolbox: MP4/MOV oriented. |
| Original audio preserved and muxed back | ✅ | ✅ | Image Toolbox also runs a duration-drift check afterwards. |
| Hardware encoding | ✅ | ✅ | Both use NVENC where available. |
| After Effects / DaVinci Resolve plugins | ✅ | ❌ | |
| CLI / headless | ⚠️ | ✅ | Topaz ships an ffmpeg build with `tvai_*` filters and has a CLI, but pulled most of its official CLI documentation, so community write-ups fill the gap. Image Toolbox: every runner is a standalone script by design, and the GUI drives them over the same seam. |
| Extract and upscale one scene | ⚠️ | ✅ | Image Toolbox: mark in/out on a live preview and it is queued as a clip through the identical estimate / GPU-pick / stream / resume path as a whole video. The source is never touched. |
| Side-by-side real-time playback with sound | ✅ | ✅ | Image Toolbox: bundled libVLC, original and upscaled side by side, audio routed to the upscaled player as the sync reference. Falls back to a silent frame-scrub if libVLC is absent. |
| Frame-accurate before/after wipe | ✅ | ✅ | Topaz: Single, Side by Side and Split preview modes. Image Toolbox: a vertical wipe with shared zoom and pan, on a timestamp-aligned frame pair. |
| Hover magnifier showing both versions at once | ⚠️ | ✅ | Topaz's Side by Side and Split modes put the two versions in one view, which answers the same question a whole frame at a time; no pointer-following loupe is documented. Image Toolbox's **Lens** (0.6.0, roadmap #14) magnifies the spot under the pointer as original **and** upscaled side by side on whichever frame you have stopped at, at the actual upscale ratio, so the upscaled panel is exactly 1:1 with the file that was produced. The wheel zooms the lens (1×/2×/4×/8×) and a click pins it. It matters more on video than on stills: an old 320x240 source is already being blown up by the window itself, so the lens starts at least as strong as the view it sits on rather than at a useless 1:1. |
| Replace originals with the upscaled results | ❌ | ✅ | Conciliation, matching videos by content-hash lineage only (deliberately no name fallback, so a short clip can never replace a whole source), with a non-destructive preview. |

### 3.6 Operations and integration

| Feature | Topaz Video | Image Toolbox | Notes |
|---|:--:|:--:|---|
| Discord / Telegram / ntfy notifications | ❌ | ✅ | Any combination, each independent and fail-safe. Relevant when a run lasts days. |
| MQTT / Home Assistant integration | ❌ | ✅ | Live task state, frame progress, ETA, last-run summary, plus ready-made HA dashboards. |
| Live system telemetry (CPU/RAM/VRAM/temp/power/clock) | ⚠️ | ✅ | Sampled continuously for both the local machine and the rented pod. |
| Per-run usage graphs | ❌ | ✅ | Four capacity-pinned matplotlib charts with a crosshair readout. |
| Taskbar progress + attention flash | ❌ | ✅ | |
| Crash logging to file | ⚠️ | ✅ | `logs/crash_*.log` plus a native dialog. |
| In-app update checker | ✅ | ✅ | |
| Code-signed installer | ✅ | ❌ | Image Toolbox trips the SmartScreen "unknown publisher" prompt. |
| Localised UI | ✅ | ❌ | English only, [deliberately](dropped-ideas.md#ui-localization--multi-language-interface-2026-07-27). |
| Light / dark theme | ✅ | ❌ | Investigated and dropped, see [`dropped-ideas.md`](dropped-ideas.md#lightdark-theme-2026-07-28). |
| Tooltips on every control | ⚠️ | ✅ | ~160 plain-language tooltips; the Video Upscaler's money-affecting ones lead with the consequence (Stop abandons the segment in progress; a RunPod volume bills monthly even when idle). |
| Account required | ✅ | ❌ | |

---

## 4. What each does that the other simply cannot

**Only Topaz Video:**

1. Everything that is not upscaling: denoise, sharpen, stabilise, interpolate, slow motion, motion deblur, grain, SDR to HDR, face recovery.
2. Runs on AMD and Intel GPUs and on Apple Silicon.
3. Multi-GPU rendering with near-linear scaling (Pro).
4. Professional and archival codecs: ProRes, FFV1, V210/R210, image sequences, up to 12/16/32-bit.
5. Output above 4K (8K on Personal, 24K on Pro).
6. After Effects and DaVinci Resolve plugins.
7. Per-model parameter tuning and live multi-clip preview.
8. Automatic per-clip model selection.

**Only Image Toolbox:**

1. Renting a **specific** datacenter GPU by the second, with live stock and price, a cost estimate first, a spending cap, and a dead-man's switch on the machine.
2. Segment-level resume, so a multi-day queue is banked minute by minute and a stop costs at most one segment.
3. Surviving the loss of a rented GPU mid-run and continuing on the identical card when it comes back into stock.
4. Per-card benchmarking with a crowdsourced corpus, so a card someone else measured is not re-measured on your bill.
5. Conciliation: replacing the original videos with the upscaled ones, by content-hash lineage, with a preview first.
6. Home Assistant / MQTT, Discord / Telegram / ntfy, per-run telemetry graphs.
7. Sitting in the same app as an image upscaler, a vision-model tagger and a renamer, over one collection.
8. Costing nothing, needing no account, and never phoning home.

---

## 5. Honest weaknesses of Image Toolbox against Topaz Video

Written down deliberately, because a comparison that only flatters the home team is
useless. This is the largest section and it should be.

- **It does one operation out of nine.** No denoise, no stabilisation, no frame
  interpolation, no slow motion, no motion deblur, no grain control, no SDR to
  HDR, no face recovery. Old camcorder footage usually needs *denoising and
  stabilising* at least as much as it needs upscaling, and Image Toolbox cannot
  do either. This is the honest headline of the whole document.
- **No professional or archival codecs.** H.265 (8-bit, or 10-bit when enabled)
  and that is it. No ProRes, no FFV1, no image sequences, no 12-bit. If the
  output has to enter an edit or an archive, Image Toolbox's deliverable is the
  wrong file, and the encoder is not even chosen from a list: it is auto-picked
  by capability.
- **4K ceiling.** Topaz goes to 8K on Personal and 24K on Pro.
- **One GPU per run.** Topaz Pro splits tiles across as many cards as you own and
  scales nearly linearly. Image Toolbox's mixed local+remote queue is roadmap
  **#12** and multi-local-GPU is its stepping stone; neither exists yet.
- **Windows-only, and NVIDIA-only to run locally.** Topaz covers Windows and
  Apple Silicon, and NVIDIA, AMD and Intel GPUs, at a 6 GB VRAM floor. SeedVR2
  video wants far more than 6 GB, and Image Toolbox's answer to a small or
  non-NVIDIA card is "rent a big one" (a **Remote-only** install skips the local
  GPU stack entirely and works fine). That is a genuine escape hatch rather than
  a dead end, but it converts a hardware limitation into a per-minute bill, which
  is not the same as running on the card you already own.
- **No per-model tuning and no live preview.** You pick a target and a method,
  and you find out what it looks like when the video is done. Topaz lets you
  audition a model on a clip before committing hours to it, which on a long run
  is worth more than it sounds. The partial answer here is the **segment
  extractor**: mark thirty seconds on the preview, upscale just that through the
  identical pipeline, and watch it side by side with sound before committing the
  whole tape. That is a genuine audition, it just costs a real (small) run
  instead of being instant.
- **Fewer models, and no model aimed at a specific defect.** Two engines against
  roughly twenty, several of Topaz's being purpose-built for one problem (faces,
  noise, aliasing, halos, CG). This is a count of options, not a claim that any
  individual Topaz model out-upscales SeedVR2 on a given clip; that was not
  tested here. The point is that when a source has a specific flaw, Topaz has
  something pointed at it and Image Toolbox does not.
- **Experimental, and honest about it.** The Video Upscaler is about a year old,
  written by one non-professional developer, with an unsigned installer. There
  **is** a support channel, and it is deliberate rather than incidental: the main
  window's "Report an issue" link opens a pre-filled GitHub issue carrying the
  app version, OS, Python and GPU, with the newest crash log named for
  attachment, and the Benchmark GPU window carries the same link. It is answered
  on a best-effort basis by one person, which is not a substitute for commercial
  support on a tool you are paying for and depending on.
- **Two documented quality limits that are not going away.** SeedVR2 jitters fine
  detail on slow pans and slow motion (the 4x causal temporal VAE, intra-batch,
  not fixable by batch size, overlap or model choice) and distorts text, plates
  and logos (generative SR with no OCR). Both are architectural. To be fair to
  the comparison, Topaz's generative models are subject to the same class of
  artifact; the difference is that Topaz has more non-generative models to fall
  back on when it bites.

**Where the balance genuinely tips the other way**, and it is worth naming so this
section is not read as capitulation: the problems Image Toolbox solves are the ones
that appear at *scale and duration*, and they are exactly where a per-seat desktop
application tends to be weakest. A forty-video queue at 4K is days of GPU time. On
that timescale, "what happens when it stops" stops being a footnote: Topaz's own
community has a long thread history of pause/resume losing hours of rendered work,
and there is no equivalent of banking every finished minute, capping the spend,
estimating the bill first, or continuing after the machine you rented disappears.
Those are not quality features and they will never make one frame look better. They
are what makes an unattended multi-day job survivable, and that is the trade being
made on purpose.

---

## 6. Can they be used together?

Yes, and for old home video it is probably the right answer.

- Use **Image Toolbox** for the bulk pass on a large, unglamorous queue: walk the
  folder, deinterlace what needs it, upscale everything to a sane target, do it on
  a rented card overnight with a spending cap, and conciliate the results back into
  the original tree.
- Use **Topaz Video** on the clips that deserve individual attention, and for
  everything Image Toolbox cannot do at all: denoise, stabilise, interpolate,
  slow motion, and any deliverable that needs ProRes or a professional bit depth.

A reasonable division on a single precious tape is to let Topaz do the restoration
work (denoise, stabilise, deinterlace) and let it upscale too, since it is already
there. A reasonable division on forty tapes is to let Image Toolbox grind the queue
and only pull the good ones into Topaz afterwards.

One caution on chaining them: Image Toolbox's Conciliation matches videos by
content-hash **lineage only**, with no filename fallback, so it will not recognise
a Topaz-produced file as the counterpart of an original. That is deliberate (a name
guess could replace a whole source with a short clip), and it means Topaz output has
to be filed manually. Preview first, as always.

---

## 7. Sources

- Topaz Video product page: <https://www.topazlabs.com/topaz-video>
- Topaz Video Pro: <https://www.topazlabs.com/video-pro>
- Topaz Video documentation (models, enhancement filters, encoders and containers,
  cloud rendering, system requirements): <https://docs.topazlabs.com/topaz-video>
- Supported formats, encoders and containers:
  <https://docs.topazlabs.com/video-ai/reference-guide/encoders-and-containers>
- Cloud rendering limits and retention:
  <https://docs.topazlabs.com/topaz-video/cloud-rendering>
- Pause/resume reliability reports, Topaz Community:
  <https://community.topazlabs.com/t/pause-resume-threw-away-17-hours-of-rendered-footage/83266>
- Perpetual-licence discontinuation (2025-10-03), CG Channel:
  <https://www.cgchannel.com/2025/09/topaz-labs-to-end-perpetual-licenses-of-its-software/>
- Image Toolbox: this repository's `README.md`, `CLAUDE.md`,
  `docs/video-upscaler.md`, `docs/local-video-upscaler.md`,
  `docs/benchmark-sharing.md` and `docs/future-features.md`
- Companion documents: [`topaz-gigapixel-vs-image-toolbox.md`](topaz-gigapixel-vs-image-toolbox.md),
  [`upscayl-vs-image-toolbox.md`](upscayl-vs-image-toolbox.md)
