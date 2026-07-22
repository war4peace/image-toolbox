# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

The remaining open milestones are two lower-priority ones that each introduce a
new process model, networking, or packaging (HTTP interface #3, Unraid #4).

**Shipped milestones (kept only as a numbering legend).** Roadmap **#1, #2, #5, #6,
#7, #8, #9 and #10** are done and live; they are no longer described here (their design
of record lives in `CLAUDE.md`, `docs/runpod-notes.md`, `docs/video-upscaler.md`,
`docs/local-video-upscaler.md`, `docs/benchmark-sharing.md`, `docs/telemetry-design.md`
and `samples/home-assistant/`). The numbers survive only because code and other docs
cite the roadmap by them (`remote #1`, `Video Upscaler #2`, `local #7`):

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

---

## Contents

- [3. HTTP interface](#3-http-interface-hard-low-priority)
- [4. Unraid Community Apps integration](#4-unraid-community-apps-integration-hardest-low-priority)
- [Sequencing & dependencies](#sequencing--dependencies)
- [Decided against / constraints](#decided-against--constraints)

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

- **#1, #2, #5, #6, #7, #8, #9 and #10 are complete** (remote upscaling + funds-floor;
  RunPod video; video conciliation; self-healing remote runs; local video; benchmark
  sharing; telemetry usage graphs; Home Assistant dashboard samples), so the remaining
  sequencing is only among the low-priority open milestones below.
- **#3 and #4 are the only open milestones**, and both are much lower priority:
  large, mostly independent, and each introducing a new process model, networking,
  or packaging. With Home Assistant already done over MQTT, the old telemetry
  coupling no longer drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-ons from the shipped #6/#7 (not yet scheduled):** generalise the
  Auto-resume supervisor from video to the image runners (batch upscale / tag); and
  #7's deferred Phase 2, a non-SeedVR fixed-ratio 2x/4x engine (Real-ESRGAN-class:
  fast, low-VRAM, deterministic) dropping into the same engine seam.
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

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, coarse idea #2 (deferred local-engine install), coarse idea #3
(parallel jobs), coarse idea #4's automatic-telemetry half, and the standing
constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
