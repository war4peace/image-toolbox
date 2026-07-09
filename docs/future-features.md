# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering, and "Decided against /
constraints" at the bottom for ideas investigated and dropped.

What actually remains is two much-lower-priority milestones (HTTP interface #3,
Unraid #4), each of which introduces a new process model, networking, or
packaging, plus one smaller candidate (video conciliation #5).

**Shipped milestones (kept only as a numbering legend).** Roadmap **#1 (remote
upscaling)** and **#2 (video upscaling)** are done and live; they are no longer
described here (their design of record moved to `CLAUDE.md`,
`docs/runpod-notes.md` and `docs/video-upscaler.md`). The numbers survive only
because code and other docs cite the roadmap by them (`remote #1`, `Video
Upscaler #2`):

- **#1 — Remote upscaling (RunPod).** Shipped 0.3.1–0.4.2. See `CLAUDE.md` +
  `docs/runpod-notes.md`.
- **#2 — Video upscaling (RunPod-only, experimental).** Shipped. See
  `docs/video-upscaler.md`.

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

## 5. Video conciliation — Moderate (candidate)
Extend Conciliation to videos: match each upscaled video output back into the
source tree and archive (or replace) the original, exactly as the image
Conciliation does today. (Lower-effort than #3/#4; listed after them only to keep
the roadmap numbers stable.)

- **Reuse:** the `lineage` table (`db.py`) already records source->processed
  content-hash links, and `conciliate.py`'s scan/run phases are format-agnostic
  file I/O. The design extends naturally, so this is a feature, not debt.
- **Work needed:** record video-output lineage as the Video Upscaler produces
  files, teach the conciliate scan to include video extensions, and surface
  videos in the Conciliation preview/plan.
- **Risk:** low. No GPU, no new dependency; the "never touch originals /
  archive-first" guarantees carry over unchanged.

## 6. Self-healing remote runs (auto-recover a lost pod) — Moderate (candidate)
Make a long remote run (video especially) survive **losing its pod mid-run** without
the user babysitting it. Today the run uses one contiguous pod; if that pod dies
involuntarily (RunPod reclaims the host, a spot eviction, the SSH tunnel drops, the
internet drops), `run_queue` catches the engine error, marks the remaining jobs
`failed`, and ends. Finished videos are safe (`done` on disk), but the rest need a
**manual** restart to resume. This closes that gap.

- **Desired behaviour (user request, 2026-07-07):**
  1. **Detect** a mid-run failure and classify it: transient connectivity blip
     (pod still alive) vs. real pod loss.
  2. **Connectivity blip** — monitor and **reconnect / reuse the existing pod**
     (re-establish the ssh tunnel, re-probe `/health`) before giving up on it.
  3. **Pod loss** — poll GPU availability (`runpod_client.available_gpus`) **every
     ~30 s** until the **IDENTICAL** card (the one originally picked) is back in
     stock, then **deploy a replacement of that exact type** and **continue from the
     first unfinished segment** (the resume already exists). Never substitute a
     different card; if the identical one is unavailable, **keep waiting
     indefinitely** — there is **no time cap** (see the guardrail note below).
  4. **Log every step** to both the terminal/log pane and the on-disk run log
     (deploy attempts, waits, reconnects, the card chosen, the segment resumed at).

- **Reuse (most of the machinery already exists):** segment-level resume is done
  (`db.py` `video_*` tables + `process_job`'s per-segment skip); `available_gpus`
  already lists deployable-now stock cheapest-first; `_find_existing_pod` can
  re-attach to a surviving pod by its mode-aware name; the dead-man's switch caps a
  pod the healer might otherwise orphan; `debug_log` + the run log give the logging
  sink. So the new part is an **orchestration/retry loop around the session**, not
  new pipeline code.

- **Resume-path prerequisites landed in 0.4.9 (load-bearing, not optional).** The
  healer resumes unattended and *repeatedly* (every redeploy), so it amplifies exactly
  the resume-path failure modes the 0.4.9 review hardened: a blindly-reused partial
  split (item 1, now validated via a `split.done` marker + gapless/frame-sum check
  before reuse) would otherwise ship a truncated deliverable silently on every
  recovery; a deterministically-failing job (item 4, now `fail_count` give-up ->
  `skipped`) would be re-attempted forever across every redeploy, not just every manual
  run; and leaked staging (item 5, now a run-start orphan sweep + remove-on-give-up)
  compounds per abandoned pod. These are therefore prerequisites for #6, not parallel
  work: build the healer on top of the 0.4.9 baseline, and treat "resumes cleanly by
  hand" as the gate that must pass before automating the resume.

- **UI gate (decided 2026-07-07): an "Auto-resume" checkbox to the right of the
  Start button, default UNCHECKED.** Per-run and visible at the point of action (not
  a hidden global Setting), so the behaviour is opt-in and unsurprising. When
  unchecked, an interruption behaves exactly as today (mark remaining `failed`, end,
  manual restart resumes). When checked, the supervisor is armed for that run.
  (Naming note: this is distinct from the existing segment-level resume, which is a
  *manual* restart; "Auto-resume" means auto-recover the pod and continue.)

- **Work needed:** a supervisor that wraps `session.start()` + `run_queue` in a
  retry loop with backoff; a health/liveness probe to distinguish blip from loss; an
  **unbounded** "wait for stock" poll (backoff to avoid hammering, but no time cap —
  see guardrail note); and a redeploy path that re-uses the resume state. Gated by
  the Auto-resume checkbox.

- **Design tensions to resolve first (why this isn't a quick bolt-on):**
  - **No-GPU-substitution rule (0.4.0) — honoured unconditionally (decided
    2026-07-07).** Recovery redeploys the **IDENTICAL** card the user picked and
    nothing else: there is **no substitution at all**, ticked or not, so 0.4.0's rule
    holds without exception. If the identical card is out of stock the healer just
    keeps polling for it **indefinitely**; it never falls back to a different card.
    The Auto-resume checkbox therefore gates only *whether to auto-recover* (and the
    associated unattended spend), not *which card* — the card is always the same one.
  - **Double-billing / orphans:** a redeploy while the old pod is only *unreachable*
    (not dead) can leave two billed pods. The healer must confirm the old pod is
    terminated (or reclaim it) before/while deploying a new one, and the dead-man's
    switch is the backstop.
  - **Guardrail is MONEY, not time (decided 2026-07-07).** No time cap on the wait,
    ever. Rationale: a 24 h batch is exactly the case this feature exists for; if the
    user opted in and left the machine running, "I gave up after 3 h" betrays the
    trust the checkbox asked for. And while *waiting* for stock **no pod runs, so
    nothing is billed** — a time cap protects against nothing. The only automatic
    stops are: (a) the queue finishes, (b) the user presses Stop, (c) `funds_guard`
    trips (balance floor / session cost cap) — a real-money bound, which only applies
    once a pod is actually redeployed and running, or (d) a genuinely unrecoverable
    error that is not a stock-out. Notify when the run enters wait-for-stock (so a
    check-in shows "waiting for RTX PRO 6000, retrying every 30 s, N h elapsed, $0
    spent while waiting") and again when it recovers.
  - **Model reload cost:** each new pod re-loads SeedVR2 (~2–3 min cold start), so
    thrashing redeploys on flapping stock is wasteful — back off the poll interval,
    don't hammer. (Backoff, not a cap: it slows retries, it never stops them.)

- **Risk:** moderate. The resume foundation is solid, but the failure-mode matrix
  (blip vs. loss vs. stock-out vs. funds-exhausted) and the billing-safety around
  redeploy are where the care goes. Scope it to the **video** run first (long,
  most exposed), then generalise to the image runners if it proves out.

---

## Sequencing & dependencies

- **#1 and #2 are complete** (remote upscaling + funds-floor, then video), so the
  remaining sequencing is only among the two low-priority milestones below.
- **#3 and #4 are much lower priority** — large, mostly independent milestones.
  With Home Assistant already done over MQTT, the old telemetry coupling no longer
  drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **#5 (video conciliation) is independent and lower-effort** — no new process
  model or dependency, just lineage recording on the video path plus scan/plan
  wiring; it can land whenever the Video Upscaler is exercised enough to want it.
- **#6 (self-healing remote runs)** builds on the shipped remote/video stack (segment
  resume, `available_gpus`, `_find_existing_pod`, `funds_guard`) plus the 0.4.9
  resume-path hardening (items 1/4/5 above): those are load-bearing prerequisites, not
  parallel work, because the healer resumes unattended and repeatedly (see the note under
  #6). No new process model; the effort is orchestration + billing safety, not pipeline
  code. Worth doing once unattended overnight video runs become routine.
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
