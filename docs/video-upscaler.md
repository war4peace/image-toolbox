# Video Upscaler (design & plan)

Design and planning notes for the **Video Upscaler**, a major new
**RunPod-only** feature: upscale a collection of videos with SeedVR2, the same
engine the Batch Upscaler already uses for stills. UI tab name: **"Video
Upscaler"**.

> Status: **planning only.** Nothing here is built yet. Per-frame timing numbers
> are carried over from the image path's benchmarks and MUST be re-measured for
> the temporal-batch video path before the cost estimator is trusted (see
> "Benchmark prerequisite"). This doc is the single source of truth for the
> feature; `docs/future-features.md` #2 is just a pointer here.

---

## 1. Goal & scope

- **Input:** a folder (or selection) of videos. Clips of a few seconds up to
  ~1 hour, source resolutions roughly 320x240 to 1280x800 (old camera footage is
  the motivating case).
- **Output:** an upscaled copy per video to a user-chosen **target** (1080p /
  1440p / 4K), using the **same "first reachable edge wins" fit math** as the
  Batch Upscaler (`compute_seedvr2_resolution` / `_skip_for_dims` in
  `batch_upscale.py`), applied per-video from the frame dimensions.
- **Never touch the source.** Source videos are read-only; everything happens on
  copies in a temp folder and a separate output tree (same promise the upscaler
  already keeps for images).
- **No rotation.** Skip the orientation/auto-straighten step entirely (videos are
  not sideways camera photos). This removes the whole `orientation.py` dependency
  from this path.
- **RunPod-only by design.** Local SeedVR2 video is both too slow (a diffusion
  pass per frame) and exposed to the GPU-degradation bug that motivated remote
  upscaling (#1). This feature never offers a local path.
- **Shared config & database.** No new config file and no new database. Settings
  live in a new `video` section of the existing `config.json`; resume/queue state
  lives in new tables in the existing `db/cache.db`. See sections 9 and 10.

## 2. The engine already does this

SeedVR2 is a *video* upscaler; the app simply never uses that side.
`seedvr2/inference_cli.py` has mature native video support the image path
bypasses:

- **Streaming chunk processing** (`--chunk_size`): memory-bounded frame loading so
  a long file never loads fully into RAM.
- **Temporal attention window** (`--batch_size`, must be `4n+1`: 1, 5, 9, ...):
  the consecutive frames denoised together. The main quality/coherence lever.
- **Cross-boundary blending** (`--temporal_overlap`, `--prepend_frames`,
  `--uniform_batch_size`).
- **FFMPEG writer with 10-bit x265** (`--video_backend ffmpeg --10bit`), fps
  read-and-preserve, and the common containers
  (`.mp4/.avi/.mov/.mkv/.webm/.flv/.wmv/.m4v`).

So this is an **orchestration + UX + tuning** feature, not an upscaler build.

## 3. Terminology (kept distinct on purpose)

| Term | Meaning | Layer |
|---|---|---|
| **segment** | a local ffmpeg split of the source video; the unit of work the queue tracks | our orchestration |
| **chunk** | SeedVR2's internal `chunk_size` frame-streaming setting | the engine |

These are different things at different layers. The doc never uses "chunk" to mean
a segment.

## 4. Architecture: segment = queue item

Do **not** treat a video as one giant remote job. Split each source into short
segments locally, and make **a segment the unit of work**, exactly like an image
is on the Batch Upscaler today. This reuses #1's per-item streaming machinery
almost wholesale.

```
source.mp4 ──ffmpeg split──> seg000.mp4 seg001.mp4 ... segNNN.mp4   (temp folder)
                  │
                  ├─ per segment: upload ─> pod upscales ─> download upscaled seg
                  │                         (queue/resume/cost/heartbeat stay local)
                  ▼
   concat upscaled segments ──> mux original audio ──> output/source.mp4
```

### Pipeline

1. **Split** the source into segment *copies* in a temp folder (source read-only):
   `ffmpeg -c copy -f segment` (lossless stream-copy). Cuts land only on
   keyframes, so each segment is *at least* the target length, **rounded UP to the
   next keyframe** (not down). Normal footage (keyframes every 1-10 s) yields
   ~60-70 s segments at a 60 s target, which is fine. See section 6.1 for the
   sparse-keyframe fallback.
2. **Stream per segment** over the existing pod path: upload one segment, the pod
   upscales it (SeedVR2 video path with the tuned settings), download the upscaled
   segment. The local queue, resume-cache, cost tracking, dead-man's-switch
   heartbeat, and notifications all stay local and barely change.
3. **Reassemble** locally once all of a video's segments are done: ffmpeg `concat`
   demuxer over the upscaled segments, then mux the **original audio** back in
   (`-map 0:v -map 1:a -c copy`). Run the duration-drift check (section 6.3) and
   surface a warning if input and output diverge.

### Why this beats a single-streaming-file job

- **Segment-level resume:** a dropped pod loses one in-flight segment, not a
  multi-day job. No partial-mp4 checkpointing.
- **Reuse:** the local queue/resume/cost code is the same shape as the image path.
- **Multi-pod parallelism (future):** independent segment files can be spread
  across several pods for ~Nx faster wall-clock at the same total cost. The
  single-streaming-file model cannot do this. Not in v1, but the architecture
  leaves the door open.

## 5. Cost is the dominant constraint, paid in installments

SeedVR2 runs **one diffusion pass per output frame**, so cost scales with frame
count and *output* resolution (low-res sources get no discount: a 320x240 frame
upscaled to 4K costs the same as any 4K frame). **Temporal batching makes this far
cheaper than the single-image rate** (see the measured benchmark in section 7):
warm throughput is **~0.74 s/frame at 1080p** on the test card, roughly 10x better
than the carried-over single-image 7.6 s/frame. Per-frame at the test card
(RTX PRO 6000, 7B fp16, $2.09/h), 1-minute clip @ 30 fps:

| Clip @ 30 fps | Frames | 1080p (~0.74 s/f) | 1440p (~1.9 s/f) | 4K (~6.6 s/f) |
|---|---|---|---|---|
| 30 s | 900 | ~0.4 h / $0.39 | ~0.5 h / $1.0 | ~1.7 h / $3.5 |
| 1 min | 1,800 | ~0.4 h / $0.77 | ~0.95 h / $2.0 | ~3.3 h / $6.9 |
| 10 min | 18,000 | ~3.7 h / $7.7 | ~9.5 h / $20 | ~33 h / $69 |
| 1 hour | 108,000 | ~22 h / $46 | ~57 h / $119 | ~198 h / $414 |

Caveats: these are **first-pass, single-card** numbers on a 96 GB RTX PRO 6000 (the
batch_size ceiling card). The cost estimator must still be calibrated on the cards
users actually rent (RTX 5090 32 GB etc.), where per-frame may be slower but $/h is
lower, so total cost could land similar or better; that run is pending (section 13).
4K is ~9x the 1080p rate and needs a big-VRAM card (4K ceiling was batch 5 on 96 GB).
Splitting is free (local ffmpeg) and RunPod bandwidth is free, so segmenting costs
nothing extra, **but it also saves nothing**: it buys resilience and parallelism,
not speed.

### Installments (a first-class UX goal, not an accident of resume)

There is no reason to spend $46 (1080p) to $400+ (4K) up front on Grandma's
hour-long birthday video. Because
resume is per-segment, a user can upscale one minute today and another next week,
and the partial result is always a usable, already-reassembled video of the
segments done so far. To make that deliberate:

- **Frame-count cost/time estimator shown before every run**: `frames = duration x
  fps`; `est = frames x per-frame-rate(target) x $/h`. **Hard confirmation** above
  a configurable threshold (`video.cost_confirm_usd`, default e.g. $10). This is
  the single most important UX element: without it a user walks away from a 1-hour
  4K video and returns to a ~$400 bill.
- **Per-run cap** (`video.per_run_minute_cap` / `video.per_run_cost_cap`): "process
  up to N minutes / $X this run, then stop." Makes the installment model one click,
  not manual file-splitting.

## 6. Decisions locked in

### 6.1 Keyframes: round-up is fine; re-encode only the pathological cases

**Decision:** rounding segment length up to the next keyframe is acceptable. Only
when keyframes are **extremely sparse** does the re-encode fallback kick in.

- Every decodable video has frame 0 as a keyframe, so "no keyframes at all" means a
  *corrupt* file: skip + log, exactly as the image path handles a corrupt image.
- `-c copy` can only cut on keyframes, so a long GOP forces a long minimum segment
  (a 2-min GOP forces 2-min segments; an hour-long single GOP yields one
  un-splittable segment).
- **Implementation:** `ffprobe` the keyframe interval first. If the resulting
  segment length would exceed a threshold (`video.max_segment_seconds`, default
  e.g. 120 s = 2x the 60 s target), fall back to a **forced-keyframe re-encode**
  for that video only (`-force_key_frames "expr:gte(t,n_forced*<seg>)"`), then
  segment the re-encoded copy. Quality is a non-issue (SeedVR2 hallucinates detail,
  so a visually-lossless intermediate is far above what it needs); the real cost of
  the fallback is time + disk for a full re-encode, so prefer `-c copy` and
  re-encode only when forced. The re-encode runs **locally** with the encoder chosen
  per 6.4 (nvenc if available, else CPU).

### 6.2 Segment edges: fixed seed per video (v1), revisit only if visible

**Decision:** start with a **single fixed seed for the whole source video** (not
per-segment-random like the image path), then **visually inspect** the results. If
segment boundaries are not obvious, keep this simple solution; only escalate if and
when artifacts demand it.

- Independent per-segment processing loses temporal blending *across* the cut, so
  the same continuous motion gets slightly different hallucinated detail on each
  side (a subtle texture "pop", once per segment). Usually invisible on noisy /
  cut-heavy old footage.
- The fixed seed removes the seed component of the pop for free. That is the entire
  v1 mitigation.
- **Deferred options, documented for later (do NOT build in v1):**
  (2) overlap + trim (split a few frames early, drop the overlap on reassembly);
  (3) the proper fix, `--prepend_frames` context-carry: build each segment's input
  locally as `[tail of previous segment] + [this segment]` and pass
  `--prepend_frames = tail_length` so SeedVR2 upscales with shared context and
  auto-removes the prepend from output. (3) still composes with multi-pod
  parallelism because the overlapped inputs are built locally before upload.

### 6.3 Duration drift: detection + warnings are mandatory in v1

**Decision:** short videos may show no detectable drift; long videos will need
manual review. Either way the app must **at least detect and warn** when the output
diverges from the input. It does not silently "fix" anything.

- **Detect (required in v1):** compare input vs output **total frame count** and
  **runtime/duration** (via `ffprobe`), and check that
  `sum(upscaled segment frame counts) == source frame count`. Any divergence beyond
  a small tolerance (e.g. > 1 frame, or > ~50 ms) raises a clear warning in the log,
  the UI, and the completion notification, and the output is flagged "review
  recommended". Keep the upscaled segments either way.
- **Causes, worst-first:** *fps drift* (output fps != source, or a VFR source) ->
  progressive lip-sync drift, the dangerous one; *frame-count drift* (VFR sources,
  fps rounding: the per-segment assertion below catches it); a *constant offset* ->
  fixed desync. Note `--uniform_batch_size` is **not** a frame-count risk: SeedVR2
  trims all temporal padding (both its `4n+1` padding and the uniform padding) back
  to the original frame count on decode, so output frames == input frames
  (verified: `generation_phases.py` saves `ori_length` at line 366 and slices back
  to it at lines 950-957).
- **Prevent where cheap:** normalise to **CFR** before segmenting (`-vsync cfr`)
  to kill the main VFR cause. Detection still runs regardless.
- **Do not** stretch audio with `atempo` to force a match (it shifts pitch). On a
  real mismatch, warn and leave it for the user, since by mux time progressive
  drift is unfixable cleanly. The up-front CFR normalize + per-segment frame-count
  assertion are what actually prevent it.

### 6.4 ffmpeg: bundled, and all container work stays local

**Decision:** the app owns a bundled ffmpeg, and **all** container-level work
(probe / split / CFR-normalize / re-encode / reassemble / audio-mux) runs
**locally**. The pod stays a thin SeedVR2-only worker. Re-encode location is chosen
by **local GPU capability, not video size**.

- **Bundle ffmpeg via `bootstrap.ps1` download** (not baked into the installer),
  matching how PyTorch / SeedVR weights are delivered, so the installer stays tiny
  and the version is pinned and known-good (gyan.dev `release-essentials` build:
  GPLv3, bundles nvenc + libx264/libx265; see 13 for the exact pin). **Needed in
  *both* install modes:** even a Remote-only install
  must have local ffmpeg for split/reassemble/mux, so this is the one heavy local
  component Remote-only cannot skip. Note the **GPL** angle (an ffmpeg with
  x264/x265 is GPL): ship its license, same as the vendored SeedVR2.
- **Split is free; re-encode is the only expensive part, and only a minority of
  videos need it.** A `-c copy` split is instant, lossless stream-copy on any CPU,
  no GPU. Most old home videos are CFR with sane GOPs and a decodable codec, so they
  split locally with `-c copy` *regardless of size*. A re-encode is triggered only
  by: VFR (CFR-normalize, 6.3), a sparse GOP (forced keyframes, 6.1), or a codec the
  pod's opencv/ffmpeg can't decode. When triggered, fold it into the segmenting pass
  (one `ffmpeg` invocation: `-vsync cfr` + `-force_key_frames` + `-f segment`).
- **Why local, not on the pod:** the re-encode is negligible next to the upscale
  (even a slow CPU x265 of a 30-min SD clip is minutes; upscaling it is days), while
  uploading the whole file to re-encode on the pod would break **segment-level
  resume** (a dropped pod loses the whole upload, not one segment) and bloat the
  pod into an ffmpeg orchestrator. Keeping all ffmpeg local preserves the clean
  per-segment resume model and the thin-pod philosophy, for a sub-1% time cost.
- **Encoder choice by local capability (the only local codec decision):** Local /
  Both install (CUDA GPU + nvenc-enabled build) -> **nvenc**, h265 if supported else
  h264 (auto-detect). Remote-only install (no capable GPU) -> CPU libx265/libx264
  fallback, with a "re-encode pre-pass may be slow on this machine" notice. Quality
  here barely matters (SeedVR2 hallucinates detail), so a fast encode is plenty.
- **The deliverable's codec is decided on the pod, so there is no local output-codec
  choice.** SeedVR2's own `FFMPEGVideoWriter` encodes each upscaled segment (x264, or
  x265 10-bit via `video_backend ffmpeg` + `use_10bit`). Local **reassembly is
  `-c copy` concat + `-c copy` audio mux** with no re-encode of the result, so all
  segments share identical codec params (same worker settings) and concat stays
  lossless.
