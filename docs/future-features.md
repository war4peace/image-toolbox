# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

The remaining open milestones are an easy Batch Upscaler gap (copy the original's
metadata into the upscaled image, #13), an easy comfort feature deferred to later
(a hover magnifier in the comparison window, #14), a medium Video Upscaler feature
(a mixed local+remote queue, #12), a medium remote-side one blocked on funds
rather than design (a second GPU provider, #15) and two lower-priority ones that
each introduce a new process model, networking, or packaging (HTTP interface #3,
Unraid #4). The **shipped** milestones are kept below as a numbering legend,
after the open work.

---

## Contents

- [13. Copy metadata from the original (Batch Upscaler + Conciliation)](#13-copy-metadata-from-the-original-batch-upscaler--conciliation-easy)
- [14. Hover magnifier ("lens view") in the comparison window](#14-hover-magnifier-lens-view-in-the-comparison-window-easy-later)
- [12. Local+remote mixed queue](#12-localremote-mixed-queue-medium)
- [15. Second remote GPU provider (packet.ai)](#15-second-remote-gpu-provider-packetai-medium)
- [3. HTTP interface](#3-http-interface-hard-low-priority)
- [4. Unraid Community Apps integration](#4-unraid-community-apps-integration-hardest-low-priority)
- [Sequencing & dependencies](#sequencing--dependencies)
- [Shipped milestones (numbering legend)](#shipped-milestones-numbering-legend)
- [Decided against / constraints](#decided-against--constraints)

---

## 13. Copy metadata from the original (Batch Upscaler + Conciliation): Easy
Carry the source photo's EXIF (and where the format allows, IPTC/XMP) into the
upscaled output, instead of writing a metadata-free image. Two parts: **13a**
fixes the upscaler so new output keeps its metadata, and **13b** recovers the
metadata for images already upscaled before the fix, at conciliation time.

### 13a. Write the metadata at upscale time

- **Today's behaviour:** `upscale_engine._save_image` saves the result tensor
  with a bare `img.save(...)` (jpeg q95 / webp q95 / PNG), passing no `exif=`,
  so **every upscaled image loses all metadata**: capture date, camera make and
  model, lens, exposure, GPS, copyright, any existing description or rating.
  For a tool whose stated purpose is reviving a personal photo collection, losing
  DateTimeOriginal is the painful one: the upscaled copy sorts by file date, and
  after Conciliation replaces the original, the capture date is gone for good.
  This is a genuine gap against Upscayl, which has a "copy metadata from
  original" toggle (see `docs/upscayl-vs-image-toolbox.md` section 3.4).
- **Work needed:** read the source's EXIF once (piexif is already a dependency,
  used by `tag_and_rename.py`), then write it onto the output in `_save_image`.
  JPEG and WebP take an `exif=` bytes argument; PNG needs the value stashed as a
  text chunk or dropped, so the honest scope is "JPEG and WebP fully, PNG
  best-effort".
- **The one correctness trap (must not be skipped):** the pipeline **already
  applied** the orientation. `_load_image` runs `ImageOps.exif_transpose`, and
  auto-straighten may have rotated a temp copy on top of that, so the output
  pixels are upright. Copying the source's `Orientation` tag verbatim would make
  every viewer rotate an already-upright image a second time. The copied block
  must have **Orientation forced to 1** (and the thumbnail sub-IFD dropped or
  regenerated, since the embedded thumbnail is stale and still the old size).
- **Also needs deciding:** whether the pixel-describing tags that are now wrong
  (`ExifImageWidth` / `ExifImageHeight`, and any `PixelXDimension` equivalents)
  are corrected to the upscaled size or stripped. Stripping is safer than lying.
- **Interaction with Tag & Rename:** it writes `ImageDescription` into whatever
  EXIF the file has. Today it has to create a block from nothing on an upscaled
  file; once this ships it edits a real one, which is strictly better, but the
  order of operations (upscale then tag) must keep the description the tagger
  wrote rather than the source's older one.
- **Should it be a toggle?** Upscayl makes it optional. Copying metadata is what
  a user expects by default, so the recommendation is **on by default**, with a
  Settings checkbox for anyone who deliberately wants scrubbed output (sharing
  photos without GPS is the real use case for off).
- **Risks:** low. A malformed or oversized EXIF block from an old camera must not
  fail the save, so the copy has to be wrapped and fall back to writing the image
  with no metadata (the current behaviour), matching the app's fail-safe rule.

### 13b. Retroactive backfill during Conciliation

Fixing the upscaler only helps images upscaled **from then on**. Anyone who has
already upscaled a large collection but has not conciliated it yet is sitting on
a pile of metadata-free outputs, and their originals are still on disk, so the
information is not lost yet. Conciliation is the one moment where the app holds
**both** files, already matched to each other, immediately before the original
stops being available. That makes it the right place (and in Delete mode, the
last possible place) to recover the gap.

- **What it does:** when conciliating an image pair, compare the original's EXIF
  against the processed file's and copy across every field the processed file is
  **missing**, leaving every field it already has untouched.
- **"Copy what is missing, keep what is present" is the whole policy**, and it is
  deliberately one rule rather than a per-field table. Any field the processed
  file already carries got there because something downstream set it on purpose:
  the pipeline's normalised `Orientation` (the pixels are upright, see the trap
  above), the `ImageDescription` Tag & Rename wrote, corrected or stripped pixel
  dimensions. Never overwriting means the backfill can never undo the work of the
  tool that ran before it, and the rule needs no maintenance when a later feature
  starts writing some new tag.
- **Still exclude outright:** the source's `Orientation` and its embedded
  thumbnail sub-IFD. On a metadata-free upscale both are "missing", so the
  general rule would happily copy them, and both would then be wrong (a second
  rotation, and a stale thumbnail at the old size).
- **Where:** `conciliate._move_processed_in` is the seam. Do it while the
  original is still in place, before the archive move or the delete.
- **Preview must show it:** Scan/Preview touches nothing today and must keep that
  promise, so it reports a count (for example "metadata restored: N") alongside
  the existing replaced / no-match / kept counts, and only **Run** writes.
- **Idempotent by construction:** re-running Conciliation over already-backfilled
  files finds nothing missing and does nothing. The same holds once the upscaler
  side ships: those outputs already carry their metadata, so the backfill quietly
  becomes a no-op and only the older backlog is touched.
- **Dependency note:** `conciliate.py` is currently pure file I/O with no Pillow
  or piexif import, which is part of why it is fast and cheap to run. The backfill
  changes that, so the import should be guarded and the feature degrade to
  skipping cleanly if it is unavailable, rather than becoming a hard requirement
  of the whole tool.
- **Fail-safe, emphatically:** Conciliation is the one destructive tool in the
  app. A metadata copy that raises must never abort the move, leave a file in two
  places, or block the archive/delete. It is a bonus pass: log the failure, carry
  on with the file operation.
- **Out of scope:** videos. Container-level metadata is an ffmpeg job with its own
  rules, and video pairs are lineage-matched only; keep this to images.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

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

- **#1, #2, #5, #6, #7, #8, #9, #10 and #11 are complete** (remote upscaling + funds-floor;
  RunPod video; video conciliation; self-healing remote runs; local video; benchmark
  sharing; telemetry usage graphs; Home Assistant dashboard samples; Real-ESRGAN engine),
  so the remaining sequencing is only among the low-priority open milestones below.
- **Open milestones: #13, #14, #12, #15, #3, #4.** #13 (copy metadata) is the cheapest and
  has no dependencies: it touches one function in `upscale_engine.py` plus a
  Settings checkbox, and it closes a real data-loss gap, so it is the natural
  next pick. #12 (mixed local+remote queue) is a medium, self-contained Video
  Upscaler feature that builds on the shipped `(engine, gpu)` grouping; #3 and #4
  are lower priority and larger, each introducing a new process model,
  networking, or packaging. With Home Assistant already done over MQTT, the old
  telemetry coupling no longer drives sequencing.
- **#13 is worth doing before any bulk Conciliation run**, since Conciliation
  replaces the original with the metadata-free upscale and the capture date is
  then unrecoverable. Its two halves are independent and can land separately:
  **13a** (upscaler) protects everything upscaled from then on, **13b**
  (Conciliation backfill) is what rescues an already-upscaled backlog, and 13b
  keeps earning its place afterwards for anyone conciliating old output.
- **#14 is deliberately parked.** It is easy and self-contained (one GUI module,
  view-only, cannot touch a file), but it is comfort rather than capability: the
  comparison window already does the job. It has no dependencies, so it can be
  picked up whenever there is appetite for a small, low-risk piece of work.
- **#15 is gated by spend, not by other features.** It needs a paid account and
  billed GPU time to answer three questions no public page answers, so its
  ordering is set by when that spend happens, not by #13/#14/#12. Nothing else
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

Roadmap **#1, #2, #5, #6, #7, #8, #9 and #10** are done and live; they are no longer
described in full here (their design of record lives in `CLAUDE.md`,
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

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, coarse idea #2 (deferred local-engine install), coarse idea #3
(parallel jobs), coarse idea #4's automatic-telemetry half, and the standing
constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
