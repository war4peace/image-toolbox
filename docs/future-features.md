# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering, and "Decided against /
constraints" at the bottom for ideas investigated and dropped.

Both remote-pod milestones have shipped: **#1 (remote upscaling)** and **#2 (video
upscaling)** are done and live in the app (see `CLAUDE.md` for the built feature
set). They are kept below as one-line pointers only, because code and other docs
cite them by number. What actually remains is two much-lower-priority milestones
(HTTP interface #3, Unraid #4), each of which introduces a new process model,
networking, or packaging.

---

## 1. Remote upscaling (RunPod) — SHIPPED (0.3.1–0.4.2)

Done and live. A disposable pod streams one image at a time to a resident on-pod
worker (straighten-on-pod, pod telemetry, dead-man's switch, zero-config SSH, the
install-mode wizard, one-click model-volume provisioning, remote Tag & Rename, the
Region/DC + live cheapest-first GPU pickers, live cost tracking), with the
**funds-floor safety-net + auto-stop** (`scripts/funds_guard.py`) landing in 0.4.2.
Design of record: `CLAUDE.md` + `docs/runpod-notes.md`.

## 2. Video upscaling (RunPod-only) — SHIPPED (experimental)

Done and live behind the **Video Upscaler** tab: split each source into segments
locally with ffmpeg, stream each segment through #1's remote path, reassemble +
mux audio locally, with segment-level resume/installments. Design + as-built
source of truth: **[`docs/video-upscaler.md`](video-upscaler.md)**.

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

- **#1 and #2 are complete** (remote upscaling + funds-floor, then video), so the
  remaining sequencing is only among the two low-priority milestones below.
- **#3 and #4 are much lower priority** — large, mostly independent milestones.
  With Home Assistant already done over MQTT, the old telemetry coupling no longer
  drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
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
