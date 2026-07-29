# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

The remaining open milestones start with an easy comfort feature deferred to later (a
hover magnifier, #14).
The medium tier is
two new input/processing capabilities (#19 RAW and DNG input, #20 a Video Stabilization
tab), one measurement-gated one (#21 denoising), a Video Upscaler feature (#12 mixed
local+remote queue) and a remote-side one blocked on funds rather than design (#15 a second
GPU provider). Two lower-priority ones each introduce a new process model, networking, or
packaging (HTTP interface #3, Unraid #4). The **shipped** milestones are kept below as a
numbering legend, after the open work.

---

## Contents

- [14. Hover magnifier ("lens view") in the comparison window](#14-hover-magnifier-lens-view-in-the-comparison-window-easy-later)
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

## 14. Hover magnifier ("lens view") in the comparison window: Easy (later)
Add a hover-driven magnifier to `ComparisonWindow` that shows one patch of the
image as original **and** upscaled at the same time, side by side, alongside the
existing before/after wipe.

> **Scheduled for later.** A comfort feature, not a gap in capability: the
> comparison window already compares the two images perfectly well. Pick it up
> when the higher-value work is done.

- **Where the idea comes from:** Upscayl has this, and reading its
  implementation (`renderer/components/main-content/lens-view.tsx`) is worth it,
  because the marketing name hides what it does. It shows the **original**
  full-frame with a crosshair cursor and a 48 px square outline tracking the
  mouse, and pops up **two 192 px panels side by side** under the cursor,
  labelled *Original* and *Upscayl AI*, both magnifying that same spot at a
  hard-coded **4x**. Both panels are sampled against the *original's* natural
  dimensions times 4, so on a 4x upscale the right-hand panel lands on the
  upscaled file's true 1:1 pixels while the left-hand one shows the original
  interpolated to match. Hover-driven and transient: no zoom control, no panning,
  no click to freeze, and it vanishes when the pointer leaves the image.
- **What is actually missing here, precisely:** not zoom. `ComparisonWindow` is
  the stronger zoom by every measure already (continuous wheel zoom centred on
  the pointer, drag-pan, up to 400% of the upscaled image's native pixels via
  `ABS_MAX`, a crisp LANCZOS pass once the gesture settles, both sides locked to
  the same region so they cannot drift apart). What is missing is
  **simultaneity**: a wipe shows any given patch as *either* original *or*
  upscaled and you slide the divider to swap, whereas a lens shows the same patch
  **twice at once**. The eye compares two things next to each other instead of
  remembering what was there a moment ago, which is a real perceptual difference
  on fine detail (exactly the detail SeedVR2 either recovers or invents).
- **The hard half is already built:** the window decodes an arbitrary region of
  either image at an arbitrary scale (Pillow `resize` with a float `box`, used
  for the visible slice today). A lens is a second pair of those calls at a fixed
  scale, drawn into two small canvas areas, plus mouse tracking.
- **Design decisions to make:**
  * **Fixed zoom or follow the window's zoom?** Upscayl hard-codes 4x. Deriving
    it from the actual upscale ratio (so the upscaled panel is always native 1:1)
    is more honest and is what makes the comparison meaningful.
  * **Hover-transient or click-to-pin?** Transient matches Upscayl and needs no
    UI. Pinning suits inspecting one spot while changing zoom, and suits a
    screenshot.
  * **Does it coexist with the wipe or replace it?** Upscayl treats lens and
    slider as two separate view modes. A toggle button is the cheaper answer than
    trying to run both gestures on one canvas at once.
  * **Video too?** `VideoComparisonWindow` subclasses the same base, so a lens
    would come along nearly free on the still-frame video compare. Worth
    confirming it does not fight the frame-stepping controls.
- **Risks:** low, and contained. It is a view-only feature in one GUI module: it
  reads pixels, writes nothing, and cannot touch a file. The only real concern is
  redraw cost on a large image while the pointer moves, which the existing
  fast-filter-then-LANCZOS pattern already solves.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

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

- **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #16, #17 and #18 are complete** (remote upscaling +
  funds-floor; RunPod video; video conciliation; self-healing remote runs; local video;
  benchmark sharing; telemetry usage graphs; Home Assistant dashboard samples; Real-ESRGAN
  engine; metadata copy + backfill; derived-directory pruning; skipping image variants the
  pipeline cannot round-trip; Conciliation Undo), so the remaining sequencing is only among
  the low-priority open milestones below.
- **Open milestones: #14, #19, #20, #21, #12, #15, #3, #4.**
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
- **#14 is deliberately parked.** It is easy and self-contained (one GUI module,
  view-only, cannot touch a file), but it is comfort rather than capability: the
  comparison window already does the job. It has no dependencies, so it can be
  picked up whenever there is appetite for a small, low-risk piece of work.
- **#15 is gated by spend, not by other features.** It needs a paid account and
  billed GPU time to answer three questions no public page answers, so its
  ordering is set by when that spend happens, not by #14/#12. Nothing else
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

Roadmap **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #16, #17 and #18** are done and live; they are no
longer described in full here (their design of record lives in `CLAUDE.md`,
`docs/runpod-notes.md`, `docs/video-upscaler.md`, `docs/local-video-upscaler.md`,
`docs/benchmark-sharing.md`, `docs/telemetry-design.md` and `samples/home-assistant/`).
The numbers survive only because code and other docs cite the roadmap by them
(`remote #1`, `Video Upscaler #2`, `local #7`):

- **#1: Remote upscaling (RunPod).** Shipped 0.3.1–0.4.2. See `CLAUDE.md` +
  `docs/runpod-notes.md`.
- **#2: Video upscaling (RunPod-only, experimental).** Shipped. See
  `docs/video-upscaler.md`.
- **#5: Video conciliation.** Shipped 0.5.1-experimental: Conciliation now
  matches and replaces VIDEO originals with their upscaled outputs, alongside
  images, in one scan. Videos match by the content-hash `lineage` the Video
  Upscaler records on completion (item 10) ONLY: no name fallback, so a partial
  clip (which records no lineage) can never be mistaken for a whole-video match;
  a video is acted on only when its output is present in the chosen processed
  tree. See `CLAUDE.md` (Conciliation) and `conciliate.py`.
- **#6: Self-healing remote runs (auto-recover a lost pod).** Shipped 0.5.0
  (video only): an opt-in "Auto-resume" supervisor reconnects a blipped pod, or
  waits unbounded for the identical card and redeploys it, continuing from the
  first unfinished segment. Funds guard / user Stop / completed queue are the only
  non-redeploy stops. See `docs/video-upscaler.md` section 17.
- **#7: Local video upscaling (free-and-slow alternative to remote).** Shipped
  0.5.0: the same SeedVR2 video work runs in-process on the user's own GPU via
  `LocalVideoEngine`, with a predictive VRAM sizer, a one-click per-card benchmark,
  and optional `torch.compile`. See `docs/local-video-upscaler.md`.
- **#8: Benchmark sharing (community download / contribute).** Shipped 0.5.1: the
  per-card video benchmark becomes a crowdsourced corpus, auto-downloaded from GitHub
  at launch and contributed back via a browser-delegated GitHub issue (multi-GPU,
  deduped against the published set); a maintainer `--merge` tool curates submissions.
  See `CLAUDE.md` (Benchmark sharing) and `docs/benchmark-sharing.md`.
- **#9: Telemetry usage graphs.** Shipped 0.5.3: clicking a telemetry row opens a
  per-run usage-graph window (embedded matplotlib, four capacity-pinned stacked
  charts, a dynamic/global range-toggle bar, a blitted crosshair), one shared
  instance per source (the local machine, or a tab's remote pod). Lazy + fail-safe:
  absent matplotlib disables only the graph, not the row or MQTT. See `CLAUDE.md`
  (Telemetry usage graphs) and `docs/telemetry-design.md`.
- **#10: Home Assistant dashboard samples.** Shipped 0.5.3: ready-made Lovelace
  dashboards under `samples/home-assistant/` (a no-HACS core dashboard + a
  Mushroom/ApexCharts one, plus the MQTT sensor + derived-percent template YAML)
  that render the app's existing `image-toolbox/*` MQTT telemetry live. Docs/samples
  only, no pipeline change. See `samples/home-assistant/`.
- **#11: Real-ESRGAN engine (fixed-ratio 2X/4X alternative to SeedVR2).** Shipped
  0.5.6: a second video upscaling engine (a GAN: fast, VRAM-light, deterministic)
  dropping into the same engine seam, local (`FixedRatioVideoEngine`) and remote (a
  volume-free esrgan pod, `pod/worker.py --mode esrgan`, models self-downloaded). Two
  tiers (Compact / Quality), native-scale only. It required a general Video Upscaler
  change that mixed-GPU SeedVR2 queues benefit from too: **per-item GPU binding +
  grouped multi-pod Start** (each job carries its engine + picked card; the queue
  groups by (engine, GPU) and runs one pod per group, re-grouping mid-run). The
  Benchmark GPU window + estimator treat ESRGAN as a distinct method (single s/frame
  + peak-VRAM probe per cell, a separate rate namespace). See `CLAUDE.md` (Real-ESRGAN
  engine cluster), `docs/local-video-upscaler.md` §23 and `docs/video-upscaler.md` §18.
- **#13: Copy metadata from the original.** Shipped 0.5.9-experimental, the third
  correctness fix of that branch. `upscale_engine._save_image` wrote the result with a bare
  `img.save(...)`, so **every upscaled image lost all metadata**: capture date, camera, lens,
  exposure, GPS, copyright. After Conciliation replaced the original, the capture date was
  gone for good. Both halves shipped together. **13a** reads the source's block and writes it
  onto the output (`exif_copy.exif_for_upscaled`), wherever the file is written - the same
  engine runs on a rented pod, so `exif_copy.py` is pushed with it. **13b** repairs the
  already-upscaled backlog inside Conciliation, at the one moment the app holds both files
  already matched and immediately before the original stops being available; Scan/Preview
  only counts it. Pillow does the reading and writing for every format (no piexif on the read
  side); piexif appears only in 13b's JPEG path, because `insert` patches the APP1 segment and
  leaves the scan data byte-identical. Three corrections are not optional: Orientation is
  normalised (the pipeline already applied it, so copying it verbatim rotates an upright photo
  twice), the stale embedded thumbnail is dropped, and a TIFF source's structural tags are
  stripped before they can describe a strip layout inside a JPEG. One Settings checkbox
  (`upscale.copy_metadata`, default on) governs both halves. See `CLAUDE.md` (Metadata carried
  across) and `tests/test_exif_copy.py`.
- **#16: Derived directories must not be re-scanned as input.** Shipped
  0.5.9-experimental, a correctness fix rather than a feature. The app writes its outputs
  inside the tree it scans (`<source>/__upscaled__`, `<source>/__Archive__`), and only
  Conciliation pruned its own archive: after an archive-mode run the Batch Upscaler found
  every archived original (the only copies still BELOW the target, therefore all eligible)
  and re-upscaled them, Tag & Rename re-tagged them, the Video Upscaler re-queued them —
  billed GPU time on a rented pod. Fixed by a shared **name** rule
  (`runner_common.DerivedPruner`, `DERIVED_DIRNAMES`) called from all four walkers plus
  Conciliation's processed-hash index; it is stateless, free per file and **retroactive**
  (a DB record would not survive a second install, a deleted `cache.db`, or an archive made
  before the fix). It prunes SUBdirectories only, so pointing a tool AT an `__upscaled__` /
  `__Archive__` folder as the chosen root still scans it. Nesting itself was deliberately
  NOT "fixed": a shared sibling output root would collide the moment a second source tree
  is processed. See `CLAUDE.md` (Derived-directory pruning) and
  `tests/test_derived_dirs.py`.
- **#17: Skip image variants the pipeline cannot round-trip.** Shipped 0.5.9-experimental,
  the second correctness fix of that branch. The upscale engine is RGB-only end to end
  (`convert("RGB")` in, `arr[..., :3]` as `mode="RGB"` out, frame 0 only), so a transparent
  PNG/WebP, a multi-page TIFF and a 16-bit TIFF all came out flattened **under the same name
  and extension** - after which Conciliation's mirrored-name fallback matched with full
  confidence and reported an ordinary "replaced", archiving or DELETING the only copy that
  still had the alpha / pages / bit depth. The decision was to **skip** them, not to support
  them (that half, with its revisit trigger, is in `dropped-ideas.md`): they are detected
  from the header (`runner_common.image_variant_reason`, no decode, and not even attempted
  for JPEG), classified with a specific plain-English reason ("would lose transparency"),
  counted separately and **named** in the log by both the Batch Upscaler and Conciliation.
  Conciliation checks the ORIGINAL before either matching path, so it also protects a tree
  upscaled before the fix. See `CLAUDE.md` (Image variants left as-is) and
  `tests/test_image_variants.py`.
- **#18: Conciliation Undo.** Shipped 0.5.9-experimental. Conciliation was the app's only
  destructive tool and the only one with no undo record: the `__Archive__` folder was the
  sole evidence a run had happened, and a Delete run left not even that. A Run now journals
  one row per file action (`db.conc_runs` / `conc_actions`) **before** performing it, and an
  **Undo last run** button on the tab reverses an archive run: each processed file returns to
  the processed tree, each original comes back out of `__Archive__`. Four decisions carry the
  feature. **Undo reads the disk, not the row's status**, so an interrupted run (a `pending`
  row, one of the two moves done) unwinds correctly and a repeated undo is a no-op. **It
  never overwrites**: both halves of a pair are checked before either moves, so a refusal
  leaves the pair exactly as it was; a file changed since the run, or a name something else
  now occupies, is a reported conflict. **A delete run is refused, not attempted** - the
  bytes are gone and no journal changes that, so the button stays disabled and says why,
  while the journal is spent on the question a user actually asks after a bad delete run
  ("what exactly did it remove?"). And **recording is free and fail-safe**: the fingerprint
  is (size, mtime) plus the content hash only when one is already memoised (`db.cached_hash`,
  which never reads a file), because recording happens on every run while verifying is the
  rare recovery path and can afford the read; a journal failure disables the journal, reports
  itself once, and lets the conciliation finish. See `CLAUDE.md` (Conciliation Undo) and
  `tests/test_conciliate_undo.py`.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, the deferred local-engine install, parallel jobs (an image tool
alongside the Video Upscaler), the automatic-telemetry half of benchmark
sharing, UI localization, a light/dark theme, background removal, and the
standing constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
