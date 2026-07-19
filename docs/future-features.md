# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering, and "Decided against /
constraints" at the bottom for ideas investigated and dropped.

The remaining open milestones are both lower priority: two that each introduce a new
process model, networking, or packaging (HTTP interface #3, Unraid #4).

**Shipped milestones (kept only as a numbering legend).** Roadmap **#1, #2, #5, #6,
#7 and #8** are done and live; they are no longer described here (their design of record
lives in `CLAUDE.md`, `docs/runpod-notes.md`, `docs/video-upscaler.md`,
`docs/local-video-upscaler.md` and `docs/benchmark-sharing.md`). The numbers survive
only because code and other docs cite the roadmap by them (`remote #1`, `Video
Upscaler #2`, `local #7`):

- **#1 — Remote upscaling (RunPod).** Shipped 0.3.1–0.4.2. See `CLAUDE.md` +
  `docs/runpod-notes.md`.
- **#2 — Video upscaling (RunPod-only, experimental).** Shipped. See
  `docs/video-upscaler.md`.
- **#5 — Video conciliation.** Shipped 0.5.1-experimental: Conciliation now
  matches and replaces VIDEO originals with their upscaled outputs, alongside
  images, in one scan. Videos match by the content-hash `lineage` the Video
  Upscaler records on completion (item 10) ONLY — no name fallback, so a partial
  clip (which records no lineage) can never be mistaken for a whole-video match;
  a video is acted on only when its output is present in the chosen processed
  tree. See `CLAUDE.md` (Conciliation) and `conciliate.py`.
- **#6 — Self-healing remote runs (auto-recover a lost pod).** Shipped 0.5.0
  (video only): an opt-in "Auto-resume" supervisor reconnects a blipped pod, or
  waits unbounded for the identical card and redeploys it, continuing from the
  first unfinished segment. Funds guard / user Stop / completed queue are the only
  non-redeploy stops. See `docs/video-upscaler.md` section 17.
- **#7 — Local video upscaling (free-and-slow alternative to remote).** Shipped
  0.5.0: the same SeedVR2 video work runs in-process on the user's own GPU via
  `LocalVideoEngine`, with a predictive VRAM sizer, a one-click per-card benchmark,
  and optional `torch.compile`. See `docs/local-video-upscaler.md`.
- **#8 — Benchmark sharing (community download / contribute).** Shipped 0.5.1: the
  per-card video benchmark becomes a crowdsourced corpus, auto-downloaded from GitHub
  at launch and contributed back via a browser-delegated GitHub issue (multi-GPU,
  deduped against the published set); a maintainer `--merge` tool curates submissions.
  See `CLAUDE.md` (Benchmark sharing) and `docs/benchmark-sharing.md`.

---

## 3. HTTP interface — Hard (low priority)
Spin up a small HTTP server with a UI that mirrors the application UI.

- **What "mirror" implies:** rebuilding the thumbnail wall, two-row live status,
  progress/ETA, pause/resume/stop, and Settings as a web app — plus a backend
  and live updates (WebSocket/SSE).
- **Reuse:** the subprocess + stdin/stdout protocol is a clean backend seam; a
  server can drive the same scripts the GUI does.
- **Work needed:** an HTTP server (stdlib `http.server` is too thin for this —
  realistically a small framework), a streaming channel for live
  progress/thumbnails, and a full second UI to maintain alongside the tkinter
  one.
- **Risks:** large, ongoing surface area (two UIs to keep in sync); auth/binding
  concerns if exposed beyond localhost.
- **Scope note:** a minimal "status + start/stop" web panel is far cheaper than
  a true mirror and worth considering first.

## 4. Unraid Community Apps integration — Hardest (low priority)
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now — there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port — not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#3) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

---

## Sequencing & dependencies

- **#1, #2, #5, #6, #7 and #8 are complete** (remote upscaling + funds-floor; RunPod
  video; video conciliation; self-healing remote runs; local video; benchmark
  sharing), so the remaining sequencing is only among the low-priority open
  milestones below.
- **#3 and #4 are much lower priority** — large, mostly independent milestones.
  With Home Assistant already done over MQTT, the old telemetry coupling no longer
  drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-ons from the shipped #6/#7 (not yet scheduled):** generalise the
  Auto-resume supervisor from video to the image runners (batch upscale / tag); and
  #7's deferred Phase 2 — a non-SeedVR fixed-ratio 2x/4x engine (Real-ESRGAN-class:
  fast, low-VRAM, deterministic) dropping into the same engine seam.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #3
  and #4 each push toward extra packages, a long-running server, and
  cross-platform support, so adopt those deliberately.

---

## Decided against / constraints

- **Region pre-seed at first-run bootstrap — dropped.** The idea was to ask the
  user's region during install and pre-seed `data_center_ids`. After repeatedly
  checking the live list, there are so few regions/data centers that auto-detecting
  one adds little: the Settings Region/DC picker already lets the user pick
  directly, which is clearer than guessing for them.
- **AMD GPUs (ROCm) — not supported, filtered out.** The pipeline is CUDA-only
  (PyTorch CUDA build, SeedVR2, the orientation CNN, `nvidia-smi` telemetry), so an
  AMD card can't run any task. RunPod occasionally lists AMD Instinct cards (e.g.
  the MI300X in EU-RO-1, sometimes *cheaper* than comparable NVIDIA), so
  `available_gpus` drops them at the source via `is_amd_gpu` (0.4.0) rather than
  letting a user pick one that fails at run time. A ROCm port would be a separate,
  large effort and is not planned.
- **vast.ai as a second provider — investigated 2026-06-23, not pursued.** The
  goal was provider choice (price/availability/region) behind a thin interface.
  Two billing dimensions RunPod doesn't charge make vast.ai a poor fit for this
  app's stream-one-image-at-a-time, disposable-pod design: **storage** is
  ~$0.33–0.40/GB/mo (RunPod $0.07), and **bandwidth is metered both ways** at
  ~$40/TB (RunPod free) — directly taxing the upload-every-image / download-every-
  result flow. It also has **no region-wide network volume** (host-local only),
  which defeats the availability gain that motivated the look. Reusable finding:
  the worker, streaming engine, dead-man's switch, and local queue/watchdog are
  provider-agnostic; a port would be a provider seam (`RunPodProvider` +
  `VastProvider`) plus a GUI selector, the GUI being the largest lift. Vet any
  future provider against this checklist before writing code: (a) free/cheap
  ingress+egress, (b) cheap region-wide persistent storage that mounts on
  disposable instances, (c) reliable SSH with key injection.
