# RunPod remote-pod notes (for a future feature)

Reference notes distilled from the old `remote-image-upscale.ps1` /
`remote-tag-and-rename.ps1` scripts (removed — see git history before commit
`baf6f8b` for the full originals). Those scripts targeted the **pre-0.1.0
ComfyUI architecture** and do not run against the current app; this file keeps
the parts that are still worth reusing when remote-pod support is rebuilt.

## Open items / TODO

- **SSH onboarding for non-technical users — DONE (0.3.2, zero-config).** The app
  now owns a dedicated ed25519 key (`scripts/ssh_setup.py`): it locates OpenSSH
  (Windows 10/11 optional feature; detected + guided if absent), generates the key
  on demand (Settings → "Set up SSH key", and auto-ensured in the remote-run
  preflight), and hands its **public half to every pod via the `PUBLIC_KEY` env
  var** — RunPod's base images append it to `authorized_keys` at boot, so **no key
  is ever registered on the RunPod website** and `ssh_key_path` needn't be edited
  (empty → the managed default). Verified live: SSH connected first try on the
  production image with only `PUBLIC_KEY`, no account key. The old plan's manual
  "show the key + link to the RunPod SSH page" step was dropped — `PUBLIC_KEY`
  makes it unnecessary. (RunPod also honours an `SSH_PUBLIC_KEY` override for
  custom-command images; we use `PUBLIC_KEY`, which the pytorch base image reads.)
- **Pod cold-start is slow — mostly network-volume read throughput.** The ~239 s
  engine load is dominated by reading the 16 GB DiT from the **network volume**
  (NFS-like, far slower than local NVMe) plus a hash-validation pass that reads it
  *again*. Mitigations to try: copy models volume→local container disk once on pod
  start then load locally; **skip the safetensors hash-validation** on a trusted
  volume; keep the worker resident so the load is paid once per pod, not per image.
- **Warm upscale throughput — RESOLVED (the 78 s was cold-start).** Measured via
  the resident worker (`bench`): cold image #1 ≈ 41 s, then **warm ≈ 7.6 s/image at
  1080** (1620×1080) and **≈ 13.4 s/image at 4K-class** (3240×2160). So the 78 s
  smoke number was one-time warmup (CUDA/cuDNN + first-run Blackwell kernel JIT),
  paid once by the resident worker and amortised over the queue. The pod 5090 at
  ~13.4 s/4K beats the local 3090 (17–19 s) and is close to a local 5090 (~10 s).
  Engine load also dropped from 239 s (first ever) to **97 s** once the volume
  held the validation cache.
- **SageAttention experiment — marginal with the pip version.** PyPI
  `sageattention` is the old **v1.0.6 (INT8)**; with SeedVR2's `attention_mode=
  sageattn_2` it gave only **12.9 s vs 13.4 s** at 4K (~4%). The real speedups
  need **SageAttention 2 (FP8)** or **3 (Blackwell FP4)**, neither on PyPI
  (`sageattn3` does not exist as a package) — they're **source builds** from
  thu-ml/SageAttention (nvcc compile, ~10 min, some risk). Deferred as an OPTIONAL
  tuning pass: the pod 5090 already does ~13 s/4K vs the user's 3090 at 17–19 s
  (~30% faster) without it, so it doesn't block the overnight-run goal. (v1.0.6 is
  now installed in the volume venv; the worker accepts an `attention_mode`
  override, e.g. `worker sageattn_2`.)

## Architecture: what changed, and what still maps

The old model: run the tool **locally**, SSH-tunnel into a service on the pod,
let the pod's GPU do the work over the tunnel.

- **Upscaling — must be redesigned.** The old script ran `batch_upscale.py`
  locally against **ComfyUI on the pod** (HTTP, port 8188). The current upscaler
  loads SeedVR2 **in-process**, so the GPU work happens wherever the script runs.
- **Tagging — pattern still works.** Tagging talks to Ollama over a URL. The old
  script tunnelled to **Ollama on the pod** (port 11434) and ran
  `tag_and_rename.py` locally against it. The same thing works today with **no
  code change**: start Ollama on the pod, open `ssh -L 11434:localhost:11434`,
  and set **Settings → Ollama URL** to `http://127.0.0.1:11434`.

