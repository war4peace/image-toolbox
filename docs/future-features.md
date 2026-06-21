# Future Features

Candidate features for the toolbox, **sorted by implementation difficulty
(easiest first)**, with a feasibility assessment for each. See the bottom for
the cross-feature dependencies that should drive sequencing.

What remains are three larger milestones that each introduce new process models,
networking, or packaging. The earlier, lower-risk additions have all shipped (the
`scripts/` reorganisation, the "Report an issue" link, and the original-vs-
upscaled comparison view) and have been removed from the list.

---

## 1. Remote upscaling (RunPod) — Hard
Spin up a runpod.io pod, ship the upscale work to it, fetch the results back, and
shut it down when finished — so a user without a strong local GPU (or one whose
GPU has hit the degradation bug, see below) can still upscale at full quality.
See `docs/runpod-notes.md` for distilled notes from the old scripts.

**Design lead — the dead-man's switch.** A rented pod bills by the second, so the
*organising constraint* of this feature is not throughput, it is **never leaving a
billed pod running**. Build the stop path first and make it the default, not an
afterthought: the pod must shut itself down unless something is actively keeping
it alive. Concretely —

- The pod runs with a **self-stop deadline** (a max-runtime ceiling and an
  idle-timeout) enforced **on the pod**, so a dropped SSH session, a crashed
  controller, or a closed laptop lid still ends the bill. The local app issuing
  `POST /pods/{id}/stop` is the *fast* path; the on-pod deadline is the
  *guaranteed* path. Don't rely on the client staying alive.
- After a run the app stops the pod automatically with a short cancel countdown
  (notes: 60 s, Escape to cancel) and **always** closes the tunnel. If the stop
  API call fails, surface the pod ID and tell the user to stop it manually —
  never fail silently.
- Track and report cost (`hours * hourly_rate`) on every run; put it in the
  Discord completion embed (`runpod-notes.md` lists the fields).

**The watchdog is the pod health signal.** The 0.3.0 performance watchdog
(`run_pass` in `batch_upscale.py`) was built with this in mind: it normalises
**seconds per output megapixel** against the run's **running minimum** and trips
on a sustained slow streak *or* a hard OOM, emitting a `DEGRADED` event. Locally
that just auto-stops and tells the user to reboot. **On a rented pod the same
signal becomes a money decision:** a degraded pod is silently burning cash at a
fraction of its healthy throughput, so `DEGRADED` should **tear the pod down**
(it's disposable — unlike the local GPU there's nothing to reboot and resume on;
provision a fresh one) and either re-queue automatically or alert. This is the
reusable health signal the watchdog work was aimed at — remote-pod #1 is its real
home, the local auto-stop is the proving ground.

- **Why it's hard:** the upscaler now loads SeedVR2 **in-process**, so the GPU
  work happens wherever the script runs. The old "tunnel to a service on the
  pod" model no longer applies — the flow must be **inverted**: ship the work to
  the pod and fetch results back.
- **Work needed:** RunPod REST calls to **create/start** a pod (the notes only
  cover *stop*); SSH connectivity + key management on Windows; a resident on-pod
  upscale worker that loads SeedVR2 once and serves **one image at a time** (the
  queue/resume-cache/watchdog stay local); a persistent **network volume holding
  the models** so disposable pods don't re-download ~22 GB each start
  (region-locked — see notes); stream progress (and `DEGRADED`) back over SSH;
  the dead-man's-switch stop path and **cost tracking** above.
- **Packaging follow-on — DONE (0.3.2).** An **install-mode wizard** (Local /
  Remote / Both) in the Inno installer writes `install_mode.txt`; `bootstrap.ps1`
  reads it and a Remote-only install skips the local torch (~3 GB) + SeedVR2
  engine/weights + timm + Ollama, checking for OpenSSH instead. The GUI already
  imports `upscale_engine`/torch lazily, so a torch-less install launches; remote
  auto-straighten runs on the pod worker (`/orient`), and tagging tunnels to
  Ollama on the pod. See `docs/runpod-notes.md`.
- **Risks:** the most failure-prone — network drops mid-transfer, partial
  uploads, billed pods left running if auto-stop fails, SSH on Windows, remote
  bootstrap drift. Should be its own milestone.

### Remote-pod backlog (enhancements on the working core)

The core remote path shipped in 0.3.1 (create/stream, straighten-on-pod via the
worker's `/orient`, pod telemetry, the Stop-pod modal, and the dead-man's switch).
**0.3.2 added the onboarding layer that makes it usable by a non-technical user:**
zero-config SSH (the app owns an ed25519 key and injects its public half via
`PUBLIC_KEY`, so no key is registered on the RunPod website — `ssh_setup.py`); a
**Local / Remote / Both install-mode wizard** in the Inno installer that writes a
marker `bootstrap.ps1` reads to skip the ~3 GB local GPU stack for Remote-only;
and **one-click model-volume provisioning** from Settings ("Provision models…" →
`runpod_provision.py setup-volume`, a create→provision→auto-terminate pod with a
streamed progress window). It also added **remote Tag & Rename** (worker "tag
mode" + `ollama serve` on the pod + a second tunnel; `tag_and_rename` runs locally
against the tunnelled Ollama, straighten on the pod) so a Remote-only install can
tag too. Remaining follow-ups:

