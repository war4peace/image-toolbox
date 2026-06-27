# Future Features

Candidate features that are **not yet implemented (or only partly implemented)**,
sorted by implementation difficulty (easiest first), with a feasibility
assessment for each. See "Sequencing & dependencies" for the cross-feature
threads that should drive ordering, and "Decided against / constraints" at the
bottom for ideas investigated and dropped.

What remains is the tail of the remote-pod work (#1), a substantial new
RunPod-only **video upscaling** feature that builds directly on it (#2), then two
much-lower-priority milestones (HTTP interface #3, Unraid #4) that each introduce
a new process model, networking, or packaging. Anything already shipped has been
removed from this list (see `CLAUDE.md` for the feature set as built).

---

## 1. Remote upscaling (RunPod) — core shipped, one enhancement remains

The remote path itself is **done** (0.3.1–0.3.9): a disposable pod streams one
image at a time to a resident on-pod worker, with straighten-on-pod, pod
telemetry, the dead-man's switch (idle-timeout + max-runtime), zero-config SSH,
the Local/Remote/Both install-mode wizard, one-click model-volume provisioning,
remote Tag & Rename, a world-wide Region/Data-center picker, a live cheapest-first
GPU picker with a per-task price ceiling and ordered fallback chain (both tabs),
and live cost tracking (the deployed pod's real `costPerHr` driving the
"Est. Time Remaining / Cost" and "$ / 100 images" readouts). See `CLAUDE.md` and
`docs/runpod-notes.md` for the shipped design. The one piece still open:

- **Funds-floor safety-net + auto-stop** (API verified live, 2026-06-21). Per-run
  cost is already tracked live, but the *account balance* isn't. Pull it from the
  **legacy GraphQL** API: `query { myself { clientBalance currentSpendPerHr } }`
  at `https://api.runpod.io/graphql`. The REST key authenticates it (Bearer or
  `?api_key=`) but Cloudflare blocks the default `Python-urllib` User-Agent —
  **must send a browser-like `User-Agent`** (REST has no balance endpoint; all
  probes 400). Derived **time until funds depleted** = `clientBalance /
  currentSpendPerHr`. Then **auto-stop** (or refuse to start) when session cost
  exceeds a configurable cap *or* remaining balance drops below a floor — a money
  safety-net alongside the time/idle dead-man's switch. Keep it in one isolated,
  fail-safe helper (no balance → skip the checks, never block on it).

## 2. Video upscaling (RunPod-only) — Moderate, builds on #1

> **Full design & plan: [`docs/video-upscaler.md`](video-upscaler.md).** That doc
> is the single source of truth; this entry is just the roadmap pointer.

A major new RunPod-only feature (UI tab **"Video Upscaler"**) that upscales a
collection of videos with SeedVR2, the same engine the Batch Upscaler uses for
stills. SeedVR2 already has mature native video support the image path bypasses, so
this is an **orchestration + UX + tuning** feature, not an upscaler build.

Chosen architecture: split each source into ~1-minute **segments** locally with
ffmpeg, make a segment the **queue unit** streamed through #1's existing per-item
remote path (upload → pod upscales → download), then reassemble and mux the
original audio locally. This reuses #1's pod/queue/resume/cost machinery almost
wholesale and gives **segment-level resume**, which makes the dominant constraint
(cost: a diffusion pass per frame means ~$3.80 per 1080p minute, ~$220 for a 1-hour
video) payable **in installments**. Shared `config.json` (new `video` section) and
`db/cache.db` (new `video_*` tables); no new files. The hard prerequisite is a
**SeedVR2 video-settings benchmark pass** (`batch_size`/`temporal_overlap`/segment
length per target x card) to set defaults and produce trustworthy cost rates. See
the design doc for the locked decisions (keyframe handling, fixed-seed seams,
duration-drift detection), gotchas, config/DB schema, build pieces, and phasing.

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

- **#1's core is shipped;** only the funds-floor safety-net above remains, and it
  is independent of everything below.
- **#2 (video) builds directly on #1** and is the clear next feature: it reuses the
  pod lifecycle, network volume, GPU picker, cost tracking, and the per-item
  streaming/resume machinery almost wholesale. Its only hard prerequisite is the
  SeedVR2 video-settings benchmark pass (and it inherits #1's funds-floor work for
  free once that lands).
- **#3 and #4 are much lower priority** and are otherwise large, mostly
  independent milestones. With Home Assistant already done over MQTT, the old
  telemetry coupling no longer drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Architectural watch-item:** the app is dependency-light and Windows-only. #3
  and #4 each push toward extra packages, a long-running server, and
  cross-platform support, so adopt those deliberately. #2 stays within the existing
  dependency-light, RunPod-only envelope (ffmpeg is the one new external tool, and
  it ships on the pod).

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
