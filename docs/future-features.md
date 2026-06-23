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

- **Region from bootstrap — MOSTLY DONE (0.3.4); only the bootstrap pre-seed
  remains.** Settings now has a **world-wide Region + Data center picker**
  (`runpod_client.data_centers` GraphQL live list, storage-capable DCs only,
  grouped into Europe / North America / Asia / Oceania via `region_of`), the volume
  buttons act in the chosen DC with a clear target readout, and selecting an
  existing volume syncs the picker to its region. So a user anywhere can configure
  the right region/DC and can't provision a volume in a DC that can't host one. The
  *remaining* nicety: ask the region during **first-run bootstrap** and pre-seed
  `data_center_ids` so even the very first volume defaults to the user's nearest
  region (today it still defaults to EU-RO-1 until they touch the picker).
- **GPU fallback chain — MECHANISM DONE (0.3.2), used for tagging; upscale TODO.**
  `create_pod_resilient` now treats `spec["gpuTypeIds"]` as an **ordered fallback
  chain**: each type is tried in turn (a create/capacity error skips to the next
  immediately; a deploy failure retries the same type) so a run still starts when
  the preferred GPU is sold out. **Remote Tag & Rename already uses it** — a
  curated low-tier chain (`TAG_GPU_TYPES`: RTX 2000 Ada → A4000 → A4500 → RTX 4000
  Ada, all 16–20 GB / ~$0.24–0.26/h, EU-available), since the vision model needs
  only ~6.6 GB. **Still TODO: give *upscaling* the same treatment** — the upscale
  path passes a single `gpu_type_id`, so wrap it in a chain too. Proposed order:
  **L40S**
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
- **Retire the static `hourly_rate` setting → use the live price** (0.3.4 raised
  this; the data is already in hand). Settings still carries a hand-typed
  **"Hourly rate (USD)"** used only to estimate a run's cost for the completion
  notification — but as of the 0.3.3 live picker we now know the *real* price two
  ways: the GraphQL availability query already returns `lowestPrice` per GPU
  (`runpod_client.available_gpus` → each card's `price`), so we have the rate
  **before the pod even exists**, and `costPerHr` is on the pod object once it's
  running. Replace the static field with the actual selected-GPU price (fall back
  to the typed value only if the lookup fails). Removes a setting the user has to
  guess at and keeps the cost estimate honest when availability shifts the card.
- **Real-time run statistics** (builds directly on the live price above). Once the
  per-run rate is known live, surface running cost as the batch progresses:
  **elapsed cost** (`elapsed_h × price`), **cost per image so far**
  (`elapsed_cost / done`), and a **projected total** (`cost_per_image × total`) —
  shown in the tool tab's status row and the Discord completion embed, and
  published to the MQTT `last_run`/`task/*` topics. The per-image figure is the
  number the benchmarking has been chasing by hand (e.g. RTX 5090 ≈ $0.0036/image
  upscaling) — computing it live turns the price ceiling and GPU picker from
  guard-rails into an informed, real-time cost view. The watchdog's per-image
  timing already exists to hang the math off; the streaming engine reports each
  image's completion, so the hook points are there.
- **Cost & funds tracking + auto-stop** (API verified live, 2026-06-21):
  - Per-pod hourly rate is on the pod object: `costPerHr` (REST `GET /pods/{id}`,
    e.g. 0.99) — read it from the pod instead of the manually-set
    `runpod.hourly_rate` (see the two bullets above — this is the same live price,
    just read from the running pod rather than the pre-deploy catalog).
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
- **Multiple remote providers (investigated 2026-06-23: vast.ai NOT pursued for
  now).** RunPod is the only backend today. The idea was to let the user pick a
  remote-pod supplier (for price, GPU availability, or region, since a user outside
  the EU may have no nearby RunPod DC) behind a thin provider interface
  (create/start/stop/terminate, an SSH endpoint, a model-store equivalent, per-hour
  cost). The motivation is real: RunPod GPU stock is often thin (all four curated
  tag cards were out of stock in EU-RO-1 on 2026-06-22), and vast.ai is a far
  larger marketplace.

  **Architecture finding (still true, reusable for any future provider).** The
  pod-side worker (`pod/worker.py`), the streaming engine
  (`remote_upscale_engine.py`), the dead-man's switch, and the local
  queue/resume/watchdog/film-strip are all provider-agnostic. Most of
  `remote_run.py` (SSH/SCP, worker launch, tunnels, heartbeat) is generic given an
  SSH endpoint plus a writable filesystem holding the venv and models. What is
  RunPod-specific and would need a sibling adapter: the whole control plane
  (`runpod_client.py`: pod CRUD, GPU stock/price, data centers, network volumes,
  regions, the `allowedCudaVersions` / GraphQL deploy path), the `spec` dict and
  `PUBLIC_KEY` injection in `remote_run._create_pod`, the `/workspace` assumptions
  in `pod/provision.sh`, the RunPod-API self-stop in `pod/deadman.py`, and the
  heavily RunPod-shaped Settings panel plus per-tab pickers in `toolbox_gui.py`
  (~250 references). So a port would be a provider seam (a `RunPodProvider`
  wrapping today's client, a new `VastProvider`) plus a GUI selector, not a rewrite
  of the run mechanics. The GUI is the largest mechanical lift, not the adapter.

  **Showstoppers (vast.ai, from manual pricing research).** Two billing dimensions
  that RunPod does not charge make vast.ai a poor fit for THIS app's design:

  1. **Steep storage pricing.** vast.ai bills both container disk and volumes at
     roughly **$0.33 to $0.40 /GB/mo** (a sampled machine: 16 GB disk $5.33/mo,
     10 GB volume $3.33/mo, i.e. ~$0.33/GB/mo). RunPod charges **$0.07/GB/mo** for
     a network volume. The ~24 GB model store (SeedVR2 16 GB + venv + Ollama 6 GB)
     costs ~$1.6/mo on RunPod versus ~$8 to $10/mo on vast.ai, about 5x more.
  2. **Metered internet, both directions.** vast.ai bills bandwidth at
     **$40/TB ingress AND egress**; RunPod includes bandwidth at no charge. This
     directly taxes the app's core design: the streaming model uploads every source
     image and downloads every result (see "Decided design" in `runpod-notes.md`),
     and a model-baked image would be a large recurring pull on every fresh host.
     A bandwidth-metered provider penalises exactly the traffic pattern the app
     depends on.

  Compounding these, vast.ai has **no region-wide network volume**: its volumes are
  host-local, which pins a fast start to one physical machine and so defeats the
  availability gain that motivated the look in the first place. The only portable
  model-store option left is a ~24 GB baked Docker image, which is both new
  build/push infra (the project has none today) and a bandwidth-metered pull per
  new host.

  **Decision: not pursued for the time being.** The combination of pricier storage,
  metered bandwidth on a streaming-heavy design, and no managed network volume
  removes the cost/availability advantage that was the entire point. This also
  explains why RunPod is so dominant and sought-after for this kind of workload:
  free bandwidth plus cheap, region-wide network storage that mounts on disposable
  pods are precisely what a stream-one-image-at-a-time, disposable-pod app needs.
  If this is ever revisited, vet a candidate provider against this checklist BEFORE
  any code: (a) free or cheap ingress/egress, (b) cheap region-wide persistent
  storage that mounts on disposable instances, (c) reliable SSH with key injection.
  The provider-seam refactor above is only worth building once a candidate clears
  (a) and (b).

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
