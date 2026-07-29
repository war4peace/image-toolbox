# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

Every open milestone is now medium or larger: two new input/processing capabilities (#19 RAW
and DNG input, #20 a Video Stabilization tab), one measurement-gated one (#21 denoising), a
Video Upscaler feature (#12 mixed local+remote queue) and a remote-side one blocked on funds
rather than design (#15 a second GPU provider). Two lower-priority ones each introduce a new
process model, networking, or packaging (HTTP interface #3, Unraid #4). The **shipped**
milestones are kept below as a numbering legend, after the open work.

---

## Contents

- [19. RAW and DNG input for the Batch Upscaler](#19-raw-and-dng-input-for-the-batch-upscaler-medium)
- [20. Video Stabilization (new tab)](#20-video-stabilization-new-tab-medium)
- [21. Denoising before upscaling](#21-denoising-before-upscaling-medium-gated-on-a-measurement)
- [12. Local+remote mixed queue](#12-localremote-mixed-queue-medium)
- [15. Second remote GPU provider (packet.ai)](#15-second-remote-gpu-provider-packetai-medium)
- [3. HTTP interface](#3-http-interface-hard-low-priority)
- [4. Unraid Community Apps integration](#4-unraid-community-apps-integration-hardest-low-priority)
- [Sequencing & dependencies](#sequencing--dependencies)
- [Shipped milestones (numbering legend)](#shipped-milestones-numbering-legend)
- [Decided against / constraints](#decided-against--constraints)

---

## 19. RAW and DNG input for the Batch Upscaler: Medium

Accept RAW files as Batch Upscaler input, rendering each to a JPEG that the existing pipeline
then upscales. Researched and scoped 2026-07-28; the decisions below are settled, and one
measurement is outstanding.

**Scope: the app renders RAW, it does not develop it.** A RAW is sensor data plus
instructions, so turning it into a picture means choosing a demosaic, white balance, tone
curve and colour space. Those choices *are* the photo, and this app has never had a rendering
opinion. A raw developing UI (exposure/WB/curve controls) is **out of scope permanently**.

### Settled decisions

| # | Decision | Note |
|---|---|---|
| 1 | **Use the camera's embedded full-size JPEG preview** when there is one ("S2"), and fall back to a LibRaw demosaic with fixed defaults ("S1") when there is not | The preview is the manufacturer's own rendering, i.e. exactly what the camera would have written in JPEG mode, and it carries the camera's EXIF intact. No opinion required from us, and it is faster |
| 2 | **Use S2 only when the preview is essentially the full image** (within ~2% of `sizes.iwidth`/`iheight`) | Makes S2 and S1 dimension-identical **by construction**, which is what lets the scan read sizes from the header alone. See "the trap" below |
| 3 | **Output is `.jpg`** | The app exists to make old photos display natively on monitors and TVs, not to produce a mastering format. Revisit `.tif` only on repeated demand |
| 4 | **`rawpy` ships in all three install modes** | 0.87 MB Windows wheel (cp312), depends only on `numpy` (already present), torch-free. The decode happens locally even for a remote run, so the pod never needs to know RAW exists |
| 5 | **Ship the LibRaw licence text** | rawpy is MIT, bundled LibRaw is LGPL-2.1. `bootstrap.ps1` already does exactly this for the GPL ffmpeg build (`ffmpeg\LICENSE.txt`) |
| 6 | **Support the common set**: `.dng .cr2 .cr3 .nef .arw .orf .rw2 .raf .pef .srw` | LibRaw covers all of them; the marginal cost is the extension list |
| 7 | **Tag & Rename ignores RAW entirely**, tagging and renaming both | Tagging means writing `ImageDescription` **into** the file, and writing into a proprietary RAW container is exactly the source mutation the app forbids. Its `IMAGE_EXTS` already excludes them, so this costs nothing to honour |
| 8 | **Conciliation never archives or deletes a RAW original.** It folds the render in **alongside**, as `<name>_upscaled.jpg` | Ends with three files: `IMG_1234.CR2`, `IMG_1234.JPG` (the camera's own), `IMG_1234_upscaled.jpg`. Lowercase `.jpg` per the app's own convention, regardless of the sibling's case |
| 9 | **Key that exception on the superset test, not on "is it RAW"** | The rule is "replace only when the processed file is a superset of the original". One test, two outcomes, no format list to maintain |

### The trap that makes this dangerous if rushed

`runner_common.get_image_dimensions()` reads the **first IFD** of a TIFF, and Pillow sniffs
content rather than extension. A DNG (and CR2/NEF/ARW, all TIFF/EP derivatives) puts a
**small preview in IFD 0** by spec. Reproduced with a two-IFD TIFF named `.dng` whose real
image is 6000x4000:

```
Pillow opens .dng as: TIFF   size (256, 171)
get_image_dimensions ->  (256, 171)
```

So adding `.dng` to `IMAGE_EXTS` and changing nothing else would make the scan see a 256x171
image, judge it far below the target, upscale **the thumbnail**, and write a 4K file built
from a 256 px preview. It would not fail. It would produce plausible garbage in bulk,
silently.

**The rule that dissolves it: measure the pixels you are actually going to upscale.**
Concretely:

1. `get_image_dimensions()` gains a RAW branch taken **before** the TIFF branch.
2. **A RAW extension must never fall through to Pillow.** This inverts the module's usual
   fail-safe instinct: here the fallback answers *confidently and wrongly*, so an unreadable
   RAW returns `(0, 0)` and is reported unreadable, exactly like a corrupt JPEG.
3. Because of decision 2 above, the scan reads `sizes.iwidth`/`iheight` (header parse, no
   thumb unpack, no demosaic) and is correct whichever path the run later takes.

`sizes.flip` comes from the same header parse, so a RAW's **upright** dimensions are known
without decoding anything, which is what lets the expensive decode sit after the skip check.

### The prepare pipeline (shared with #21)

RAW decode, auto-straighten and (if #21 ships) denoise all want the same thing: produce a
working copy, point the upscaler at it, clean up. Today `_make_straightened_copy` owns that
pattern privately. The agreed shape is one chain:

> **RAW decode -> auto-straighten -> denoise -> upscale**, source never touched.

- **The order is deliberate.** Denoise-then-straighten would also work on pixels (a 90 degree
  rotation is lossless), but it would feed the orientation CNN an input distribution it was
  never validated against, and "only confident calls act" is what makes that CNN safe.
- **Stages pass arrays, not files.** Three file hops with JPEG temps would put a RAW through
  **three lossy generations** before SeedVR2 sees a pixel, which is a self-inflicted version
  of the degradation the app exists to undo. Hold one in-memory array; write **exactly one**
  temp at the end. The honest cost: `orientation.analyse()` takes a **path** today and needs
  an array-accepting variant. If that refactor is declined, use lossless PNG/TIFF temps
  instead: more disk and time, no quality loss. **JPEG temps must not be used**, and that is
  the easy accident because `.jpg` is what the output is.
- **The chain runs AFTER the skip decision.** A 24 MP body is 6000x4000, wider than the 4K
  target, so most modern RAW files will be skipped; decoding first would spend the most
  expensive step on files that never upscale. Free to get right, because dimensions and
  orientation both come from the header.
- **Delete the temp in a `finally`, not on success.** Otherwise a folder that fails leaks one
  temp per image, and those are the largest files the pipeline produces.

### Consequences elsewhere

- **Output naming:** `run_pass` builds `out_name = f"{stem}{ext}"`, keeping the source
  extension, and `_save_image` lets Pillow infer the format from it. `photo.CR2` is a Pillow
  "unknown file extension" error on every file, so the extension mapping is not optional.
- **Conciliation's fold-back is a NEW mode** for a tool that until now only replaced. It needs
  its own preview count ("added alongside: N"), a real collision check (Windows is
  case-insensitive, so a pre-existing `_upscaled.jpg` and `.JPG` are one file), and
  idempotence so a second run cannot produce `_upscaled_upscaled.jpg`.
- **The added file re-enters the app's input space.** It lands in the source tree as a `.jpg`,
  so later scans see it. It will be above target and skipped, and Tag & Rename *should* see
  it (per decision 7, the render is what gets tagged). Both fine, both worth knowing.
- **The decode strategy is a METADATA decision, not just an imaging one.** Shipped #13 reads
  the source's block with Pillow's `getexif()`, which handles JPEG, WebP, PNG and TIFF and
  **not** a proprietary RAW container; `rawpy` is a LibRaw *imaging* wrapper that exposes
  pixels and sizes, not an EXIF library. So under **S2** (extract the camera's embedded
  full-size JPEG preview) #13 works unchanged, because the "source" it reads from is that
  preview and it carries the camera's EXIF intact. Under **S1** (demosaic the sensor data)
  there is nothing for it to read from, and the metadata has to come from somewhere else
  (`exifread`, or piexif against the TIFF IFD of a DNG/CR2). **S2 makes the metadata free;
  S1 makes it a second piece of work.**
- **#13's orientation trap has a RAW-specific shape.** #13 forces the written `Orientation`
  to 1 on the assertion "the pipeline already applied it" (`ImageOps.exif_transpose` in
  `_load_image`, plus auto-straighten). A RAW carries no EXIF `Orientation` for
  `exif_transpose` to consume: it carries LibRaw's own `flip` flag, which `postprocess()`
  applies by default. The outcome is the same (the pixels come out upright, so 1 is still
  correct) but nothing in today's code path is what makes it so, and the assertion has to be
  re-established rather than inherited.
- **#13b needs no RAW carve-out, by construction.** Conciliation may never archive or delete
  a RAW original (decision 8 above folds the render in ALONGSIDE it), so a RAW never becomes
  a conciliated pair and the backfill never sees one.
- **Most modern RAW files will be skipped as already-above-target**, so this feature mainly
  serves **old** RAW: 2003-2010, 6-8 MP early DSLRs. That is the same demographic the app
  already serves, but it is worth saying out loud because it changes who it is for.

### Outstanding before building

**One measurement:** how often is a full-size embedded preview actually present? It decides
how much work S2 does versus S1. It needs real files from several manufacturers, skewed to
2003-2010 bodies, from the CC0 corpus at <https://raw.pixls.us/>. The same run verifies the
IFD-0 trap against real camera files rather than the proxy reproduction above. See
`docs/manual-todos.md` item 2 (untracked).

**Risks:** medium. The failure mode is silent (Trap 1) rather than loud, which is exactly the
kind this project treats seriously, and the Conciliation carve-out must ship in the same
version or the first user to conciliate a RAW folder loses their negatives.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 20. Video Stabilization (new tab): Medium

Stabilise shaky old footage, as an **independent feature with its own tab**, not as a stage
of the Video Upscaler. Researched and measured 2026-07-28.

### Shape

- **Name: "Video Stabilization".** Tab position: **after Conciliation.** The existing order
  deliberately groups the three GPU tools together, and this is not one of them.
- **Not a batch tool.** It takes **one video**: prompt for the input file, prompt for the
  output path and filename. One job at a time.
- **All other tabs are locked while a stabilization runs**, matching the run-exclusivity
  model 0.5.2 established.
- **No GPU, no pod, no remote mode.** Architecturally it is a sibling of **Conciliation**
  (local file work) rather than of the Video Upscaler: no VRAM sizing, no batch tuning, no
  benchmark corpus, no funds guard, no degraded-GPU watchdog.
- **It composes by folder/file, not by pipeline.** Stabilise a video, then feed the result to
  the Video Upscaler. The ordering that matters (stabilise **before** upscaling, so the crop
  happens at source resolution and the box-fit target still fills the frame) is preserved by
  the user's own sequencing.

### Why it is a separate feature, and not a Video Upscaler option

`vidstab` is a **two-pass global** algorithm: pass 1 measures camera motion across the
**whole file**, pass 2 smooths that trajectory and warps each frame. The Video Upscaler
splits into ~60 s segments and processes them independently. Run per segment and every
segment boundary gets a **visible jolt**, because each stretch is smoothed toward its own
mean. Forcing it into that pipeline would have meant a full-length temp file per video, an
extra serial pass, and time that is **not resumable at segment granularity**. Outside the
pipeline none of that arises: whole-file is simply its natural shape.

### Settled decisions

| # | Decision | Why |
|---|---|---|
| 1 | **`vidstab` (two-pass), not `deshake`** | `deshake`'s only advantage was that it survives a segment split, and moving out of the pipeline removed the constraint it was solving. `deshake` is measurably weaker (block-matching, cannot see slow drift). `deshake_opencl` rejected outright: a runtime-optional GPU dependency for a marginal step |
| 2 | **Off by default, opted into per video** | Its failure mode is **silent and permanent**: content leaves the frame and nothing in the output says so. And shakiness is not a defect the way interlacing is: a handheld pan is how the footage was shot |
| 3 | **Never auto-detected** | Measured: the *unmodified* camcorder clip already scores a 9.64% correction, so a detector would fire on nearly everything |
| 4 | **Coverage over steadiness. Default `optzoom=0` + `crop=keep`, and a conservative `smoothing`** | Old digital footage is amateur footage from small cameras. Forcing steadiness risks removing a lot of real content, and content at the edge of frame is often the reason the clip is treasured |

### The measurements that produced decision 4

1200x900 clips from a real camcorder source, `vidstabdetect shakiness=8 accuracy=15`, then
`vidstabtransform optzoom=1` (**the ffmpeg default everyone copies**). The figure is
libvidstab's own reported `Final zoom`:

| Clip | `smoothing` | Final zoom | Frame area kept |
|---|---:|---:|---:|
| No added shake (source's own handheld motion only) | 30 | **9.64%** | ~83% |
| + mild added shake | 30 | 10.05% | ~83% |
| + moderate added shake | 30 | 11.22% | ~80% |
| + severe added shake | 30 | 12.75% | **~79%** |
| No added shake | **10** | **4.34%** | ~92% |

- **The default discards about a fifth of the picture.** Not a sliver.
- **The floor is high and barely moves with shake**, because `optzoom=1` picks one **static**
  zoom that must cover the **worst frame in the whole clip**. A single jolt in a ten-minute
  video sets the crop for all ten minutes.
- **`smoothing` is the real lever**: 9.64% at 30 versus 4.34% at 10, on the same clip. It is
  a direct steadiness-versus-coverage trade, which is what decision 4 resolves.

The alternatives, both of which preserve the whole frame:

| Setting | What the edges do | Loses content? |
|---|---|---|
| `optzoom=1` (ffmpeg default) | zoomed until no border is ever visible | **Yes**, ~10-13% per dimension |
| **`optzoom=0` + `crop=keep`** (chosen) | border pixels filled in from **previous frames** | **No.** Worst case the extreme edge looks slightly stale for a few frames |
| `optzoom=0` + `crop=black` | black bars that move with the correction | No, but it looks broken |

### Cost and speed

**Measured** at 1080p: pass 1 runs at 3.8x realtime, pass 2 at 2.4x, so a full stabilise is
about **0.7x the clip duration** for both passes. Negligible next to anything GPU-bound.

`libvidstab` is present in **both** ffmpeg builds `bootstrap.ps1` uses, so there is no new
dependency and no build change: BtbN's win64-gpl includes `scripts.d/50-vidstab.sh`, and
gyan's release-essentials lists `libvidstab`. Worth a runtime capability check anyway
(`ffmpeg -filters`) so a user with a hand-installed ffmpeg gets a clear message rather than a
cryptic failure.

### Implementation notes

- **The transform-file path is a real trap** and the error message is useless.
  `vidstabdetect=result=<path>` and `vidstabtransform=input=<path>` take a **file path inside
  a filter argument**, where `:` is the option separator and `\` is an escape. An absolute
  Windows path (`C:\Users\...\t.trf`) fails with a bare
  `Error opening output files: Invalid argument`, naming neither the filter nor the path.
  Reproduced: the identical command with the working directory set to that folder and a bare
  relative filename works immediately. `os.chdir` is not acceptable in a threaded GUI
  process, so either escape the drive colon (`C\:/Users/.../t.trf`, forward slashes) or keep
  the `.trf` in a temp dir and pass a bare filename with the child process's `cwd` set to it.
  **Write a unit test for whichever is chosen**, because nothing about the failure points at
  the cause.
- **Output codec:** this output *feeds* the upscaler, so it is not a throwaway intermediate
  and should be encoded well. Reuse `vp.pick_encoder()` plus the 10-bit
  `fixed_ratio_engine._delivery_pix_fmt` rule (`hevc_nvenc` -> `p010le`, `libx265` ->
  `yuv420p10le`, else 8-bit).
- **Audio** is muxed back from the source unchanged, following the Video Upscaler's existing
  `-c copy` audio path and its mp4-friendly-codec exception.
- **Progress** is per pass (two passes, each with a frame count), which is simpler than the
  Video Upscaler's segment model. No resume needed: a single file finishes in well under its
  own duration, so Stop simply discards.

### MQTT and notifications

- **A new `task/name` value is a CONTRACT change, not an implementation detail.** The values
  are matched by the shipped Home Assistant automations. Adding "stabilizing" requires
  updating **`docs/mqtt-integration.md`** (the single source of truth for every topic and
  payload key) **and** `samples/home-assistant/`, in the same change.
- The rest comes free from the `MqttTaskState` mixin: `task/details`, `task/runtime`,
  `task/progress`, `task/eta`, the `last_run` summary, and the non-retained
  `event/run_started` / `event/run_finished` pair.
- **Notifications:** use `send_notification` with a `notifications.COLOR_*` **constant**,
  never a raw int. `tests/test_notification_severity.py` fails the build on a raw colour
  literal in a runner, which is the guard that exists because the Video Upscaler once shipped
  a palette matching no entry and degraded quietly.
- Taskbar progress, the taskbar attention flash on completion, and the telemetry row all come
  from `ToolTab` unchanged. The telemetry row will show CPU/RAM activity and an idle GPU,
  which is correct and worth not "fixing".

**Risks:** low. It writes one new file to a user-chosen path, never touches the source, needs
no GPU and no network, and its worst failure is a badly cropped output that the user can
simply redo with stabilisation off.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 21. Denoising before upscaling: Medium (gated on a measurement)

Optionally denoise a source before it reaches the model, as a **checkbox** in the Batch
Upscaler (images) and the Video Upscaler (videos).

> **Do not build this before the A/B harness reports.** Unlike everything else on this list,
> the *value* here is unknown rather than the cost. SeedVR2 is already a restoration model
> trained on degraded inputs, so denoising first may add nothing, or may remove detail the
> model would have used as evidence. See `docs/manual-todos.md` item 1 (untracked). If the
> answer is "no visible benefit", this milestone moves to `dropped-ideas.md` and nothing is
> built.

### Settled decisions (conditional on the measurement)

| # | Decision | Why |
|---|---|---|
| 1 | **Denoise BEFORE the model, not after** | After the model, the noise is no longer noise: SeedVR2 reads it as evidence of texture and reconstructs **plausible structure** from it at 4x scale, correlated and edge-consistent. A denoiser then has nothing to key on and can only blur everything uniformly. Cost also scales with output pixels (4-16x more), and the pre-split `-vf` seam already exists |
| 2 | **A checkbox in both upscalers, not a tab** | The seams already exist: for images it is the third stage of #19's prepare pipeline, for video it is a `denoise` flag on `SplitPlan` appending to the same `-vf` chain that already carries `bwdif`. A tab is a whole new surface for an unproven feature |
| 3 | **One implementation, at most two entry points** | Two independently-tuned filter chains spelled the same way will drift. A shared module with a checkbox calling into it is fine |
| 4 | **Fixed conservative `hqdn3d`, no strength UI** | Over-denoising an old tape removes the grain **and** the detail, and the model then invents something else entirely. A conservative default is the honest v1; expose a knob only if the measurement shows people need to tune it |
| 5 | **`nlmeans` is refused outright** | Measured at **0.06x realtime** (79 s for 125 frames of 1080p), i.e. 16x the clip duration, to feed a model that will re-invent the detail anyway |

### Why stabilization (#20) gets a tab and this does not

The distinction is technical, not aesthetic:

| | **Stabilise (#20)** | **Denoise (this)** |
|---|---|---|
| Temporal scope | **Global.** Needs the whole file; per-segment jolts at every boundary | **Local.** A few frames of window, so segment boundaries are a non-issue |
| Fits as a pipeline stage? | **No.** That is the whole finding | **Yes**, into a re-encode that already runs |
| Destructive side effect | **Yes**, ~10-21% of the frame, invisible in the output | None: a filter, reversible by re-running without it |
| Needs per-item review? | **Yes**, hence the per-video lever | No, a conservative default is honest |

### Measured filter costs (1080p, 125 frames, decode + filter + null sink)

| Filter | fps | vs realtime |
|---|---:|---:|
| `removegrain=1` | 266 | 10.7x |
| `atadenoise` (temporal) | 224 | 9.0x |
| **`hqdn3d`** (spatial + temporal) | 199 | **8.0x** |
| `fftdnoiz` | 106 | 4.3x |
| `bm3d` (basic) | 12 | 0.5x |
| `vaguedenoiser` | 22 | 0.9x |
| `nlmeans` | **1.6** | **0.06x** |

Images, CPU, `cv2.fastNlMeansDenoisingColored`: 0.34 s at 0.8 MP, 0.51 s at 3.9 MP, 2.09 s at
12 MP. Negligible against a SeedVR2 upscale either way.

### Things that must be decided as part of building it

- **Turning denoise on forces a re-encode of a video that would otherwise stream-copy**,
  converting a free lossless split into a full transcode whose intermediate is
  `yuv420p` 8-bit. Irrelevant for a noisy VHS capture, but it should be stated rather than
  discovered.
- **Remote-only installs have no `cv2`** (the Remote bootstrap installs pillow, piexif,
  paho-mqtt, python-vlc, matplotlib, certifi). **Decision: serve `cv2` from the RunPod
  network volume**, the same way the volume already caches the Ollama runtime and the SeedVR2
  weights, rather than adding ~40 MB to the Remote bootstrap for a feature most remote users
  may not enable. `provision.sh` is the place; it already does incremental,
  self-pruning provisioning, so this is an addition to an existing mechanism.
- **Three unrelated problems hide under one word**, and they will not have one answer:

  | Problem | What it actually is | Right tool |
  |---|---|---|
  | Sensor noise (old digicam, high ISO) | random per-pixel noise | a denoiser. SeedVR2 may already handle it |
  | JPEG compression artifacts | structured, not random | a deblocker, or nothing |
  | Scan defects: dust, scratches, mould | sparse localised damage | **inpainting**, not denoising |

  The third is what people actually complain about with old photo collections, and no
  denoiser touches it. The A/B set is deliberately built to separate these three.

### The separate experiment worth running at the same time

A mild **temporal** filter applied **after** upscaling would act on the model's *own*
instability rather than on the source's noise. SeedVR2's documented temporal jitter of fine
detail on slow pans (the 4x causal temporal VAE, `docs/video-upscaler.md`) is exactly what a
filter like `atadenoise` is built to suppress, and no pre-pass can touch it because it does
not exist yet at that point. Different feature, different target, same test clips.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 12. Local+remote mixed queue: Medium
Let a single Video Upscaler queue run some jobs on local GPU(s) AND others on
rented RunPod pods in one Start, instead of the whole run being local **or**
remote.

- **Today's constraint:** the "Run on" switch is one mode for the entire run
  (`_start` branches to `_start_local` for the whole queue, or the remote
  single-/multi-pod path). Per-item GPU binding only distinguishes among
  **remote** cards; a local job stores no GPU (there is one implicit local card).
  As of 0.5.7 the selector is **locked while the queue is non-empty**, so a queue
  can't be half-built in one mode and switched, which is the correct interim
  behaviour until mixing exists.
- **Foundation already in place:** the `(engine, gpu)` queue grouping
  (`job_group_key` / `group_queue_order` / `distinct_group_keys`), the multi-pod
  orchestrator `_start_grouped` (one runner per group), the GPU picker combobox,
  and the per-item GPU column (which now renders the local card as
  "Local <name>", 0.5.7).
- **Work needed:** (a) a local GPU **identity** scheme so a job can bind a
  specific local card (e.g. `local:0` / `local:1` from `nvidia-smi -L`), not just
  an implicit single GPU; (b) let the GPU picker offer local card(s) as bindable
  options alongside live remote cards; (c) a launcher that dispatches **local
  groups to the in-process/subprocess local engine and remote groups to pods,
  concurrently** (the current grouped path is remote-only and serial); (d)
  per-source telemetry rows + estimates that already exist, wired per group; (e)
  scope the funds guard / confirm-before-rent to the **remote** groups only.
- **Clean stepping stone:** **multiple local GPUs within Local mode** alone
  (bind + run local groups on several local cards) is a smaller, self-contained
  first step that exercises (a)+(b)+(c-local) without any remote concurrency.
  Rare on consumer hardware but real (e.g. a multi-card workstation).
- **Risks:** concurrent orchestration of heterogeneous runners (a local
  in-process engine holding the GPU + N remote pods) is more moving parts than
  the current pendulum; a degrading local card (the watchdog) must not stall the
  remote groups; VRAM feasibility is per-card.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 15. Second remote GPU provider (packet.ai): Medium
Let a remote run rent its GPU from a provider other than RunPod, starting with
[packet.ai](https://packet.ai/), behind a thin provider interface.

> **Blocked on funds, not on design.** The three unknowns below can only be
> answered by signing up and running one real deploy/terminate cycle, and vetting
> the cards costs billed GPU time. See `docs/packet-ai-secondary-gpu.md` for the
> full evaluation (2026-07-14).

- **Why a second provider:** price, stock and region coverage. The app already
  refuses to substitute a GPU type the user did not pick (0.4.0), so when a card
  is sold out in the chosen region the run simply fails and the user re-picks.
  A second catalog is the honest fix for that, and packet.ai's sample pricing
  (RTX 4090 ~$0.39/h, L40S ~$0.92/h, A100 80 GB ~$1.43/h) undercuts RunPod on
  several cards. Its catalog includes the **RTX 6000 Pro 96 GB** already
  benchmarked for video.
- **Why packet.ai and not vast.ai:** vast.ai was investigated 2026-06-23 and
  rejected on billing shape, not on principle: metered bandwidth **both ways**
  (~$40/TB) directly taxes the stream-every-image design, storage is ~5x RunPod's,
  and it has no region-wide network volume. See `docs/dropped-ideas.md`. That
  entry's vetting checklist is the standard packet.ai has to clear: (a) free or
  cheap ingress+egress, (b) cheap region-wide persistent storage that mounts on
  disposable instances, (c) reliable SSH with key injection. On advertised
  behaviour packet.ai clears all three; none is confirmed.
- **Gate before any code (from the evaluation note):** (1) is there a documented
  customer REST API, or is programmatic use CLI-only? (2) can a volume be created
  once and reattached to new pods via API, and is it region-locked? (3) is stock
  on the needed cards reliable, given it is a much smaller provider? Each answer
  changes the interface shape, so the ~15-minute account + `packet gpus --json` +
  one launch/terminate cycle comes first.
- **The known integration risk:** RunPod's GraphQL schema is inspectable
  anonymously, which is how `runpod_client.py` was built at all. packet.ai's API
  reference is login-gated (`dash.packet.ai/docs` returns 403) and the real
  orchestration API underneath is hosted.ai's provider-side REST, which may not be
  fully exposed to customers. So `packet_client.py` may have to **shell out to the
  `packet` CLI** rather than talk HTTP, which is a different seam (subprocess,
  parsing `--json`, a binary to locate) than `runpod_client.py`'s.
- **Work needed:** (a) a provider interface covering what `remote_run` actually
  uses (list GPUs with live price/stock, deploy with an injected public key and a
  mounted volume, inspect, terminate, account balance); (b) `packet_client.py`
  behind it, HTTP or CLI-backed; (c) a provider selector in the GUI plus
  per-provider credentials in `config_store.SECRET_FIELDS`; (d) provisioning the
  model volume a second time on the new provider (`provision.sh` is portable, the
  volume lifecycle is not); (e) the funds guard reading a second balance API.
- **The largest lift is the GUI, not the client.** Provider choice touches the
  RunPod tab, the per-tab GPU pickers, the cost estimator's rate tables, the
  benchmark corpus keys (a card's rate is per provider once prices differ), and
  every "is this remote" branch. Scope it deliberately: a first version that
  supports packet.ai for **video only** (one tab, one flow) is far cheaper than
  making every remote path provider-aware at once.
- **Risks:** a second provider doubles the surface that can break silently at a
  distance (stock, pricing, API drift) on a vendor stack one layer deeper than
  RunPod's. The dead-man's switch, worker, streaming engine and resume logic are
  all provider-agnostic already, so the blast radius is the control plane only.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 3. HTTP interface: Hard (low priority)
Spin up a small HTTP server with a UI that mirrors the application UI.

- **What "mirror" implies:** rebuilding the thumbnail wall, two-row live status,
  progress/ETA, pause/resume/stop, and Settings as a web app, plus a backend
  and live updates (WebSocket/SSE).
- **Reuse:** the subprocess + stdin/stdout protocol is a clean backend seam; a
  server can drive the same scripts the GUI does.
- **Work needed:** an HTTP server (stdlib `http.server` is too thin for this,
  so realistically a small framework), a streaming channel for live
  progress/thumbnails, and a full second UI to maintain alongside the tkinter
  one.
- **Risks:** large, ongoing surface area (two UIs to keep in sync); auth/binding
  concerns if exposed beyond localhost.
- **Scope note:** a minimal "status + start/stop" web panel is far cheaper than
  a true mirror and worth considering first.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

## 4. Unraid Community Apps integration: Hardest (low priority)
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now: there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port, not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#3) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Sequencing & dependencies

- **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18 and #22 are complete** (remote
  upscaling + funds-floor; RunPod video; video conciliation; self-healing remote runs; local
  video; benchmark sharing; telemetry usage graphs; Home Assistant dashboard samples;
  Real-ESRGAN engine; metadata copy + backfill; the comparison lens; derived-directory
  pruning; skipping image variants the pipeline cannot round-trip; Conciliation Undo; browsing
  already-upscaled images), so the remaining sequencing is only among the open milestones below.
- **Open milestones: #19, #20, #21, #12, #15, #3, #4.**
- **#19 (RAW) inherits #17's superset rule**: an original may only be replaced when the
  processed file is a superset of it. #17 satisfies that rule by never producing a
  non-superset; #19 has to keep satisfying it for a format the pipeline *does* process.
- **#19 (RAW) is gated on one measurement**, not on other features: how often a full-size
  embedded preview exists. It shares its prepare-pipeline design with #21, so whichever
  lands first builds the chain and the other slots a stage into it.
- **#21 (denoise) is gated on the A/B harness and may never be built at all.** It is the only
  open milestone whose *value* is unknown rather than its cost. Do not start it before the
  measurement; a "no visible benefit" result moves it to `dropped-ideas.md`, which is a
  successful outcome.
- **#20 (Video Stabilization) has no dependencies** and does not touch the Video Upscaler's
  pipeline at all: separate tab, separate run, composes by file. Its one cross-cutting cost
  is the new MQTT `task/name` value, which is a **contract change** requiring
  `docs/mqtt-integration.md` and `samples/home-assistant/` to be updated in the same change.
- **#12 (mixed local+remote queue)** is a medium, self-contained Video Upscaler feature that
  builds on the shipped `(engine, gpu)` grouping; #3 and #4 are lower priority and larger,
  each introducing a new process model, networking, or packaging. With Home Assistant already
  done over MQTT, the old telemetry coupling no longer drives sequencing.
- **#15 is gated by spend, not by other features.** It needs a paid account and
  billed GPU time to answer three questions no public page answers, so its
  ordering is set by when that spend happens, not by #12. Nothing else
  depends on it, and it does not depend on anything else. Note the overlap with
  #12: both add a dimension to "where does this job run", so whichever lands
  second inherits the other's grouping/selector work (a job would then carry
  engine + provider + GPU).
- **#12 has a clean stepping stone** (multiple local GPUs within Local mode)
  that can land first without any remote-concurrency work.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-on from the shipped #6/#7:** generalise the Auto-resume supervisor from
  video to the image runners (batch upscale / tag) (not yet scheduled).
- **Follow-on from the shipped #8 (not yet scheduled): extend benchmark sharing
  to the IMAGE tasks.** Today the crowdsourced corpus covers `db.video_bench`
  only; per-card image throughput (`db.gpu_perf` for batch upscale and tag) is
  still served solely by the author-maintained `docs/image-benchmarks.csv`, so a user
  picking a remote GPU for an image run gets the author's numbers or nothing.
  The transport, CSV format, local-precedence import and maintainer merge tool
  are all reusable as-is; the work is deciding the shared row's identity for a
  task whose unit is an image, not a (target x compile x tile) cell, and keeping
  it out of the accumulating `gpu_perf` store on import (see
  `docs/dropped-ideas.md`). See `docs/benchmark-sharing.md`.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #3
  and #4 each push toward extra packages, a long-running server, and
  cross-platform support, so adopt those deliberately.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Shipped milestones (numbering legend)

Roadmap **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18 and #22** are
done and live. **This section is a pointer list, not a record.** Each entry says what the
number meant and where the design of record actually lives; nothing is described in full
here. The numbers survive because code and other docs cite the roadmap by them (`remote
#1`, `Video Upscaler #2`, `local #7`), so deleting the entries outright would strand those
references.

When a milestone ships, its rationale moves to the document that owns the feature and the
entry here shrinks to one of these lines. That rule is the point of the section: a design
kept in two places drifts, and the stale copy is the one that gets read.

- **#1: Remote upscaling (RunPod).** Shipped 0.3.1-0.4.2. The Batch Upscaler and Tag &
  Rename on a rented pod: disposable pod, resident streaming worker, dead-man's switch.
  See `CLAUDE.md` (Remote upscaling) and `docs/runpod-notes.md`.
- **#2: Video upscaling (experimental).** Shipped 0.4.x. The Video Upscaler:
  probe / split / stream / reassemble on a rented pod, with segment-level resume.
  See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`.
- **#5: Video conciliation.** Shipped 0.5.1. Conciliation matches and replaces VIDEO
  originals alongside images, by content-hash lineage only (no name fallback, so a partial
  clip can never be taken for a whole-video match). See `CLAUDE.md` (Conciliation) and
  `conciliate.py`.
- **#6: Self-healing remote runs.** Shipped 0.5.0, video only. An opt-in Auto-resume
  supervisor survives losing the pod mid-run: reconnect a blip, or wait for the identical
  card and redeploy. See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`
  section 17.
- **#7: Local video upscaling.** Shipped 0.5.0. The same SeedVR2 video work in-process on
  the user's own GPU, with a predictive VRAM sizer, a per-card benchmark and optional
  `torch.compile`. See `docs/local-video-upscaler.md`.
- **#8: Benchmark sharing.** Shipped 0.5.1. The per-card video benchmark as a crowdsourced
  corpus: pulled from GitHub at launch, contributed back through a pre-filled issue, curated
  by a maintainer `--merge` tool. See `CLAUDE.md` (Benchmark sharing) and
  `docs/benchmark-sharing.md`.
- **#9: Telemetry usage graphs.** Shipped 0.5.3. A per-run usage-graph window behind each
  telemetry row, one shared instance per source. See `CLAUDE.md` (Telemetry usage graphs)
  and `docs/telemetry-design.md`.
- **#10: Home Assistant dashboard samples.** Shipped 0.5.3. Ready-made Lovelace dashboards
  over the MQTT topics the app already published; docs and samples only, no pipeline change.
  See `samples/home-assistant/` and `docs/mqtt-integration.md`.
- **#11: Real-ESRGAN engine.** Shipped 0.5.6. A second video engine (a fixed-ratio 2X/4X
  GAN) local and remote, plus the general queue change it rides on: per-item GPU binding +
  grouped multi-pod Start. See `CLAUDE.md` (Real-ESRGAN engine cluster),
  `docs/video-upscaler.md` section 18 and `docs/local-video-upscaler.md` section 23.
- **#13: Copy metadata from the original.** Shipped 0.5.9. 13a writes the source's metadata
  onto the upscaled file wherever it is written; 13b backfills the already-upscaled backlog
  inside Conciliation, at the last moment both files exist. See `CLAUDE.md` (Metadata
  carried across) and `tests/test_exif_copy.py`.
- **#14: Hover magnifier ("lens view").** Shipped 0.6.0. Both comparison windows magnify
  the patch under the pointer as original AND upscaled side by side, at the real upscale
  ratio, with a wheel-zoomed and pinnable lens. See `CLAUDE.md` (Comparison) and
  `tests/test_lens_view.py`.
- **#16: Derived directories must not be re-scanned as input.** Shipped 0.5.9. One shared
  name rule prunes the app's own output folders (`__upscaled__`, `__Archive__`,
  `.imgtbx_video`) from every input walk. See `CLAUDE.md` (Derived-directory pruning) and
  `tests/test_derived_dirs.py`.
- **#17: Skip image variants the pipeline cannot round-trip.** Shipped 0.5.9. Transparency,
  several pages and 16-bit depth are detected from the header and skipped with a named
  reason, in the Batch Upscaler and in Conciliation (which checks the ORIGINAL, so the
  protection is retroactive). See `CLAUDE.md` (Image variants left as-is) and
  `tests/test_image_variants.py`.
- **#18: Conciliation Undo.** Shipped 0.5.9. Every file action is journalled before it
  happens, and an archive run can be reversed from that journal; a delete run is refused
  rather than attempted. See `CLAUDE.md` (Conciliation Undo) and
  `tests/test_conciliate_undo.py`.
- **#22: Browse already-upscaled images.** Shipped 0.6.0. A **Browse upscaled…** window
  pairs an output tree back to its originals long after the run ended, by inverting the
  upscaler's own mirror. See `CLAUDE.md` (Browse upscaled) and
  `tests/test_browse_upscaled.py`.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, the deferred local-engine install, parallel jobs (an image tool
alongside the Video Upscaler), the automatic-telemetry half of benchmark
sharing, UI localization, a light/dark theme, background removal, and the
standing constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