- **Two `-c copy` exceptions found in phase 2 (refines the two bullets above):**
  (a) **Audio may not fit mp4.** Old-camera audio codecs (measured: `Pisici.AVI`
  carries `pcm_mulaw`) cannot `-c copy` into an mp4/mov container, so the **audio
  alone** is re-encoded to AAC (the *video* still `-c copy`s — 6.4's real concern).
  Detected by checking the source audio codec against an mp4-friendly set
  (`aac/mp3/ac3/eac3/alac`). (b) **Re-encode must normalize pixel format.** When the
  split DOES re-encode (VFR/sparse-GOP), NVENC rejects 4:2:2 input (measured:
  `hevc_nvenc` 400s on the source's `yuvj422p` with "YUV422P not supported"), so the
  re-encode forces `-pix_fmt yuv420p` (universal for x264/x265/nvenc; harmless, since
  that intermediate is upscaled downstream by a detail-hallucinating model).

## 7. SeedVR2 video settings + benchmark prerequisite

The real R&D, with no shortcut. The video-only knobs (none used by the image path)
need tuning on a pod against a few representative clips. Drive it from
`scripts/benchmarks.py`.

- **`batch_size`** (`4n+1`): main quality lever (temporal coherence). Measured
  (see results below): throughput gains **saturate by ~bs 9-13** (most of the win is
  bs1 -> bs5 -> bs9), and peak VRAM **plateaus** (it does not grow linearly, because
  the video VAE compresses the temporal axis), so a moderate window is the sweet
  spot. The VRAM ceiling is the real limit and is set by **output resolution**: on
  96 GB, 1080p and 1440p never OOM up to bs89, but **4K tops out at bs 5**.
- **`temporal_overlap`**: suppress seams between internal batches.
- **`chunk_size`**: VRAM bound for frame loading **and the system-RAM bound** (8); the
  worker must always set it > 0 so output frames stream out instead of accumulating.
- **`uniform_batch_size`**: pad the final short batch to avoid temporal artifacts
  from a small last batch. Safe for frame count: SeedVR2 trims the padding back to
  the original length on decode (confirmed, see 6.3), so it never changes the output
  frame count.
- **`color_correction lab`**: same default as the image path.
- **`seed`**: one fixed value per source video (per 6.2).
- **Segment length** (`video.segment_seconds`): a new tuning variable. Shorter
  segments give finer resume + more parallelism + more boundary seams + more
  overhead; longer the reverse. Default ~60 s; let the benchmark refine it.

The benchmark must also produce trustworthy **per-frame rates per (target x card)**
to feed the cost estimator (section 5), replacing the carried-over image numbers.

**Test asset:** `benchmark-videos/Pisici.AVI` (gitignored, local-only): an original
320x240, 30 fps, 2m44s (~4,920 frames) old-camera clip, the exact motivating
case. The harness (`pod/bench_video.py`) reads short windows from it per config to
measure throughput + VRAM cheaply; a full-video 1080p run is ~1 h of compute
(~$2 on the test card), useful as one end-to-end correctness pass but not per config.

### Measured results (first pass: RTX PRO 6000 Blackwell, 96 GB, 7B fp16, 2026-06-27)

Warm per-frame (one temporal window of N frames; bs1 is overhead-dominated), and
peak VRAM, on `pod/bench_video.py`. **`per-frame` excludes model load**; the card
keeps DiT+VAE resident (>=40 GB), so these are best-case (a 32 GB card offloads DiT
and will differ):

| Target (output) | bs1 | bs5 | bs9 | bs13 | bs21 | floor | VRAM | ceiling |
|---|---|---|---|---|---|---|---|---|
| 1080p (1440x1080) | 1.44 | 0.81 | 0.76 | 0.74 | 0.70 | ~0.67 (bs89) | plateau ~50 GB | none <=bs89 |
| 1440p (1920x1440) | 2.71 | 2.04 | 1.98 | 1.96 | 1.91 | ~1.88 (bs89) | plateau ~71 GB | none <=bs89 |
| 4K (2880x2160) | 6.64 | 6.63 | OOM | - | - | 6.63 | bs5 = 81 GB | **bs5** |

Findings that shaped the plan: (1) **temporal batching is ~10x faster per frame than
single-image** (1080p warm ~0.7 s vs the carried-over 7.6 s); (2) **gains saturate by
~bs 9-13**, so a moderate window is the operating point, not the max; (3) **VRAM
plateaus** (temporal VAE compression), so batch_size is bounded by a per-resolution
ceiling, not a linear climb; (4) **4K needs a big-VRAM card** (bs5 ceiling at 96 GB,
no batching speedup) and is ~9x the 1080p cost; (5) **load-all vs streaming RAM
confirmed** (see 8): load-all ~50 MB/frame (~90 GB for a 1-min 1080p segment),
streaming flat. Still pending (section 13): the same throughput/VRAM grid on the
cards users actually rent (RTX 5090 etc.) for the real cost calibration.

### Measured results (second pass: B200, 180 GB, 7B fp16, 2026-06-27)

Same harness, full ladder. The B200 was benchmarked while it was briefly in stock in
EU-RO-1 (not normally available there: see section 13). 178 GB usable, DiT+VAE
resident, $5.89/h. (4K stopped at bs45: per-frame had already saturated and VRAM was
pinned, so bs61/bs89 were skipped to save pod time.)

| Target (output) | bs1 | bs5 | bs9 | bs13 | bs21 | floor | VRAM | ceiling |
|---|---|---|---|---|---|---|---|---|
| 1080p (1440x1080) | 0.93 | 0.45 | 0.42 | 0.41 | 0.38 | ~0.35 (bs89) | plateau ~49 GB | none <=bs89 |
| 1440p (1920x1440) | 1.83 | 1.73 | 1.71 | 1.70 | 1.66 | ~1.63 (bs61) | plateau ~74 GB | none <=bs89 |
| 4K (2880x2160) | 4.34 | 6.81 | 6.55 | 6.43 | 6.29 | ~6.19 (bs45) | plateau ~135 GB | none <=bs45 |

B200 vs PRO 6000 (both 7B fp16, resident, warm bs13): **1080p ~1.8x faster**
(0.41 vs 0.74 s/frame), **1440p ~1.15x** (1.70 vs 1.96), **4K ~1.03x** (6.43 vs 6.63).
Two structural differences beyond raw speed: (a) the B200 **removes the 4K batch
ceiling** the PRO 6000 hit: 4K ran bs5->bs45 sitting at a flat **135 GB** (vs the PRO
6000's bs5 = 81 GB hard ceiling); 4K per-frame is **flat from bs9 on**, so the extra
VRAM buys no *throughput*, but it is **not wasted: a larger window is a continuity
lever, not just a memory cost** (see the batch-size/continuity note below). (b) **4K
bs1 (4.34 s) is faster per-frame than any batched 4K** (~6.2-6.8 s), same shape seen on
the PRO 6000 (bs1 ~= bs5): the temporal-attention pass doesn't speed up 4K, it only
adds cross-frame coherence, so at 4K you pay for continuity, you don't get it free as
at 1080p. The takeaway: the B200's *throughput* win is concentrated at **1080p** (where
it nearly doubles it), exactly the target a fast card should serve; at 4K per-frame is
card-independent, so a cheaper big-VRAM card matches it on $/frame **at equal window
size** but a bigger card buys a bigger 4K window (more continuity), which an 80 GB card
caps at ~bs5. **load-all vs streaming RAM is host-side and card-independent**:
re-measured on the B200, load-all still balloons and streaming stays flat (see section
8): the worker streams regardless of GPU.

### Measured results (third pass: H200 SXM, 141 GB, 7B fp16, 2026-06-27)

Same harness, in EU-FR-1 (the second region-locked volume, `lpla3ia3l0`). 140 GB usable,
resident, $4.39/h. Run to push the **batch-size-until-OOM** question (continuity ceiling).

| Target (output) | bs1 | bs5 | bs9 | bs13 | bs45 | bs89 | per-frame floor | VRAM |
|---|---|---|---|---|---|---|---|---|
| 1080p | (B200/PRO 6000 cover it; H200 is overkill here) ||||||| ~49 GB plateau |
| 1440p (1920x1440) | 2.31 | 2.15 | 2.08 | 1.99 | 1.90 | 1.89 | ~1.88 | plateau ~71-77 GB |
| 4K (2880x2160) | 6.01 | 7.86 | 7.34 | 7.24 | - | - | ~7.2 | plateau ~128 GB |

H200 is ~15% slower per-frame than the B200 at 1440p (1.9 vs 1.64) and 4K (7.2 vs 6.4),
for ~$1.50/h less. **The decisive finding: VRAM plateaus at every target** (1080p ~49 GB,
1440p ~71-77 GB, 4K ~128 GB on H200 / ~135 GB on B200), jumping to the plateau by ~bs9 and
then **flat**: the temporal VAE compresses the time axis, so adding frames to a window
costs almost no extra VRAM. The only VRAM term that grows is the output tensor
(~35 MB/frame at 4K). So **the GPU does not OOM at any practical window**: extrapolated 4K
OOM is ~`bs>300` on the H200 (a >35-min single window) and ~`bs1200` on the B200 (a
~2.5-hour window). "Run until OOM" is therefore not a useful continuity ceiling at these
resolutions: **the real limit on window length is time (and pod RAM on small pods), not
VRAM** (see section 8 for the pod-RAM coupling and the cgroup-not-`free` correction). 4K
on the B200 was left at bs45 not because of a ceiling but because per-frame had saturated
and further rows only add cost.

### Measured results (fourth pass: RTX 5090, 32 GB, 1080p, the cheap-card calibration)

The card a cost-conscious 1080p user actually rents (EU-RO-1, $0.99/h, stock High). At
32 GB it is **below the 40 GB resident threshold, so the engine offloads DiT/VAE to CPU
(`resident=False`)** and reloads them over PCIe each window: a genuinely different regime
from the big cards, and the reason this needs its own numbers.

| bs | 1 | 5 | 9 | 13 | 21 | 29 | 45 | 61 | 89 |
|---|---|---|---|---|---|---|---|---|---|
| per-frame (s) | 7.08 | 1.89 | 1.44 | 1.28 | 1.05 | 0.98 | **0.94** | 0.98 | OOM |
| peak VRAM (GB) | 16.5 | 19.3 | 30.7 | 30.7 | 30.7 | 30.7 | 30.7 | 30.7 | - |

Findings: (1) **bs1 = 7.08 s/frame** is the offload penalty (load the 16 GB DiT to GPU,
run one frame, offload) - this IS the carried-over "single-image 7.6 s" number, now
explained. (2) **Batching matters far more on a cheap card: 7.5x** (7.08 -> 0.94 s) vs
~2x on a resident big card, because it amortizes the per-window DiT load. So the video
path's temporal batching is what makes the 5090 viable at all. (3) **VRAM plateaus at
30.7 GB hard against the 32 GB wall**, so the **continuity ceiling is bs61** (OOM at
bs89) - a ~2 s window, vs the big cards' bs89+. (4) Pod RAM is only **92 GB / 85.7 GiB**
(cgroup), so a 1-min 1080p load-all (~90 GB) would nearly fill it: streaming is
mandatory, not optional, on this pod.

**1080p cost ranking (1-hour 30 fps video = 108k frames, per-frame at each card's floor):**

| Card | per-frame | GPU-hours | $/h | **cost/hr-of-video** |
|---|---|---|---|---|
| **RTX 5090** | 0.94 s | 28.2 | $0.99 | **~$28** (cheapest) |
| PRO 6000 | 0.74 s | 22.2 | $2.09 | ~$46 |
| H200 | ~0.47 s | 14.1 | $4.39 | ~$62 |
| B200 | 0.35 s | 10.5 | $5.89 | ~$62 |

The 5090 is **~40% the B200's cost and half the PRO 6000's** for 1080p, and at $0.99/h
it is also the lowest hourly of the four, so it is the right **default for 1080p**:
cheapest total, least exposure on a dropped connection. The big cards win wall-clock
time and longer windows, not cost. **1080p ->
5090** is now confirmed.

### Measured results (fifth pass: A100 80 GB PCIe, 1440p) - the cheap card LOSES here

The card expected to be the cheap 1440p pick (EU-RO-1, $1.39/h). Resident (80 GB > 40 GB).

| bs | 1 | 5 | 9 | 13 | 21 | 45 | 61 | 89 |
|---|---|---|---|---|---|---|---|---|
| per-frame (s) | 3.65 | 3.65 | 3.62 | 3.57 | 3.48 | 3.45 | 3.43 | **3.42** |
| peak VRAM (GB) | 26 | 47 | 67.6 | 67.6 | 72.9 | 72.9 | 72.9 | 78.2 |

The A100 is **Ampere**: at ~3.42 s/frame it is **~1.75x slower than the PRO 6000** (1.96 s)
and batching barely helps (~7%, vs the 5090's 7.5x), because it is resident and purely
compute-bound. No OOM (plateau ~73 GB inside 80 GB). The cost consequence is the
**counterintuitive result worth measuring**:

**1440p cost ranking (1-hour 30 fps video = 108k frames, per-frame at floor):**

| Card | per-frame | GPU-hours | $/h | **cost/hr-of-video** |
|---|---|---|---|---|
| **PRO 6000** | 1.96 s | 58.8 | $2.09 | **~$123** (cheapest) |
| A100 80 GB | 3.42 s | 102.6 | $1.39 | ~$143 |
| H200 | 1.89 s | 56.7 | $4.39 | ~$249 |
| B200 | 1.64 s | 49.2 | $5.89 | ~$290 |

The A100's **lower hourly is more than eaten by its slower throughput**, so it costs
*more* than the PRO 6000 and is **dominated** (slower AND pricier). So the 1440p pick is
the **PRO 6000**, not the A100, refuting the "cheaper card = cheaper job" intuition: at a
fixed job, $/frame = per-frame x $/h, and the PRO 6000's speed wins. **Price-ceiling
implication:** the cheapest viable 1440p card ($2.09) is **above** the $1.10 upscale
ceiling tuned for the 5090, so the Video Upscaler needs a **higher (per-target) price
ceiling for 1440p/4K** than the image upscaler's 1080p-tuned one, or 1440p/4K refuse to
auto-deploy.

**Batch size is a continuity lever, not only a throughput knob (source-confirmed).**
Within one `batch_size` window the frames are denoised *jointly* with shared temporal
attention, so a window has no internal seam; consecutive windows advance by
`step = batch_size - temporal_overlap` and the overlap frames are reprocessed in both
and blended, softening the join. So a **larger window = fewer joins = longer
continuously-coherent runs**. SeedVR2's own code agrees: `calculate_optimal_batch_params`
returns `best_batch = largest 4n+1 <= total_frames` commented *"maximizes temporal
stability"*, and the runtime logs *"Matching batch_size to shot length improves temporal
coherence."* The ideal window is the **whole shot**; VRAM is the only reason to chunk it.
This means the throughput knee (~bs9-13, where per-frame stops dropping) is **not** the
quality operating point: since per-frame is flat past the knee, a bigger window costs ~the
same total $ for a segment but yields fewer seams, so on a big-VRAM card the quality
operating point is **the largest window VRAM allows at the target** (with a modest
`temporal_overlap`), not bs13. Caveats: gains have diminishing returns (the DiT's trained
temporal context) and a 1-minute segment (~1800 frames) never fits as one window anyway,
so this is "bigger-is-smoother within the VRAM budget", and overlap trades a little
throughput (reprocesses `overlap` frames per window) for the join blend. Not yet measured
perceptually (benchmark ran `temporal_overlap=0` for clean throughput) -> see section 13.

## 8. Other gotchas to honour

- **Audio is dropped by the engine.** `FFMPEGVideoWriter` pipes only rawvideo
  frames, so the upscaled output is **silent** until reassembly muxes the original
  track back. Non-negotiable.
- **Dead-man's switch vs. long segments.** A single segment is still hours of
  compute, so the worker must **touch the heartbeat per internal `chunk_size`
  iteration** (the streaming generator in `inference_cli` yields per chunk, an ideal
  hook), or the 15-min idle timeout self-stops the pod mid-segment. Run with
  `max_runtime=0` and rely on active-heartbeat idle, as the image path already does.
- **Per-segment transaction must poll, not block.** A multi-hour segment cannot be
  a single blocking HTTP request (the image engine's timeout is 600 s). Use
  submit -> poll frames-done/total -> fetch result; this also feeds live
  intra-segment progress to the UI for free.
- **System RAM: stream, never load-all (the worker MUST set `chunk_size > 0`).**
  **Measured** (`pod/ram_probe.py`, 600 frames @ 1080p): load-all (`chunk_size = 0`)
  used **30.3 GB** of system RAM, while streaming (`chunk_size = 33`) stayed **flat at
  0 GB frame-accumulation delta**. **Re-confirmed on the B200 (30.6 GB load-all vs 0.0 GB
  streaming): host-side, card-independent.** Bonus: streaming was also **faster
  end-to-end** there (272 s vs 431 s), because load-all defers the entire mp4 encode to
  one ~3-min burst at the end while streaming encodes incrementally: so streaming wins on
  both RAM and wall-clock, no tradeoff. SeedVR2's load-all path holds every output frame
  through assembly as multiple **float32** copies (decode + LAB color-correction +
  assembly), so it is **~50 MB/frame**, not the ~9 MB/frame a single fp16 buffer would
  be. That extrapolates to **~90 GB for a 1-minute 1080p segment and ~360 GB at 4K** -
  impossible on any pod (and the exact ceiling the author hit on a 64 GB / RTX 3090
  box; it is independent of VRAM). Streaming writes each chunk to the encoder and
  frees it, so RAM stays flat regardless of segment length. The worker must
  **always stream**, `chunk_size` sized to the pod's RAM (`chunk_size >= batch_size`;
  measured 33 was already flat). The **pod** does this assembly, not the local side
  (local only stream-copies). Our 1-minute segments do **not** replace streaming
  (a load-all minute already needs ~90 GB).
  **4K per-frame measured (H200): ~253 MB/frame load-all** (15.2 GB / 60 frames), so a
  1-minute 4K segment load-all is **~455 GB** (worse than the earlier ~360 GB estimate).
  Streaming `chunk_size = 30` at 4K peaked **<8 GB** total RSS, confirming the bound holds
  at 4K too.
- **Window length vs pod RAM are coupled (the `chunk_size >= batch_size` rule).** Because
  a chunk must contain whole windows ([inference_cli.py] rounds the chunk up to a multiple
  of `batch_size`), a longer *coherent* window forces a larger streaming chunk, so the pod
  must hold **>= batch_size** decoded frames during reassembly. Per-window RAM therefore
  scales with `batch_size x frame_bytes` (~130 MB/frame held at 4K streaming, ~250 MB
  worst case). The worker sets `chunk_size = batch_size` (minimum RAM for the chosen
  window) and picks `batch_size = min(VRAM-fit, pod-RAM-fit)`. So the continuity lever
  (section 7) is bounded by pod RAM at 4K, not only VRAM.
- **Read the pod's cgroup RAM limit, NOT `free` / host RAM.** A RunPod pod is a container
  on a shared host; `free` (and `nvidia-smi`-adjacent host probes) report the **whole
  server**. Measured: a 1-GPU H200 pod's `free` showed **2015 GB** but its cgroup
  (`/sys/fs/cgroup/memory[.v1 limit_in_bytes | .v2 max]`) and the RunPod listing both say
  **251 GB / 234 GiB**, 24 vCPU (the host is 2 TB / 192 CPU). The kernel OOM-kills the
  container at the **cgroup** limit regardless of host free RAM, so the worker's RAM
  sizing and the cost estimator's "does this window fit" check must read the cgroup limit.
  (This also means the remote-pod telemetry row, section 11 / `pod/worker.py /telemetry`,
  must report cgroup RAM, not host RAM, or it overstates the pod by ~8x.)
- **At 4K, which ceiling binds first is pod-specific.** On the 251 GB H200 the GPU's
  output-tensor growth OOMs (~`bs>300`, a >35-min window) **before** RAM does (~bs900+),
  and both are far past any practical window, so **time** is the real limit. On a cheaper
  4K-capable pod with a smaller RAM slice, RAM can bind first. The estimator should warn
  when a requested 4K window exceeds either the pod's VRAM or its cgroup RAM.
- **Container disk.** A long 4K output is many GB; the pod `containerDiskInGb`
  (default 30) must hold input + output, or write to the mounted volume.
- **4K video is the practical ceiling problem.** `batch_size` VRAM at 4K forces a
  tiny temporal window (weaker coherence) and/or a big expensive card, compounding
  the cost. 1080p is the sane default for video; gate 4K behind an extra warning.

## 9. Shared config (`config.json` -> new `video` section)

No new config file. A new section alongside `upscale`, `tagging`, `runpod`, etc.
The whole `runpod` section (pod lifecycle, GPU pickers, price ceilings,
dead-man's-switch limits, SSH) is reused unchanged. Draft keys:

```jsonc
"video": {
    "target": "1080p",              // 1080p | 1440p | 4K (own target, not upscale's)
    "skip_cutoff_pct": 0,           // same meaning as upscale's skip-cutoff
    "segment_seconds": 60,          // nominal segment length
    "max_segment_seconds": 120,     // sparse-keyframe re-encode trigger (6.1)
    "batch_size": 0,                // 0 = auto per (target x card) from benchmark
    "temporal_overlap": 0,
    "chunk_size": 0,                // 0 = engine default / auto
    "color_correction": "lab",
    "video_backend": "ffmpeg",      // pod-side output writer; ffmpeg enables 10-bit
    "use_10bit": false,             // pod-side deliverable codec (x265 10-bit)
    "normalize_cfr": true,          // -vsync cfr before segmenting (6.3)
    "local_reencode_codec": "auto", // auto = nvenc(h265->h264) if available, else CPU (6.4)
    "per_run_minute_cap": 0,        // 0 = no cap; installment control (section 5)
    "per_run_cost_cap": 0.0,        // 0 = no cap
    "cost_confirm_usd": 10.0        // hard confirmation above this estimate
}
```

`defaults` gains a Video Upscaler source/output folder pair like the other tools.

## 10. Shared database (`db/cache.db` -> new tables)

No new database. New tables in the existing cache, mirroring the upscale
eligibility/resume pattern (`upscale_roots` / `upscale_files`) so resume and the
installment model work across runs:

- **`video_roots`** (source folder -> scan metadata), like `upscale_roots`.
- **`video_files`** (one row per source video: path, dimensions, fps, total
  frames, duration, chosen target, status, output path). Drives the queue and the
  "skip already-done / near-target" decision.
- **`video_segments`** (one row per segment: parent video id, segment index, time
  span, input frame count, status `pending|done|failed`, upscaled output path,
  output frame count). This is what makes **segment-level resume** and the
  per-run cap work: a re-run picks up the first `pending`/`failed` segment.

Logs stay text files in `logs/`, not the DB, consistent with the rest of the app.

## 11. Build pieces

- **`scripts/batch_video_upscale.py`** runner sibling: **BUILT (phase 4).** Walks
  the source tree, mirrors to the output root, splits each video locally
  (video_pipeline), streams each segment through the injected engine, reassembles
  + mux + drift-checks, manages the `video_*` resume tables and the per-run
  installment cap, sends notifications. No torch import (remote-only). The engine
  is injected (`run_batch(engine, …)`) so the same orchestration runs against the
  real `RemoteVideoEngine` or a built-in `PassthroughVideoEngine` (`--passthrough`,
  no pod: stream-copies each segment) for testing the whole pipeline GPU-free.
- **Worker `--mode video`** (`pod/worker.py`): **BUILT (phase 3).** Loads SeedVR2
  once (like `full`) and serves the async **submit / poll / fetch** trio
  (`POST /video/submit` -> `{id, total_frames}`; `GET /video/status?id=` ->
  state/frames/bytes/elapsed; `GET /video/fetch?id=` -> upscaled mp4). One segment
  at a time (a concurrent submit returns 409); GPU work holds `_GPU_LOCK`. The
  per-segment upscale runs via the new **`UpscaleEngine.process_video`** (the same
  `inference_cli.process_single_file` streaming path the benchmark/RAM harnesses
  validated), reusing the cached DiT/VAE. **Heartbeat:** instead of literal
  per-chunk touches, a `_HeartbeatTee` over the pipeline's tqdm output refreshes
  the heartbeat on every line of progress — finer-grained than per-chunk AND it
  doubles as hang-detection (a stuck GPU stops emitting progress, so the heartbeat
  goes stale and the dead-man's switch reclaims the pod). Sibling to `full` / `tag`.
  Proven locally with a fake engine (full protocol + 409 + heartbeat + frame-count
  read) — see commit. **Still to wire (phase 4): the client half.**
- **`RemoteVideoSession`** (or extend `remote_run.RemoteSession`): **DONE** by
  extending `RemoteSession` with `mode="video"` (worker `--mode video`) — the same
  create -> push -> start worker -> arm dead-man's switch -> teardown lifecycle,
  handing back a **`RemoteVideoEngine`** (`scripts/remote_video_engine.py`) that
  subclasses `RemoteUpscaleEngine` for the proven tunnel/health/telemetry/close and
  adds `process_segment` (submit -> poll status -> fetch, with live `on_progress`).
- **GUI tab "Video Upscaler"** (`toolbox_gui.py`): the thumbnail/film-strip wall
  does not map to video, so show a **per-video segment-progress + queue + the cost
  estimator/gate** instead, plus the live remote telemetry row the upscale tab has.
  Reuses the Settings Region/DC + GPU pickers.
- **Video comparison window** (GUI, phase 5): a floating, resizable
  original-vs-upscaled **video** viewer, the video analogue of the image
  `ComparisonWindow` (0.2.9). Same shape: one shared instance, geometry persisted
  (`video_compare_geometry`), opened from the Video Upscaler tab's per-video
  context menu / a **Compare** action on a completed video (there is no thumbnail
  wall for video, so the entry point is the queue row, not a double-click on a
  strip). Both streams draw aligned on one canvas split by a vertical **before/after
  wipe** (left = source scaled up nearest-neighbour so its pixels line up with the
  upscaled grid, right = upscaled), with **shared** zoom (wheel, pointer-centred) and
  pan and a draggable divider, exactly like the image version, so the quality gain
  is directly visible. Key decisions that keep it dependency-light and correct:
    * **Align by TIMESTAMP, not frame index.** CFR-normalize can change the upscaled
      frame count (4835 -> 4923, section 14), so frame-index pairing would slowly
      mismatch; seeking both sides to the same time keeps the same content under the
      wipe.
    * **v1 is scrub + frame-step, not real-time playback** (a timeline slider +
      prev/next-frame, decode-on-seek). For judging upscale *quality* a held frame
      beats motion, and this mirrors how the image viewer decodes only when a gesture
      settles. Real-time synchronised playback is a later add.
    * **Decode frames on demand through the bundled ffmpeg**
      (`ffmpeg -ss <t> -i <file> -frames:v 1` to a pipe -> Pillow), so the GUI needs
      **no opencv and no new dependency** and reuses the same bundled-ffmpeg + Pillow
      stack as the rest of the app. Decode only the visible slice of each side (the
      upscaled side is the cost; the source is tiny), fast filter during gestures and
      a crisp pass when they settle, as the image viewer does.
- **`ffmpeg` dependency (settled, 6.4):** `bootstrap.ps1` downloads a pinned,
  nvenc-enabled ffmpeg in **both** install modes (Remote-only needs it too). All
  container work (probe/split/normalize/re-encode/reassemble/mux) runs locally; the
  pod stays SeedVR2-only. Local re-encode (only when a video needs it) uses nvenc if
  a CUDA GPU is present, else a CPU fallback. Ship ffmpeg's GPL license.

Reused unchanged from #1: pod lifecycle, network-volume + models, SSH/key
injection, GraphQL deploy + GPU picker, per-task price ceiling + fallback chain,
cost tracking, notifications, taskbar progress/flash.

## 12. Phasing

1. **Benchmark pass first** (section 7): real per-frame rates and good
   `batch_size`/`overlap`/`segment_seconds` defaults per (target x card) on a pod,
   before any UI. De-risks the cost estimator and the whole feature. **Harness
   built: `pod/bench_video.py`** — loads the engine once and drives its video core
   across a (target short-side x batch_size) grid, reporting per-frame time, peak
   VRAM, and the OOM ceiling to a CSV (scp it next to `upscale_engine.py`, run with
   the volume venv's python).
2. **ffmpeg split/reassemble/mux + drift detection** locally (no pod): prove the
   segment -> concat -> audio-mux -> drift-warn pipeline on already-upscaled or
   passthrough segments. **DONE — `scripts/video_pipeline.py`** (probe / plan /
   split / concat / mux / drift, CLI passthrough round-trip proof). See section 14
   for what the proof surfaced.
3. **Worker `--mode video` + submit/poll protocol + heartbeat-per-chunk.**
   **DONE** — `pod/worker.py` (async submit/status/fetch, one segment at a time)
   + `UpscaleEngine.process_video`. Heartbeat is progress-line-gated via a tqdm
   tee (finer than per-chunk + hang-detection), see section 11. Client half is
   phase 4.
4. **`batch_video_upscale.py`** wiring the `video_*` tables, per-segment streaming,
   resume, and the per-run cap. **DONE** — plus `RemoteVideoEngine`, `RemoteSession`
   `mode="video"`, and the `db.py` `video_roots/video_files/video_segments` tables.
   Proven end to end locally with `--passthrough` (no pod): installment cap stops
   cleanly mid-video, a resume run picks up at the first unfinished SEGMENT off the
   reused split, recursion/mirroring works, and the round trip is frame-perfect
   (4835->4835) with drift OK. See section 14.
5. **GUI "Video Upscaler" tab** with the cost estimator/gate and segment progress,
   plus the **original-vs-upscaled video comparison window** (the video analogue of
   the image `ComparisonWindow`; before/after wipe, shared zoom/pan, timestamp-aligned
   scrub + frame-step, frames decoded on demand via the bundled ffmpeg — see the
   build piece in section 11). **DONE** — `VideoTab` + `VideoComparisonWindow` in
   `toolbox_gui.py`, `video_estimate.py`, the worker's `frames_processed`, Settings
   -> Video, and `db.get_conn(check_same_thread=False)` for the GUI's worker
   threads. All logic tested headless (fake-app + temp-DB smoke tests); the
   **live running view is still un-exercised against a real pod** (a GUI run is the
   next validation).

**Reasonable v1 scope:** 1080p target only; fixed seed per video (6.2); plain
`-c copy` splitting with the sparse-GOP re-encode fallback (6.1); CFR normalize +
duration-drift detection/warning (6.3); file-level + segment-level resume; a
per-run minute/$ cap on by default. Lift the cap, add 1440p/4K, and consider
multi-pod parallelism and `prepend_frames` seam-blending once the cost UX and
reassembly are proven.

## 13. To verify before/while building

- **DONE on the RTX PRO 6000** (96 GB): per-frame rates + batch_size/VRAM curve +
  ceilings (see section 7 measured results). **Still needed: the same grid on the
  cards users actually rent** (RTX 5090 32 GB, RTX PRO 4500 32 GB) for the real cost
  calibration. A 32 GB card offloads DiT (resident=False), so its VRAM profile and
  per-frame both differ from the PRO 6000; the cost estimator must use *those*
  numbers, not the 96 GB ones.
- **Batch-size continuity A/B (perceptual, not yet measured).** Source confirms a
  larger `batch_size`/window improves temporal coherence (section 7 note), but the
  throughput grid ran `temporal_overlap=0` and judged nothing visually. Before locking
  the per-target defaults, render the same clip at a small window + overlap vs the
  largest the card fits and compare seam visibility / flicker, to choose `batch_size`,
  `temporal_overlap` and `segment_seconds` on quality, not just VRAM/throughput. This
  also sets whether 4K justifies a 96 GB+ card for the *window* (continuity), not only
  to fit at all.
- **Pod RAM must come from the cgroup, not `free`/host (measured trap).** A RunPod pod
  is a host-shared container: `free` reported 2015 GB on a 1-GPU H200 pod whose real cap
  (cgroup + RunPod listing) is **251 GB / 234 GiB**, 24 vCPU. The worker's window/RAM
  sizing and the estimator's fit check must read `/sys/fs/cgroup/memory.max` (v2) or
  `memory/memory.limit_in_bytes` (v1). **Fix `pod/worker.py` `/telemetry`** if it reports
  host RAM/CPU (it would overstate the pod ~8x and mislead the remote telemetry row).
- **Per-target window ceiling = min(VRAM, cgroup-RAM, time).** 4K VRAM plateaus (~128 GB
  H200 / ~135 GB B200, flat from ~bs9), so the GPU only OOMs at impractically large
  windows; the binding limit is pod RAM on small pods (~130 MB/frame held streaming at 4K,
  ~253 MB load-all) and otherwise wall-clock. Encode the per-target default `batch_size`
  from the chosen card's VRAM plateau AND its cgroup RAM, and gate 4K windows behind a
  RAM/time check rather than assuming VRAM is the limit.
- **ffmpeg build (source chosen: gyan.dev `release-essentials`, GPLv3, includes
  nvenc + libx264/libx265; latest verified 8.1.1 / 2026-05-04).** For
  `bootstrap.ps1`: download the **`.zip`** (native `Expand-Archive`; the `.7z` is
  ~3x smaller but needs a 7-Zip extractor the app doesn't ship) and verify the
  published `.sha256` sidecar. Pin a version via the packages URL
  (`builds/packages/ffmpeg-<ver>-essentials_build.zip`, retained one release back)
  or read `builds/release-version` and fetch the moving
  `builds/ffmpeg-release-essentials.zip`. Extract only **`ffmpeg.exe` + `ffprobe.exe`**
  (skip `ffplay`) into an `APP_ROOT`-resolved tools dir; ship the build's `LICENSE`.
- Confirm nvenc availability at runtime: NVIDIA GPU/driver present **and**
  `ffmpeg -hide_banner -encoders` lists `h264_nvenc`/`hevc_nvenc`; else the CPU
  libx265/libx264 fallback (6.4).

## 14. Phase 2 built: `scripts/video_pipeline.py` (local container pipeline)

The phase-2 deliverable (section 12.2) is built and proven on the real motivating
asset (`benchmark-videos/Pisici.AVI`, 320x240 mjpeg/AVI, pcm_mulaw audio). The
module is **stdlib + a bundled ffmpeg/ffprobe, no torch, never touches the
source**: `probe` / `count_frames` / `max_keyframe_gap` / `plan_split` /
`pick_encoder` / `split` / `concat_segments` / `mux_audio` / `check_drift`, plus a
CLI `passthrough_roundtrip` that splits then reassembles the *same* segments
(identity = "upscaled") and verifies a lossless round trip. Phase 3 swaps the
identity step for the real per-segment pod upscale; nothing else in the pipeline
changes. `find_ffmpeg` resolves `$IMGTBX_FFMPEG_DIR` -> `APP_ROOT/ffmpeg/bin`
(bootstrap target) -> PATH.

**Proven:** copy-path round trip is **frame-perfect** (4835 -> 4835 frames,
duration preserved) at both 60 s (3 segments) and 5 s (33 segments) granularity;
the forced-re-encode path runs end to end; the drift detector both **stays quiet**
on a clean copy and **fires** on a real timing change.

**What the proof surfaced (each now handled in code + folded into 6.3/6.4):**

- **The `nb_frames` stream HEADER is unreliable.** `Pisici.AVI` reports 4923 in
  the header but holds **4835** actual frames (packet count == decoded count == 4835;
  the 4923 is `30 fps x 164.1 s`, but the video stream is ~29.5 fps under an
  audio-driven 164.1 s container duration). **Drift detection must compare COUNTED
  frames, never the header** (`probe(count=True)` does one demux pass), or every such
  file raises a false 88-frame alarm. This is the single most important phase-2
  finding for correctness.
- **Audio codec vs mp4** and **pixel format vs NVENC**: the two `-c copy`
  exceptions now documented in 6.4 (audio -> AAC fallback; re-encode -> `yuv420p`).
- **CFR normalize legitimately shifts duration**, and the detector correctly warns:
  forcing this source to exactly 30 fps CFR padded 4835 -> 4923 frames (+0.098 s),
  because its real frame count doesn't fill the nominal `fps x duration` grid. Per
  6.3 the app **warns and keeps the result, never silently fixes** — working as
  intended. (Implication for the runner: prefer the `-c copy` path whenever
  possible; the re-encode path can nudge timing on sources whose own metadata is
  internally inconsistent.)

**Phase 4 built (the integration step):** `batch_video_upscale.py` ties the phase-2
local ffmpeg pipeline to the phase-3 worker. New pieces:
`scripts/remote_video_engine.py` (`RemoteVideoEngine.process_segment` = submit ->
poll -> fetch), `remote_run.RemoteSession` `mode="video"`, and `db.py`
`video_roots/video_files/video_segments` (resume at TWO granularities: a stopped
run resumes at the first unfinished SEGMENT). The runner is engine-injected, so a
built-in `PassthroughVideoEngine` (`--passthrough`) runs the whole orchestration
with no pod. **Proven locally:** the per-run installment cap stops cleanly
mid-video (not a failure); a resume run continues at the first pending segment off
the *reused* split (no re-split, no re-upscale of done segments); recursion +
output mirroring work; the round trip is frame-perfect (4835->4835) with drift OK;
"already done" short-circuits. Two bugs the test caught and fixed: (1) a non-ASCII
log glyph crashed Windows cp1252 stdout and masked the installment path — the
runner now reconfigures stdout to UTF-8 `errors="replace"`; (2) the Discord/Telegram
`fields` API wants list-of-dicts (`{"name","value"}`), not tuples.

**Real-pod end-to-end PASSED (RTX 5090, EU-RO-1, 2026-06-27).** A full
`Pisici.AVI -> 1080p` run on a rented 5090 ($0.99/h): worker came up in `video`
mode (354 s incl. model load), 3 segments streamed (submit/poll/fetch), each a real
SeedVR2 upscale (1778/1762/1295 frames at ~0.88-0.91 s/frame, matching the
benchmark), the **heartbeat held across each ~27-min segment** (dead-man's switch
never misfired), pod-reuse + worker version/mode reuse worked on the second
connect (no reload), and the pod **terminated cleanly** at the end. Output:
1440x1080, frame-perfect **4835 -> 4835** frames. ~72 min wall, ~$1.20.

**The run found a real bug — and the drift detector caught it.** The output had a
**2.9 s progressive A/V desync**. Cause: `Pisici.AVI` holds 4835 frames over a
164.1 s container = a REAL **29.46 fps**, but the AVI *tags* 30 fps (r == avg ==
30/1). SeedVR2's opencv writer trusts the tag and writes 30 fps -> 4835/30 =
161.17 s video, while the original 164.1 s audio is muxed unchanged -> they drift
2.9 s over the clip (the dangerous lip-sync case in 6.3). The phase-2 VFR check
missed it because it compares r vs avg frame-rate (both tagged 30), never the
COUNTED rate. **Fixed** (`plan_split`): also trigger CFR-normalize when
`counted_frames / duration` disagrees with the tagged fps by >0.5 %. CFR-normalize
pads 4835 -> 4923 frames = 164.2 s @ 30 fps, matching the audio: the desync drops
from **2.9 s (progressive) to 0.11 s (near-constant)**, and the detector honestly
notes the small residual (the CFR-rounding overshoot, output 164.20 s vs source
164.10 s). Validated locally via the round-trip; on a pod the segments are now true
30 fps CFR so SeedVR2 reproduces the same 164.2 s. **Possible future refinement:**
CFR-normalize to the *effective* fps (29.46) for an exact-duration match instead of
the ~0.1 s overshoot. The two other notes: the opencv backend writes **mpeg4**
(`mp4v`), not h264 — switch to the ffmpeg backend (x264/x265) for a better
deliverable once ffmpeg is confirmed on the pod; and the pod's worker re-runs the
SeedVR2 video path with no code change from #1.

**Still to build (phase 5):** the GUI "Video Upscaler" tab (cost estimator/gate +
per-video segment progress, consuming the `QUEUE`/`VIDEO`/`SEGMENT`/`VRESULT` GUI
events the runner already emits) and its `cost_confirm_usd` gate. The full UX spec
is section 15.

## 15. Phase 5: GUI "Video Upscaler" tab (UX spec)

Settled with the user before building. The tab is the front end of the existing
`batch_video_upscale.py` runner (launched as a subprocess, same stdin/stdout seam
as the other tools); it adds no pipeline logic.

### 15.1 Resolved decisions (the contract)

- **Remote-only.** No local path; the tab is blocked with guidance until remote is
  ready (15.3 step 1).
- **A persistent, DB-backed QUEUE is the core object.** "Prepare" adds a
  (source, target) job to the queue; Start processes the whole queue. The queue is
  exactly the set of `video_outputs` rows not yet `done`, so it **survives app
  restarts in full** (prepared-but-unstarted items too, not only partially-done
  ones): select 10 videos, start, close the app, reopen, continue, with nothing to
  re-select. This one mechanism gives the queue, restart-persistence, and
  segment-level resume.
- **One source can target multiple resolutions, but never in one action.** A clip
  already done at 1080p can be re-added targeting 4K. Outputs are keyed by
  (source, target); the output filename encodes the target.
- **v1 runs ONE pod per Start, GPU chosen for the queue's most-demanding target.**
  This is a deliberate FIRST-STEP simplification: a mixed 1080p+4K queue runs the
  1080p items on the big-VRAM card too (costlier than a 5090 would be), for one
  spin-up and simple orchestration. **v2:** group the queue by target and run a pod
  per group (cheapest per-target card each) at the cost of multiple spin-ups.
- **GPU list is sorted cheapest-total-cost-first** for the queue (15.7).
- **Two-tier scanning (15.4): never deep-probe or hash on scan.** Fast ffprobe
  metadata for the list + provisional eligibility; the expensive exact pass
  (counted frames, keyframe gap) and the content hash happen at Prepare / upscale
  time, only for the file being acted on.
- **Confirm-before-rent** is a Settings checkbox (default ON); Start shows the
  estimate and a louder warning above `cost_confirm_usd`.
- **Output subfolder `__upscaled__`** (configurable `video.output_subdir`), matching
  the image tab's baked-in GUI default and the `__Archive__` style.
- **Deliverable codec lives in Settings**, not the main flow (a non-technical user
  shouldn't have to care): the opencv backend writes mpeg4 today; an ffmpeg-backend
  h264/h265 option belongs in Settings -> Video (14).
- **Cell-specific double-click** in the list (15.5), so columns can grow their own
  actions later (e.g. double-click a resolution cell to filter).

### 15.2 Screen layout (top to bottom)

1. **Remote-readiness strip:** one line, "Remote ready" or what's missing
   (API key valid, SSH key present, network volume provisioned, region selected),
   with a link to Settings.
2. **Folder row:** Browse source folder + "Save upscaled to:" (auto-fills
   `<source>\__upscaled__`, mirrored tree), like the image tab.
3. **Scan list** (`ttk.Treeview`): columns = full path, resolution, duration
   (hh:mm:ss), codec, framerate, upscaled filename(s), upscaled resolution(s),
   status. Green path = has an upscaled counterpart. Double-click row = open in
   default player; double-click an upscaled-filename cell = ComparisonWindow;
   right-click = context menu. Sortable headers.
4. **Source + target row:** "Source File" textbox (filled from the selected row),
   a target combobox populated with that file's ELIGIBLE targets only, and
   **Prepare** (adds the (file, target) job to the queue; disabled if that
   (file, target) is already `done`).
5. **Queue list** (`ttk.Treeview`): the jobs to run, reorderable (move up/down) and
   removable. Persisted (15.1).
6. **Estimate status line:** GPU combobox (recommended cards, cheapest-first) plus
   read-outs for the WHOLE queue: estimated duration, estimated cost, total
   segments, average cost/segment. Changing the GPU refreshes them.
7. **Start Upscaling** + (during a run) a live progress view + **Stop** (15.8):
   a frames-based queue progress bar, current video/segment bars, ETA, accrued
   cost, live s/frame vs estimate, and the pod telemetry row.

### 15.3 The flow

1. **Readiness.** Remote-only, so the strip gates everything; a missing piece links
   to the relevant Settings field.
2. **Pick a folder.** Output auto-fills to the mirrored `__upscaled__` tree.
3. **Scan (fast).** Walk the tree, fast-probe each video (15.4), cache by
   (path, mtime, size), show the list with provisional per-target eligibility.
4. **Browse / inspect.** Double-click to play; green rows have an upscaled
   counterpart; double-click the upscaled cell to compare.
5. **Select + target + Prepare.** Selecting a row fills "Source File" and populates
   the target combobox with eligible targets. Prepare runs the EXACT pass for that
   (file, target) (counted frames, keyframe gap, segment plan, lazy content hash),
   then adds the job to the queue. Already-done (file, target) -> Prepare disabled.
6. **Build the queue.** Repeat 5 for as many files/targets as wanted; reorder/remove
   in the Queue list. The estimate line sums the queue.
7. **Pick the GPU.** The combobox lists cards deployable now in the volume's region
   that meet the queue's max-target VRAM floor, cheapest-total-cost first; selecting
   one refreshes the estimate.
8. **Start.** Confirm-before-rent (if enabled) shows the estimate; then one pod is
   rented and the queue streams. The live view shows the current video + segment
   X/Y, ETA, accrued cost, the pod telemetry row, and Stop (finish current segment,
   tear the pod down). Resume is automatic next Start (the queue is DB-backed).

### 15.4 Scanning: two tiers

- **Fast scan (every file, on folder open):** `ffprobe` stream metadata only
  (resolution, codec, framerate, duration, HEADER frame count). No demux, no decode,
  no hash. Cache the result in `video_files` keyed by (path, mtime, size) so an
  unchanged re-scan is instant. Enough for the list + provisional eligibility.
- **Exact pass (at Prepare, per acted-on file):** COUNTED frames (the header lies,
  14), keyframe gap (segment planning / sparse-GOP re-encode, 6.1), and the source
  content hash (lineage, lazily, only for files being upscaled). This is the data
  the segment plan and the cost estimate actually need, computed only when needed.

### 15.5 Eligibility

Per target, eligible if the source SHORT side is meaningfully below the target short
side, honouring the skip-cutoff: `min(w, h) < target_short * (1 - skip_cutoff/100)`
(1080p -> 1080, 1440p -> 1440, 4K -> 2160). A 320x240 clip is eligible for all three;
a 1920x1080 clip for 1440p/4K only; a >=4K clip for none (shown greyed, Prepare
disabled, "already >= 4K"). The combobox offers only eligible targets.

### 15.6 Schema (revises the phase-4 tables; cache.db is regenerable, nothing shipped)

Normalised so source properties are stored once and per-target job/resume state is
separate:

- **`video_files`** (source, PK root_id+rel_path): width, height, vcodec, acodec,
  fps, nb_frames (counted, filled at Prepare), duration, mtime, size (cache
  validation), src_hash (lazy). Eligibility is derived on read.
- **`video_outputs`** (per-target job, PK root_id+rel_path+target): status
  (`queued|splitting|streaming|partial|done|failed|skipped`), output_path,
  out_frames, queue_order (reorder; the queue = rows not `done`, ordered by it),
  created_at/updated_at. THIS is the durable queue + resume state.
- **`video_segments`** (PK root_id+rel_path+target+seg_index): in_frames, out_frames,
  status, seconds, output_path. Segment-level resume picks up the first non-`done`.

The phase-4 runner (which put target/output inline on `video_files` and keyed
segments without target) is updated to this in phase 5.

### 15.7 Cost estimator

- **Rate table:** seconds/frame per (target, GPU), seeded from the benchmark
  (section 7) and refined by the user's own history via `gpu_perf` with video task
  keys (`video-1080p` etc.) the way the image estimator already self-improves.
- **Per-target VRAM floor** (benchmark-derived, tunable): 1080p ~ a 32 GB offload
  card (5090) and up; 1440p ~ >= 80 GB resident (plateau 71-77 GB); 4K ~ >= 80 GB to
  fit at all (PRO 6000 bs5), big-VRAM (~140 GB) for a usable continuity window. The
  GPU list is filtered to the queue's MAX target floor.
- **Live availability:** `runpod_client.available_gpus(region, floor)` for cards
  deployable now with price/stock, intersected with the rate table, sorted by
  cheapest TOTAL queue cost.
- **Spin-up counted once per Start:** pod boot + worker model load (measured
  ~360 s worker-ready on a 5090) dominates short clips, so
  `total = ($/h) * (spin_up + sum_over_queue(frames * s_per_frame)) / 3600`.
  Per-video and per-segment read-outs are processing-only; the queue total adds the
  single spin-up.

### 15.8 Progress feedback (the running view)

Video upscaling is long (tens of minutes per segment), so the running view must
show continuous, honest progress, not just "working". Progress is measured in
**frames** (the natural, monotonic unit that correctly weights a long video over a
short one), at three levels:

- **Queue bar (primary):** `frames_done / total_queue_frames`, where the total is
  the sum of every queued segment's `in_frames` (known after Prepare). Shown with a
  %, an **ETA** (frames remaining x live s/frame) and **cost accrued** so far.
- **Current video:** "name -- segment X / Y" plus a per-video frames bar.
- **Current segment (the long wait):** a bar from frames processed WITHIN the
  segment. This is the piece that matters most here: without it a 3-segment video's
  bar sits still for ~27 min at a time.

**Within-segment frames need a small worker addition.** Today `/video/status`
returns `output_bytes` (a coarse liveness signal that already steps up per chunk).
Add **`frames_processed`**: the worker's `_HeartbeatTee` already sees the SeedVR2
tqdm progress, so it parses the latest `n/total` and exposes it (falling back to
`output_bytes` if a line can't be parsed). `RemoteVideoEngine` forwards it on the
`SEGMENT` event, already emitted every ~5 s poll, so the bar updates smoothly. The
zero-worker-change fallback is a **time-based** within-segment bar
(`elapsed / estimated_segment_seconds` from the rate): smooth but it drifts if the
rate estimate is off, so `frames_processed` is preferred.

**"Is it going well?"** Beyond the bar: show the **live seconds/frame next to the
estimate** so the user sees it tracking (a large divergence is the signal something
is wrong, the same idea as the image tool's degraded-GPU watchdog, #1), plus the
pod telemetry row (GPU / VRAM / temp) and accrued-vs-estimated cost. ETA and cost
both derive from the live s/frame (refined as the run proceeds) x frames remaining,
so they self-correct instead of trusting only the up-front estimate.

**Reuse the existing app chrome:** drive the Windows taskbar progress bar
(`taskbar_progress.py`, ITaskbarList3) from the queue %, as the image batch does;
flash the taskbar on completion (`App.flash_attention`); and send the
queue-complete / error notification (`notifications`). All already in the app.

#### Required improvement: the shipped bar is a time-based estimate, not frame-accurate (0.4.0)

**Status: shipped as a fallback, NOT good enough; a real fix is still required.**
In practice the worker's `frames_processed` almost never arrives: `_HeartbeatTee`
only accepts a tqdm `n/total` whose denominator matches the segment frame count, but
SeedVR2's bar counts **batches/chunks, not frames**, so the conservative match never
fires. The running view therefore relies on the **time-based** within-segment bar
(`elapsed / estimated_segment_seconds` from the rate table), capped at 97 % until the
segment's real `done` event.

This is honest about being an estimate, but it is **not reliable enough for a billed
pod**, because the rate table is optimistic and the estimate can be badly low. Live
example (2026-06-29, RTX PRO 6000 Blackwell **Workstation** Edition, 1440p, 760-frame
segment): up-front estimate **30:50**, but the run overshot it by **8+ minutes and was
still going**, so the bar sat **pinned at 97 % with the ETA exhausted (0:00)**. A
non-technical user reads "stuck at 97 %, past the ETA, on a pod I am paying for" as a
hung run and interrupts it, wasting the spend, the exact failure this view exists to
prevent.

Two things are needed (**validate against real worker output on a live pod, do not
guess**):

1. **Real within-segment frame progress (the proper fix).** Inspect SeedVR2's actual
   tqdm/stdout on a running pod and map its true unit (batches/chunks, accounting for
   `batch_size` / `chunk_size` / `temporal_overlap`) to frames in
   `_HeartbeatTee._scan_progress`, so `frames_processed` is reported reliably. A
   monotonic frame count is truthful regardless of rate-estimate error and removes the
   dependence on the estimate entirely.
2. **Make the time-based fallback degrade gracefully past the estimate.** When elapsed
   exceeds the estimate, do **not** freeze at 97 % with a 0:00 ETA. Keep proving the
   run is alive (continue an elapsed/"running NN:NN" counter, and say "running longer
   than estimated, the pod is still working" instead of showing a stalled bar). Also
   recalibrate the rate table from `db.gpu_perf` as runs finish (already recorded via
   `video_estimate.record_run`) so the up-front estimate self-corrects over time, and
   note the **Workstation vs Server** editions of a card can differ enough to matter.

**Aspect ratio / output megapixels (FIXED 0.4.0).** A second, separate cause of the
under-estimate: the benchmark `RATES` were all measured on a **4:3** clip
(`Pisici.AVI`, 320x240), so each target's output frame is 4:3 (1440p = 1920x1440 =
2.76 MP). SeedVR2's cost scales with **output pixels**, and the target is the SHORT
side, so a **16:9** video at "1440p" is 2560x1440 = **3.69 MP, ~33 % more per frame**
than the 4:3 benchmark, at every target. The estimator keyed only on the target
label, so it under-predicted widescreen by ~33 % regardless of GPU or self-calibration.
Note the *input* resolution barely matters: cost follows output size, which the aspect
ratio (not the source being small) sets. Fixed by normalising the rate to **seconds
per output-megapixel** (`video_estimate.seconds_per_mp`, `output_megapixels`,
`BENCH_OUT_MP`) and multiplying by each video's real output dimensions, so any aspect
ratio estimates correctly from the one 4:3 benchmark. `record_run` now accumulates in
**output megapixels** (task `video-mp-<target>`), so self-calibration is aspect-
independent too. Remaining: the static `RATES` are still 4:3-*measured* (just
converted, not re-measured on a 16:9 clip), which is fine because the conversion is
exact for the dominant DiT cost; re-measure only if VAE encode (which does scale with
input size) turns out to matter at the margin.

**Estimate was 3x optimistic on real footage (observed 0.4.0).** A first real run
(1080p -> 1440p, 760 frames, RTX PRO 6000 Workstation) took 5824 s = 7.66 s/frame =
~2.08 s/output-MP, vs the benchmark's 0.71 s/MP: the benchmark `RATES` were taken on a
tiny 320x240 synthetic clip and are simply too fast for real 1080p sources. This is not
a bug (self-calibration `record_run` stored the real timing, so the next 1440p estimate
on that card uses ~2.08), but the static table should be treated as a floor, not a
prediction, until a few real runs season `gpu_perf`. Enabling `temporal_overlap` (below)
also adds ~`overlap/(batch-overlap)` to the time, which self-calibration then absorbs.

#### Quality fixes from the first real run (0.4.0)

**Seams every `batch_size` frames = `temporal_overlap` defaulted to 0 (FIXED).** SeedVR2
denoises video in temporal batches of `batch_size`; with `temporal_overlap=0` each batch
is independent and hard-concatenated, so a visible "break" shows every `batch_size`
frames (with bs13 at 30 fps, ~3x/second - exactly what the first run produced). The
overlap was never set (not in config, not in the GUI), so it was 0 on every run.
`batch` steps by `batch_size - overlap` and the overlap region is blended
(`seedvr2/.../generation_phases.py:271, 973-995`). Fixed by defaulting
`video.temporal_overlap` to `DEFAULT_TEMPORAL_OVERLAP = 3` in
`batch_video_upscale.resolve_video_cfg` (an explicit config 0 still disables it).
SeedVR2 clamps `overlap >= batch` back to 0.

**Reassembled file unplayable on a network output drive (FIXED).** The first run wrote
to a mapped SMB drive (X:). Finalizing an mp4 needs a seek-back to write the `moov`
atom; over SMB that seek can be lost, leaving an unplayable "no codec shown" file - even
though the per-segment files (written by a plain sequential pod->local fetch, no seek)
play fine. Fixed in `process_job`: the concat + audio-mux now stage their mp4 writes in
a **local** temp dir, the result is probed (`nb_frames > 0` or raise), and only then are
the finished bytes `shutil.move`d to the output path (a plain copy, safe on any drive). A
single-segment job also skips the concat demuxer entirely (mux straight from the lone
segment).

**Deliverable codec defaulted to H.265 10-bit (FIXED).** The opencv `video_backend`
wrote segments (and the `-c copy` deliverable) as cv2 `mp4v` = MPEG-4 Part 2, a
low-quality 1990s codec that re-compresses the upscale and that Windows Explorer often
can't read metadata for. The default is now the **ffmpeg backend with H.265 10-bit**
(`FFMPEGVideoWriter`, libx265 CRF 12, yuv420p10le - less gradient banding), set in
`resolve_video_cfg` (`video_backend="ffmpeg"`, `use_10bit=True`) and pre-selected in the
Settings codec picker. Pick "Standard - MPEG-4" to fall back to opencv. The pod is
guaranteed ffmpeg three ways: `pod/provision.sh` caches a static build (libx264/libx265)
to `/workspace/ffmpeg`, `remote_run` adds that to the worker's PATH, and the worker
launch fetches it once if a volume predates this (a system ffmpeg in the image wins over
all of them). Re-provision an existing volume to cache ffmpeg ahead of the first run.
Local reassembly (`-c copy` concat + audio mux) handles x265 10-bit mp4 unchanged.

#### Auto-tuning: the user picks target + GPU, the pod picks the rest (0.4.0)

Goal: a user (and often the dev) shouldn't have to learn SeedVR2's knobs. `batch_size`,
`temporal_overlap`, `chunk_size` and `attention_mode` all default to **Auto** and are
resolved **on the pod**, the only place that knows the card's real VRAM.

- **batch_size (window)** — `pod/worker._auto_batch(out_w, out_h, vram_gb, resident)`
  inverts a fitted VRAM model `vram ~= out_megapixels * (A + B*batch)` (A=11.69,
  B=0.2746, from measured anchors: PRO 6000 96 GB 4K-4:3 bs5=81 GB; B200 180 GB 4K-16:9
  bs33=172 GB; the 1440p ~75 GB plateau checks out) to the largest 4n+1 that fits
  `vram * 0.80`, capped at 33 (continuity flattens past there; throughput past ~9) and
  floored at 5. Picks ~5 at 4K on 96 GB, ~17 at 4K on a B200, 33 at 1440p on 96 GB.
- **temporal_overlap** — `_auto_overlap(batch)` = ~1/6 of the window, clamped `[2,
  batch-1]` (SeedVR2 silently resets overlap >= batch to 0).
- **chunk_size** — ~90 frames rounded to whole batches (RAM-bound, no quality effect).
- **attention_mode** — `UpscaleEngine._resolve_attention("auto")` reads SeedVR2's
  `compatibility.SAGE_ATTN_*_AVAILABLE` / `FLASH_ATTN_*_AVAILABLE` flags and picks the
  fastest installed kernel (sageattn_3 > sageattn_2 > flash_attn_3 > flash_attn_2 >
  sdpa). Applies to the image upscaler too.

**OOM auto-recovery is what makes Auto trustworthy** (`_run_video_job`): a CUDA OOM
retries the segment at the next-smaller 4n+1 (down to the floor, `empty_cache` between),
so an optimistic guess self-corrects on the pod (a reload, not a redeploy) instead of
failing the run. It also defuses the degraded-GPU/OOM failure mode for video.

The runner passes the AUTO sentinels through (`batch_size 0`, `temporal_overlap -1`,
`chunk_size 0`); the worker reports the resolved values in `/video/status`
(`resolved_batch`/`resolved_overlap`) and the runner logs them once ("pod resolved:
…"). Settings → Video shows batch/overlap under an **"Advanced (leave on Auto)"** group;
explicit values still override (a power-user escape hatch), and an explicit overlap is
clamped below the batch on the pod.

### 15.9 Settings additions (Settings -> Video)

`confirm_before_rent` (default true), `output_subdir` (default `__upscaled__`), the
deliverable codec/quality (opencv mpeg4 vs ffmpeg h264/h265), plus the existing
`video` knobs (section 9). Region/DC + GPU pickers and the price ceilings are reused
from Remote (#1).

### 15.10 Deferred to v2 (documented, not built)

Per-target pod grouping for mixed-target queues (15.1); the h264/h265 deliverable if
not done in v1 (14); real-time synchronised playback in the comparison window (it is
scrub + frame-step in v1, section 11).