Prefer building any new version **inside the app** (GUI/Settings, Python,
cross-platform) rather than as standalone PowerShell.

## Decided design (0.3.1-experimental)

Settled when groundwork started — drives every phase below:

- **Create-on-demand, disposable pods.** The app creates a fresh pod via
  `POST /pods`, runs, then **terminates** it. No manually-managed long-lived pod.
  This is what makes the watchdog teardown (below) cheap: a degraded pod is
  thrown away, not nursed.
- **Streaming, one image at a time — *not* a bulk transfer.** Production sets are
  tens of thousands of files / many GB, so neither "upload everything first" nor
  a network volume fits. Instead the **queue, resume-cache, skip logic,
  film-strip and watchdog all stay local**; for each image the local orchestrator
  uploads one source copy → the pod upscales it → downloads the one result →
  next. A dropped connection loses one in-flight image, not a multi-GB transfer,
  and results land locally immediately.
- **The pod is a resident *worker*, not a batch runner.** SeedVR2's ~16 GB model
  load can't be per-image, so the pod runs a small long-lived process that loads
  DiT/VAE **once** and serves single images (over an `ssh -L` tunnel). This is a
  thin, single-purpose service — *not* the full HTTP UI mirror of roadmap #2.
- **`RemoteUpscaleEngine`, same interface as `UpscaleEngine`.** Remote vs local
  is a config switch in `batch_upscale.py`; the batch loop barely changes. The
  watchdog keeps working unchanged — it now times the full remote round-trip per
  output-MP, which also catches network stalls. **"Never touch source" is trivially
  kept** (only a copy is ever uploaded).
- **Watchdog = pod health signal.** On `DEGRADED`, tear the pod down and
  re-provision a fresh one; the local resume-cache continues the queue. (Locally
  the same signal just auto-stops for a reboot.)
- **Dead-man's switch is the organising constraint.** A billed pod must never be
  left running. The app's after-run auto-stop is the *fast* path; a self-stop
  deadline **enforced on the pod** (max-runtime + idle-timeout) is the
  *guaranteed* path that survives a dropped connection or a crashed controller.
  Build the stop path first.
- **0 disables a limit (0.3.2).** `deadman.evaluate` treats `max_runtime=0` (and
  `idle=0`) as "no limit" — so a long overnight run can set **max-runtime 0** and
  rely on the **idle timeout** as the safety net (the worker touches a heartbeat
  per image, so idle never fires during active work). The GUI warns if BOTH are 0
  (no auto-stop at all → a crash leaves the pod billing).