- **Region from bootstrap.** Ask the user's region during first-run bootstrap and
  use it as the default for both (a) creating the model network volume and (b) the
  target data center when spinning up pods. Today the volume's region is read back
  and pods are pinned to it (region-locked), but the *initial* choice is implicit —
  pre-seeding it from bootstrap (defaulting to the nearest EU DC, e.g. EU-RO-1 for
  this user) removes a manual step and prevents a wrong-region volume that no pod
  can then attach. The curated EU list already exists (`EU_DATACENTERS`).
- **GPU fallback chain when the primary type is unavailable.** RTX 5090 capacity in
  a single EU DC is intermittent (seen: "not enough free GPUs" on restart, and a
  dry region failing `create`). When the configured GPU can't be allocated, fall
  through a prioritized list instead of failing the run. Proposed order: **L40S**
  (48 GB, Ada, datacenter-reliable, good EU availability) → **RTX PRO 4500
  Blackwell** (32 GB, newest gen, fast) → **A40** (48 GB, Ampere — older/slower but
  cheap and widely available). All four run on the current cu128 / torch 2.9.1
  image (Blackwell sm_120, Ada sm_89, Ampere sm_86). The SeedVR2 workload peaked
  ~21.7 GB VRAM, so 32 GB is enough — VRAM isn't the differentiator, throughput and
  availability are, which is why the order is speed-then-cheap-fallback. *Agreed
  with the proposed priority;* L40S is strong enough (48 GB + datacenter
  reliability) it could even be a co-default with the 5090, but the 5090 benched
  faster so it stays primary. Implementation: `create_pod` already takes a
  `gpuTypeIds` **list** — verify whether RunPod treats it as "any of these in
  order" (then the fallback is just passing the list); otherwise loop the types in
  `create_pod_resilient` on a capacity failure.
- **Cost & funds tracking + auto-stop** (API verified live, 2026-06-21):
  - Per-pod hourly rate is on the pod object: `costPerHr` (REST `GET /pods/{id}`,
    e.g. 0.99) — read it from the pod instead of the manually-set
    `runpod.hourly_rate`.
  - Account balance + total spend rate come from the **legacy GraphQL** API:
    `query { myself { clientBalance currentSpendPerHr } }` at
    `https://api.runpod.io/graphql` (returned clientBalance≈34.57,
    currentSpendPerHr≈0.998). The REST key (`rpa_…`) authenticates it (Bearer *or*
    `?api_key=`), but the endpoint Cloudflare-blocks the default `Python-urllib`
    User-Agent — **must send a browser-like `User-Agent`**. The REST API has **no**
    balance/billing endpoint (all probes 400).
  - Derived: session cost = `elapsed_h × costPerHr`; **time until funds depleted**
    = `clientBalance / currentSpendPerHr` (≈34 h here). Surface in the status row /
    Discord embed, and **auto-stop** the pod (or refuse to start) when session cost
    exceeds a configurable cap *or* remaining balance drops below a floor — a money
    safety-net alongside the time/idle dead-man's switch.
  - Caveat: GraphQL is the legacy, semi-supported API — keep it in one isolated,
    fail-safe helper (no balance → skip the funds checks, never block on it).
- **Multiple remote providers (investigate).** RunPod is the only backend today.
  Investigate alternatives (e.g. **vast.ai**, and others) so the user could pick a
  remote-pod supplier — for price, GPU availability, or region (a user outside the
  EU may have no nearby RunPod DC). This means abstracting the provider behind a
  thin interface: create/start/stop/terminate, an SSH endpoint, a model-volume
  equivalent, and per-hour cost / balance. The pod-side worker, the streaming
  engine, the dead-man's switch and the queue/watchdog are all provider-agnostic
  already, so the work is a provider adapter + a Settings picker — not a rewrite.
  vast.ai's model differs (a marketplace of individual hosts, per-host pricing,
  its own CLI/API and no managed network volumes), so feasibility hinges on
  whether a persistent model store and reliable SSH can be had per provider.

## 2. HTTP interface — Hard
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

## 3. Unraid Community Apps integration — Hardest
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now — there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port — not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#2) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

---

## Sequencing & dependencies

- **Already shipped (0.2.0–0.3.0):** image-tree conciliation, in-app auto-update,
  Home Assistant (MQTT), the system-telemetry sampler, crash logging,
  auto-straighten-before-upscaling, the `scripts/` reorganisation, the "Report an
  issue" link, the original-vs-upscaled comparison view (a floating before/after
  wipe window with shared zoom/pan, plus green/red outcome frames in the
  film-strip), and — in 0.3.0 — the **performance watchdog**, taskbar
  flash/progress, and the film-strip context menu. Those former roadmap items
  have been removed from the list.
- **#1 has a foundation already in place.** The 0.3.0 performance watchdog was
  built as the reusable **pod health signal** for #1 (see its section above); the
  local auto-stop is its proving ground. That is the one cross-feature thread that
  now drives sequencing toward #1.
- **#1, #2 and #3 are otherwise large, mostly independent milestones.** With Home
  Assistant already done over MQTT, the old telemetry coupling no longer drives
  sequencing.
- **#3 depends on #2** (headless Unraid needs a web UI).
- **Architectural watch-item:** the app is dependency-light and Windows-only. #1,
  #2 and #3 each push toward extra packages, a long-running server, and
  cross-platform support — adopt those deliberately.
