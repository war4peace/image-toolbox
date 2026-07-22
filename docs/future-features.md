# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

The remaining open milestones: two small, self-contained QoL items (telemetry
usage graphs #9, Home Assistant dashboard samples #10), and two lower-priority
milestones that each introduce a new process model, networking, or packaging
(HTTP interface #3, Unraid #4).

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

## 9. Telemetry usage graphs — Easy-Medium (QoL)
A pop-up, non-modal, read-only window with time-based graphs (1h / 3h / 6h / 12h
/ 24h ranges) of the telemetry the app already samples: the local machine's
always-on row, and a remote pod's row during an active run. Opened by clicking
anywhere inside the relevant telemetry row.

- **No history exists today.** `TelemetryRow` renders instantaneous samples and
  discards them. Local flow: `App.sample_telemetry` (worker thread) ->
  `_apply_telemetry` -> rows + MQTT. Remote flow: the runner's `RTELEM` events
  (10 s) -> `App.apply_remote_telemetry` -> the tab's remote row + MQTT. Both
  flows carry the same sample shape (`cpu`, `ram_used_mb/_total_mb`,
  `gpu_used_mb/_total_mb`, `gpu_temp_c`), so **one** graph window class serves both.
- **Cadence is irregular by design** (5 s upscaling, 30 s tag/conciliate, 60 s
  idle, 10 s remote), so the graph must plot by timestamp, never by sample index.
  Volume is trivial: 24 h at the worst-case 5 s cadence = ~17,280 points/series,
  a few MB of RAM in a ring buffer.
- **No new dependency.** matplotlib is present on Local/Both installs (a seedvr2
  requirement) but absent on Remote-only, so it can't be used. A stdlib tkinter
  Canvas line chart with decimation is enough for 4 series.

**Recommended design.**
1. **`TelemetryHistory`** (new, pure, unit-testable; could live in
   `system_telemetry.py`): a per-source ring buffer (`local`, `remote:<tab>`),
   `append(ts, sample)`, `window(seconds) -> decimated series`. Hooked with one
   line each in `_apply_telemetry` and `apply_remote_telemetry`. In-memory only
   (logs are deliberately not in the DB, and telemetry is even more disposable);
   accept that the 24 h range only fills after 24 h uptime and history is lost on
   close (state it, e.g. a subtle "since 14:02" label).
2. **`TelemetryGraphWindow`** (new `gui/` module or in `gui/widgets.py`):
   non-modal Toplevel, one shared instance per source, geometry persisted as a
   `telemetry_geometry` sibling of `compare_geometry`. Range buttons; two stacked
   Canvas charts (a percent chart CPU/RAM/VRAM colour-matched to the row's band
   palette, and a GPU-temp chart). Redraw on a 5-10 s `after()` tick while open;
   decimate to ~one point per 2 px; break the line where adjacent samples are
   >~3x the median interval apart (an honest gap, no interpolation across idle).
3. **Remote history is per-run**: cleared when a run starts; keep the last run's
   history viewable until the next run, titled with the pod's GPU.
4. Clicking a telemetry row opens the window for THAT source. `TelemetryRow`
   labels are destroyed/recreated on each `_set`, so the `<Button-1>` binding
   must be applied inside `_set` per label (plus once on the frame).

**Effort:** S-M (1-2 sessions). **Risk:** low; read-only, fail-safe, no new
dependency, no runner changes. Open: persist local history across restarts?
Recommend NO for v1 (keep it in-memory and honest). One combined dual-axis chart
vs two stacked? Recommend two stacked (no dual-axis confusion).

## 10. Home Assistant dashboard samples — Easy-Medium (QoL, docs/samples)
Ship ready-made Home Assistant **dashboards** (Lovelace YAML) for users who run
both HA and Image Toolbox, in two tiers: a **simple** one built only from HA's
**core** Lovelace cards (no HACS, works on any install), and a **richer /
eye-candy** one using named HACS custom cards (each listed with its source URL)
for nicer graphs and status tiles. All HA material lives in a new
`samples/home-assistant/` folder.

- **This is a docs/samples deliverable, not a code feature.** The MQTT surface is
  already complete and stable (`mqtt_publisher.py`): every topic a dashboard needs
  is published retained under `image-toolbox/` — `version`, `update`,
  `latest_version`, `availability` (LWT online/offline), `last_run` (JSON),
  `last_used`, the `task/*` live group (`name`, `details`, `runtime`, `progress`
  = "X/Y", `eta`, `average_processing_time`, `last_processing_time`), and the
  `system/*` + `system/remote/*` telemetry groups.
- **Builds on the existing sensor list** `docs/ha-mqtt-sample-sensors.yaml`
  (linked from the README), which defines the MQTT `sensor:` entries. This idea is
  the next layer up: Lovelace views arranging those entities (user pastes sensors
  first, then a dashboard).
- **Two gaps in that sensor file to fix as part of this work:** (1) it is missing
  `task/progress` and `task/eta`, which the app DOES publish (`tooltab.py`) and a
  dashboard wants; add both. (2) `last_run` is a JSON object (runner summary +
  `tool`/`finished_at`), not a scalar: bound as a plain `state_topic` it blows
  past HA's 255-char state limit and shows truncated. It needs
  `json_attributes_topic` plus a short `value_template` for the state, and the
  dashboard reads fields via attribute templates.
- **No MQTT Discovery today** (no retained `homeassistant/.../config`), so
  entities are manual and don't auto-group under an HA device. Fine for a
  samples/paste approach and keeps the app dependency-light; a Discovery-based
  auto-setup is a much larger separate app-side feature (log it separately).
- **Cadence realities to document:** `system/*` updates only while a task runs
  (plus a 60 s idle sampler); `system/remote/*` exists only during a remote-pod
  run and goes stale afterward. History cards should note that gaps are normal.

**Recommended contents of `samples/home-assistant/`:**
1. **`README.md`** (entry point): prerequisites, install order (sensors YAML
   first, then a dashboard), a short topic reference. **Copy**
   `ha-mqtt-sample-sensors.yaml` in (keeping the docs link pointing at the new
   home) so the folder is self-contained, as the coarse idea asked.
2. **`dashboard-core.yaml`** (tier 1, zero HACS): `entities` card (version /
   update / availability / last-run), a `conditional` card revealing a live-task
   panel only while `task/name` != idle, `gauge` cards (CPU / RAM% / VRAM% / GPU
   temp), and a `history-graph` telemetry card. The "works everywhere" baseline.
3. **`dashboard-custom.yaml`** (tier 2): the same information via named,
   pinned-by-name HACS cards, each documented with its repo URL. Candidate set:
   **Mushroom** (`piitaya/lovelace-mushroom`) for a compact chip header;
   **ApexCharts** (`RomRider/apexcharts-card`) OR **mini-graph-card**
   (`kalkih/mini-graph-card`) for real time-series graphs; **button-card**
   (`custom-cards/button-card`) + **card-mod** (`thomasloven/lovelace-card-mod`)
   for band-coloured tiles matching the app's blue/green/yellow/red palette;
   **auto-entities** (`thomasloven/lovelace-auto-entities`) optional. Recommend a
   **minimal** required set (Mushroom + one graph card + card-mod), the rest as
   optional extras, so it isn't a heavy HACS shopping list.
4. **`screenshots/`**: PNGs of both dashboards (light + dark), captured by hand
   from a live HA instance (the user runs HA and is the only one who can produce
   authentic ones); README ships text-only until then.

**Effort:** S-M (mostly YAML + docs + screenshots; the only code-adjacent change
is the two sensor fixes). **Risk:** low; nothing in the app changes, no new
dependency. Only risk is HACS card churn (pin versions / note "as of <date>").

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
- **#9 and #10 are the immediate small QoL wins**, and neither depends on the
  other: #9 (telemetry graphs) is self-contained, immediately visible, no new
  dependency and no runner changes; #10 (HA dashboard samples) is a docs/samples
  deliverable that touches no pipeline code (only the two sensor fixes), so it can
  ship any time.
- **#3 and #4 are much lower priority** — large, mostly independent milestones.
  With Home Assistant already done over MQTT, the old telemetry coupling no longer
  drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-ons from the shipped #6/#7 (not yet scheduled):** generalise the
  Auto-resume supervisor from video to the image runners (batch upscale / tag); and
  #7's deferred Phase 2 — a non-SeedVR fixed-ratio 2x/4x engine (Real-ESRGAN-class:
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

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, coarse idea #2 (deferred local-engine install), coarse idea #3
(parallel jobs), coarse idea #4's automatic-telemetry half, and the standing
constraints (AMD/ROCm, vast.ai as a second provider).