- **A dead-man's-switch stop ends the run gracefully (0.3.2).** When the pod stops
  mid-run, the next image's request fails with a connection error. The runners
  used to treat that as a recoverable local *outage* and pause forever (the
  GUI can't resume a dead pod). Now, on a failure, `batch_upscale` /
  `tag_and_rename` check the pod's status (`_remote_pod_stopped` → `pod_status`);
  if it is gone/EXITED/TERMINATED they **end the run cleanly** (save the resume
  cache, amber notification, skip the rescan) instead of pausing — a re-run
  continues the queue. A still-RUNNING pod keeps the normal outage path.

## Heavyweight models live on a persistent network volume

Disposable pods must **not** re-download the models on every launch — that burns
GPU-hour time on a pure download. The fix (decided 2026-06-20):

- **One persistent RunPod network volume holds the heavyweight data** — SeedVR2
  weights (~16 GB) and the Ollama vision model (~6 GB) — written **once** and
  mounted on every disposable pod. The on-pod worker loads SeedVR2 from the
  mounted path; Ollama points its model dir there too.
- This is **not** a reversal of the "no network volume for images" decision: that
  was about the *image data* (tens of thousands of files, streamed one-by-one).
  This volume is write-once / read-every-pod — the opposite access pattern.
- **Cost is trivial:** RunPod charges **$0.07/GB/mo** (under 1 TB), so ~22–25 GB
  ≈ **~$1.6/month** — negligible against even one hour of GPU time, and it removes
  the ~22 GB cold-download from every pod start.
- **Caveat — network volumes are region-locked.** The volume is pinned to one
  data center, so every pod that mounts it **must** be created in that same data
  center. `dataCenterIds` on `create_pod` is therefore *derived from the volume's
  region*, not a free choice. `runpod.network_volume_id` is in `config.json`
  (Phase 2 wires create_pod + a one-time "provision the model volume" flow:
  create volume → spin a pod with it mounted → download models → done).

## Install modes & a first-run wizard (bootstrap rework — late phase)

Once remote upscaling works end-to-end, the **bootstrap/installer should branch
on how the user intends to run**, so someone without a capable GPU isn't forced
through the ~20 GB local download. A first-run wizard records the mode in config:

- **Local** (current default): full `bootstrap.ps1` — Python, **PyTorch CUDA
  (~3 GB)**, **SeedVR2 engine + weights (~16 GB)**, Pillow, paho-mqtt. Needs an
  NVIDIA GPU (8 GB+).
- **Remote:** lightweight — Python, Pillow, paho-mqtt, the RunPod control plane;
  OpenSSH ships with Windows. **Skips torch CUDA and SeedVR2 weights** (GPU work
  is on the pod). A ~20 GB install becomes tiny; no GPU required.
- **Both:** full local bootstrap + the remote control plane available.

Wrinkles to honour when this is built:

- **Auto-straighten needs torch.** `orientation.py`'s CNN runs *locally* today,
  before upscaling. Remote-only has no local torch, so the straighten step must
  **move onto the pod worker** (straighten → upscale in one round-trip), for both
  Batch Upscale and Tag & Rename. So "remote-only" is only viable once the worker
  straightens — which is why the wizard lands *after* Phases 2–3, not now.
- **`upscale_engine` import must stay lazy/conditional.** `batch_upscale.py` must
  not import the SeedVR2 engine (torch) when running in remote mode, or a
  torch-less install can't launch. Select Remote vs Local engine before importing.
- **Tagging in remote mode — DONE (0.3.2), now automated.** The GUI sets
  `IMGTBX_TAG_REMOTE=1`; `tag_and_rename._setup_remote_tagging()` starts a
  `RemoteSession(mode="tag")` which: starts the worker in **tag mode** (skips the
  SeedVR2 load, serves `/orient` only — leaves the VRAM for Ollama), starts
  `ollama serve` on the pod (models from the volume; the ollama **binary** is
  cached on the volume by provision.sh, with an install-if-missing fallback),
  opens a **second ssh -L tunnel** to 11434, and exposes `session.ollama_url`.
  tag_and_rename then repoints `OLLAMA_URL` at the tunnel and routes
  auto-straighten detection to the pod's `/orient` (rotation stays local PIL).
  bootstrap still never installs Ollama locally. **Known v1 gap:** no remote
  telemetry row for tagging yet (the upscale path has one).
- **Tag GPU tier — cheap card + fallback chain (0.3.2, benchmarked live).** The
  vision model uses only **~6.6 GB** VRAM (measured), so tagging runs on a cheap
  card, NOT the upscale GPU. `runpod.tag_gpu_type_id` (Settings → "Tag GPU")
  picks the primary; `remote_run` builds an ordered fallback chain from the
  curated `TAG_GPU_TYPES` (RTX 2000 Ada → A4000 → A4500 → RTX 4000 Ada, all
  16–20 GB, EU-available, ~$0.24–0.26/h). **Benchmark (RTX 2000 Ada, 16 GB,
  EU-RO-1):** session up ~32 s, cold inference 24.4 s (model load), **warm
  ~2.6 s/image**, VRAM 6.6/16 GB, 37 °C — ~3.5–4× cheaper/hour than the RTX 5090
  ($0.99/h) for near-equivalent tag throughput.
- **Pillow and the comparison view stay in every mode** — the GUI needs Pillow,
  and the before/after wipe compares the local original against the locally
  downloaded result, so it works unchanged for remote runs.

### Status

- **Phase 0 (done):** control plane + config + Settings. `scripts/runpod_client.py`
  wraps the REST calls (stdlib `urllib`); `config.json` has a `runpod` section;
  Settings → *Remote upscaling (RunPod)* holds the API key (with a **Test**
  button), hourly rate, and the dead-man's-switch limits. Nothing is provisioned
  yet — Test only lists pods (free).
- **Phase 1 (in progress):** the on-pod dead-man's switch — `pod/deadman.py` is
  done (self-stops on max-runtime/idle via `runpodctl stop pod $RUNPOD_POD_ID`,
  pure tested `evaluate()`; the user's API key never lives on the pod). Remaining:
  the local after-run auto-stop with a cancel countdown (wired in with the run
  flow, Phase 3). The on-pod half is fully exercisable only once Phase 2
  provisions a real pod, but its decision logic is verified off-pod (`--selftest`).
- **Phase 2a (done):** region-aware provisioning groundwork. `runpod_client` has
  network-volume CRUD (`/networkvolumes`, verified live) + curated `GPU_TYPES` and
  EU-only `EU_DATACENTERS` enums (the REST API has no list endpoint). Settings now
  has a GPU-type picklist, an **EU data-center picklist (defaults to EU-RO-1)**, and
  a model-volume row (Refresh lists, Create makes one in the chosen DC with a cost
  confirmation). No pod spun up.
- **Phase 2b (done — validated live on an RTX 5090 in EU-RO-1):** provision +
  one-image upscale proven end-to-end. `runpod_client` helpers `wait_until_running`
  / `ssh_endpoint` / `volume_region`; `scripts/runpod_provision.py` dev driver
  (create/status/probe/provision/smoke/ssh/terminate); `pod/provision.sh` filled
  the volume (16 GB weights + 5.6 GB Ollama + 2.6 GB venv + 50 MB engine ≈ 24 GB);
  `pod/upscale_one.py` upscaled 900×600 → 1620×1080 via the **unchanged**
  `UpscaleEngine`, models loaded from the volume, result fetched back. Pod then
  terminated; the volume persists for the next pod.

  **Hard-won provisioning lessons (baked into provision.sh / the driver):**
  - **Base image:** `runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204` (the
    plausible `2.8.0-...-cuda12.8.1-...` tag does NOT exist on the registry —
    `create` failed cleanly with RunPod's own message, no pod leaked). Ships
    Python 3.12, torch 2.9.1+cu128, torchaudio 2.9.1 — but **no torchvision**.
  - **Torch pinning is essential.** Installing the seedvr2 deps naively makes
    `timm`/`torchvision` drag in a newer torch (2.12 + CUDA 13) that mismatches
    the image's torchaudio 2.9.1 → `libtorchaudio.so: undefined symbol` at import
    (diffusers→transformers→torchaudio). Fix: a venv with `--system-site-packages`
    + a constraints file pinning `torch==2.9.1` + install matching
    `torchvision==0.24.1` from the cu128 index. Final: torch/tv/ta all 2.9.1+cu128.
  - **Volume mounts at `/workspace`.** Models auto-download there via the engine's
    `download_weight` (skips if present, so later pods reuse them).
  - **Cold start ≈ 5 min per fresh pod:** ~239 s engine load (incl. one-time 16 GB
    safetensors hash-validation) + ~78 s first-image warmup (no Sage/Flash attn;
    sdpa). **Phase 3's resident worker pays this once and amortizes it over the
    whole queue** — and we may skip the hash-validation on a trusted volume.

### Provisioning architecture (Phase 2b)

- **Everything heavy lives on the network volume** (mounted at `/workspace`): the
  Python venv (torch CUDA + seedvr2 requirements), the SeedVR2 weights, and the
  Ollama models. A one-time provisioning builds them once; every disposable pod
  just mounts the volume and starts fast — no ~20 GB reinstall/redownload per pod.
- **Models auto-populate.** `UpscaleEngine.__init__` calls
  `src.utils.downloads.download_weight(..., model_dir=...)`, which downloads the
  weights to `model_dir` if missing. Point `model_dir` at a path on the volume and
  the **first run fills the volume**; later pods find them present. Same idea for
  Ollama via `OLLAMA_MODELS` on the volume. So there is **no separate model
  uploader** — provisioning just sets the paths and triggers one download.
- **The worker reuses `UpscaleEngine` unchanged.** `pod/worker.py` is a thin
  resident wrapper: load the engine once (models from the volume), then serve one
  image per request, touching the heartbeat `deadman.py` watches.
- **Connectivity.** Pod is created with `ports: ["22/tcp"]`; only port 22 is
  public (`publicIp:portMappings["22"]`). The worker binds localhost on the pod
  and is reached through an `ssh -L` tunnel — never exposed publicly. SSH auth is
  the dev box's `id_ed25519_runpod` key (public half added to the RunPod account).
- **Region.** The pod's `dataCenterIds` is derived from the volume's region
  (`volume_region`), keeping pod and volume co-located (EU for this user).
- **Phase 3 (core built + validated live):** `pod/worker.py` — resident worker,
  loads `UpscaleEngine` once, serves one image per HTTP request over localhost
  (reached via `ssh -L`), touches the deadman heartbeat. `scripts/remote_upscale_engine.py`
  — `RemoteUpscaleEngine` (same interface as `UpscaleEngine`) opens the tunnel and
  streams each image. Dev driver gained `worker` (start it; kills a prior one by
  **pidfile** — not `pkill -f worker.py`, which matches the launching shell) and
  `bench` (cold-vs-warm timing). Also `runpod_client.create_pod_resilient` — a
  **deploy watchdog**: each pod gets a deploy budget (240 s); on timeout or early
  EXIT it terminates the bad pod and tries a fresh one (RunPod sometimes hands out
  pods that never finish deploying), up to N attempts.
- **Phase 3 integration (done — real batch validated):** `scripts/remote_run.py`
  (`RemoteSession`: create pod via watchdog → push engine/worker/deadman → start
  worker → **arm the on-pod dead-man's switch** → hand back a connected
  `RemoteUpscaleEngine` → terminate on close; has an `attach` mode for dev).
  `batch_upscale.py` selects the remote engine when `IMGTBX_UPSCALE_REMOTE=1`
  (queue/resume/skip/watchdog stay local) and tears the pod down via `atexit`.
  Validated: a real 7-image batch ran on the pod at ~12 s/image (4K), results
  written locally, 0 failed.
  **SSH gotchas solved:** (1) a pod self-terminates via the REST API with the key
  ON the pod — REST-created pods have NO pre-authed runpodctl and no
  $RUNPOD_POD_ID, so key-on-pod is unavoidable (written to a 0600 file; use a
  SCOPED key in prod). (2) a backgrounded daemon must be launched as
  `setsid sh -c '…' </dev/null >log 2>&1 &` with the redirect directly on the
  backgrounded command — a `cd && …` wrapper (or missing redirect) keeps the ssh
  channel open and the call hangs.
  Remaining for Phase 3: the GUI "Run on remote pod" toggle, `DEGRADED`
  teardown/re-provision, cost embed.

## RunPod REST API

- Base URL: `https://rest.runpod.io/v1`  ·  OpenAPI: `…/v1/openapi.json`
- Auth header: `Authorization: Bearer <api_key>`
- Endpoints **verified against the live OpenAPI spec (2026-06)** and wrapped in
  `scripts/runpod_client.py`:

  | Method | Path | Use |
  |---|---|---|
  | `GET`    | `/pods`             | list (filter by `desiredStatus`, etc.) |
  | `GET`    | `/pods/{id}`        | status — `desiredStatus` ∈ `RUNNING`/`EXITED`/`TERMINATED` |
  | `POST`   | `/pods`             | create: `gpuTypeIds`, `imageName`/`templateId`, `networkVolumeId`, `containerDiskInGb`, `ports`, `env`, `dataCenterIds` |
  | `POST`   | `/pods/{id}/start`  | start / resume |
  | `POST`   | `/pods/{id}/stop`   | stop (storage still billed while stopped) |
  | `DELETE` | `/pods/{id}`        | terminate (frees all billing) |

- The exact `gpuTypeIds` string (e.g. for a 5090) should be read from the GPU
  types endpoint at provision time rather than hard-coded — the config default is
  a best guess to be confirmed in Phase 2.

## Live GPU availability + pricing (GraphQL, 0.3.3)

The REST control plane exposes **no** GPU-types/pricing/stock endpoint — that's
why the picklists (`GPU_TYPES`, `TAG_GPU_TYPES`) were hand-curated, and why they
went stale: on 2026-06-22 **all four** curated tag cards (RTX 2000 Ada / A4000 /
A4500 / 4000 Ada) were out of stock in EU-RO-1, so a non-technical user couldn't
tag at all and the app gave no hint why.

The **GraphQL** endpoint has the data the REST one lacks:

- `POST https://api.runpod.io/graphql` · `Authorization: Bearer <key>` ·
  **must send a browser `User-Agent`** (Cloudflare 403s an unknown one).
- Query `gpuTypes { id displayName memoryInGb
  lowestPrice(input: {gpuCount: 1, secureCloud: true, dataCenterId: $dc}) {
  uninterruptablePrice stockStatus } }`.
- `id` is exactly the `gpuTypeIds` value `create_pod` wants
  (`"NVIDIA GeForce RTX 5090"`); `displayName` is the short name.
- `lowestPrice.uninterruptablePrice` = on-demand $/h in that DC;
  `stockStatus` ∈ High/Medium/Low when deployable, **null = out of stock there**.

**Gotcha — GraphQL catalog ≠ REST create enum → deploy via GraphQL.** The GraphQL
`gpuTypes` list is a *superset* of the GPU ids the REST `POST /pods` endpoint
accepts. Newer cards (seen 2026-06-22: `NVIDIA RTX PRO 4500 Blackwell`,
`NVIDIA RTX PRO 4000 Blackwell`) appear in GraphQL with live stock + price but
REST `create_pod` **rejects them with HTTP 400** ("value must be one of …").

First attempt was to intersect availability with the REST enum (`CREATABLE_GPU_IDS`,
dumped via `POST /pods` `gpuTypeIds=["__invalid__"]`). But that *hides* cheap,
available cards the website happily deploys — so instead **pod creation moved to
the GraphQL deploy path** (`runpod_client.deploy_pod` → `podFindAndDeployOnDemand`,
the same mutation the RunPod console uses). It accepts the full catalog, so the
picker shows everything in stock (incl. RTX 2000 Ada ~$0.24) and `available_gpus`
no longer filters. `CREATABLE_GPU_IDS` stays only as documentation.

**Two GraphQL-deploy gotchas that cost a debugging round (both verified live):**

1. **`volumeMountPath` is mandatory with a network volume.** REST defaults it to
   `/workspace`; GraphQL does not, and omitting it makes the container fail at
   create with `invalid mount config for type "bind": field Target must not be
   empty`. The pod shows `desiredStatus: RUNNING` but the container never starts —
   so it **never gets a public IP / port-22 mapping** and SSH never comes up. Pass
   `volumeMountPath: "/workspace"`, `volumeInGb: 0`, `networkVolumeId`.
2. **`supportPublicIp: true` + `ports: "22/tcp"`** (a *string*, comma-separated —
   not the REST array) are needed for the direct-TCP SSH endpoint the app relies
   on (`publicIp` + `portMappings["22"]`, which appear ~90 s after RUNNING).

A failed-mount deploy can also leave an **auto-replacement pod** whose id the
mutation never returned — diag/teardown must sweep by **name**, not just the
returned id, or a replacement lingers and bills.

**Gotcha — CUDA driver mismatch → `allowedCudaVersions` deploy filter.** The pod
image (`runpod/pytorch:…cu1281…`) needs host driver CUDA **≥ 12.8**. RunPod
machines vary: benchmarking landed a CUDA-**12.7** RTX 4090 whose container never
started (`nvidia-container-cli: requirement error: unsatisfied condition:
cuda>=12.8`), and the doomed pod burned the whole fallback chain (2×240 s waits ×
each GPU). `deploy_pod` now passes `allowedCudaVersions` so the pod only lands on
a machine that can run the image. Verified-live facts:

- the field is **exact-match set membership**, not a `>=` range — so to mean
  "≥ 12.8" you must enumerate every version ≥ 12.8 (`runpod_client.allowed_cuda_versions`
  derives this from the image's `cuXYZ` tag against `KNOWN_CUDA_VERSIONS`, listing
  a couple of not-yet-existing future versions so a newer host isn't excluded).
- it turns a *random* failure (you might get a 12.7 host and crash) into reliable
  success — you only ever get a ≥12.8 host — or a clean, fast supply-constraint
  fallback. It can't conjure compatible machines where none exist for that GPU.

**Limit / future work:** this makes the cu128 image *reliable*, but doesn't let an
older-driver-only machine run it. To actually use cards whose only hosts are on
<12.8 drivers you'd need a **lower-CUDA image** — which for tagging is viable
(only Ollama + the orientation CNN, and orientation falls back to CPU via
`torch.cuda.is_available()`), but the on-pod venv is built with
`--system-site-packages` against the image's exact torch (2.9.1+cu128, see
provision.sh), so a different image needs the **volume re-provisioned** with a
matching/own torch. Deferred — the filter already makes the current image
reliable.

**Cost guardrail — per-task price ceiling.** The picker's selection seeds a
price-ordered fallback chain, but the *automatic* part is capped at a configurable
hourly ceiling (0 = no cap). Live testing hit the failure this prevents: a picked
RTX 4000 Ada was sold out at create, the chain fell through the phantom Blackwells
(400), and landed on an **A100-SXM4-80GB at $1.49/h** — wildly overkill for
tagging. With the ceiling, only cheaper in-stock cards are tried automatically; the
user's own explicit pick is honoured regardless (its price is shown in the confirm,
flagged when above the ceiling).

The ceiling is **split by task** (0.3.4) — `runpod.max_price_per_hour_upscale`
(default **$1.10/h**) and `runpod.max_price_per_hour_tag` (default **$0.50/h**).
Benchmarks forced this: tagging runs fine on $0.24–0.39 cards, but the cheapest
viable *upscale* card is the RTX PRO 4500 at **$0.74** — already above a $0.50 cap —
so a single shared cap left an upscale run with **no automatic fallback at all**
(it would be refused if the hand-picked card sold out). The upscale default of
$1.10 covers the RTX 5090 value pick (cheapest *per run* for upscaling, ~$0.36/100
images) while still blocking a runaway A100/B200. Each tab carries its own ceiling
(`_gpu_price_key`/`_gpu_price_default` → `_fallback_ceiling` in `toolbox_gui.py`);
the pre-0.3.4 single `runpod.max_price_per_hour` key is deprecated (ignored, and
dropped from config on the next Settings save).

Wrapped as `runpod_client.available_gpus(api_key, data_center_id, min_memory_gb)`
→ in-stock GPUs ≥ the VRAM floor, **sorted by price ascending**. The GUI's tab
pickers call it (off-thread) for the volume's region (≥32 GB upscale, ≥16 GB
tag), default to the persisted preference when in stock else the cheapest, and
pass the selection + a price-ordered fallback chain to `RemoteSession` via the
`IMGTBX_GPU_OVERRIDE` env. Availability fluctuates minute-to-minute, hence the ↻
refresh button. Surprise from the first run: RTX PRO 4500 (32 GB, $0.74) was
cheaper than the RTX 5090 ($0.99) for upscaling — exactly the "is a pricier pod
more cost-efficient per image?" question worth benchmarking.

**World-wide region + data-center picker (0.3.4).** The early Settings DC picker
was an **EU-only** curated enum (`EU_DATACENTERS`, defaulting to EU-RO-1) — fine
for the original user (Romania) but wrong for anyone else. GraphQL also exposes
the full data-center catalog: `{ dataCenters { id location storageSupport listed } }`
(`runpod_client.data_centers`). Crucially it reports **`storageSupport`** — a
network (model) volume can only live in a DC that supports storage, so the picker
offers **storage-capable, listed DCs only**, preventing a user from provisioning a
volume somewhere no pod can then attach it. Settings now has a **Region**
combo (Europe / North America / Asia / Oceania, derived from the id prefix via
`runpod_client.region_of`) feeding a **Data center** combo, with a Refresh that
pulls the live list and a one-line "Volume actions act in: <region> · <dc>" target
readout. A curated `DATACENTERS` fallback (storage DCs as of 2026-06) makes it work
offline. Selecting an existing model volume **syncs the picker to that volume's
region** (it's region-locked, so that's where Create/Provision and pods run).
**Storage support is the hard filter**: a region or data center with no
network-volume storage is simply **never populated** — only regions with at least
one storage-capable DC appear, and the DC combo lists only storage DCs. So
Oceania (whose only DC, OC-AU-1, reports `storageSupport:false` — a compute-only
data center that shows GPUs on the RunPod website but can't host a network volume)
just doesn't show up. Simple, no confusing "exists but unusable" message.
**Volumes are scoped to the selected data center**: the Region/DC **Refresh also
re-lists model volumes**, showing only the one(s) in that DC or a
**`None | <data center>`** placeholder when there isn't one — so the volume shown
always matches where a pod would run. The volume combo is read-only (pick, don't
type); the chosen volume is persisted with its **full display label**
(`network_volume_label`, alongside the bare `network_volume_id` the run code uses)
so it reads in full on the next launch instead of showing only the raw id.
**The Refresh also fills the Upscale/Tag GPU preference combos** with the GPUs the
selected data center offers + live price (`available_gpus(..., include_out_of_stock=True)`,
a preference list so a momentarily sold-out card still shows), partitioned by the
VRAM floor (>=32 GB upscale, >=16 GB tag) and defaulting to RTX 5090 / RTX 2000 Ada
(or the current pick if still offered, else cheapest). The Upscale GPU, Tag GPU and
Model volume comboboxes share one aligned grid column, stacked below the
Region/Data center row with the volume action buttons beneath them.

## `runpod` config section (shipped in 0.3.1)

The block now in `config.json` (`api_key` blank in the tracked template — a
credential, same rule as the `mqtt` block):

```jsonc
"runpod": {
    "api_key": "",                  // RunPod REST key; local only, never committed
    "gpu_type_id": "NVIDIA GeForce RTX 5090",
    "image_name": "",               // or template_id; set in Phase 2
    "template_id": "",
    "data_center_ids": [],
    "container_disk_gb": 30,
    "ssh_key_path": "%USERPROFILE%\\.ssh\\id_ed25519_runpod",
    "worker_port": 8200,            // on-pod upscale worker, tunnelled via ssh -L
    "hourly_rate": 0.90,            // USD/h, for cost estimates
    "stop_pod_when_done": true,
    "terminate_when_done": false,   // true = disposable: delete, don't just stop
    "max_runtime_minutes": 720,     // dead-man's switch, enforced on the pod
    "idle_timeout_minutes": 15
}
```

## SSH connectivity + tunnel

```powershell
# Connectivity check (also confirms the GPU)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no root@<host> `
    "echo connected && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

# Tunnel a pod service to localhost (Ollama shown; ComfyUI was 8188)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no -o ServerAliveInterval=30 `
    -L 11434:localhost:11434 -N root@<host>
```

## Cost tracking + auto-stop safety

The most valuable bit: never leave a billed pod idle.

- Track `sessionElapsed = end - start`; `cost = round(hours * hourly_rate, 2)`.
- After the run, **stop the pod automatically** with a short cancel countdown
  (old default: 60 s, cancel on Escape). Always close the SSH tunnel regardless.
- If the API stop call fails, tell the user to stop it manually and show the
  pod ID — don't fail silently.

## Completion notification

Send on completion (green `0x2ecc71` / `3066993`), with fields:
duration, estimated cost, hourly rate, processed count, average time per image,
pod ID, completed-at timestamp. The runners call `send_notification(...)` →
`notifications.notify(...)` (0.3.8), which fans out to every configured backend
(Discord webhook + Telegram bot + ntfy); it already covers the general case, so remote
runs would just add the cost/pod fields. Telegram has no embed colours, so the
green status colour shows as a leading emoji there, and on ntfy as an emoji tag
plus priority.
