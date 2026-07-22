# Dropped ideas & constraints

Ideas that were investigated and **decided against**, plus the standing
constraints that rule whole classes of feature out. Kept because the reasoning
is the valuable part: without it the same idea comes back every few months and
gets re-investigated from scratch.

Nothing here is scheduled. If one is revisited, the trigger is named in its
entry ("revisit only if ...").

Sources: `docs/future-features.md` (open roadmap) and
`docs/coarse-ideas-plan.md` (the 2026-07 coarse-idea investigation).

---

## Contents

- [Deferred local-engine install (coarse idea #2)](#deferred-local-engine-install-coarse-idea-2-2026-07-21)
- [Parallel jobs: an image tool + the Video Upscaler (coarse idea #3)](#parallel-jobs-an-image-tool--the-video-upscaler-coarse-idea-3-2026-07-21)
- [Pause for the Video Upscaler](#pause-for-the-video-upscaler-2026-07-21)
- [Region pre-seed at first-run bootstrap](#region-pre-seed-at-first-run-bootstrap)
- [Automatic run-telemetry reporting](#automatic-run-telemetry-reporting-coarse-idea-4-phase-2)
- [Standing constraints](#standing-constraints)

---

## Deferred local-engine install (coarse idea #2, 2026-07-21)

**The idea.** Stop shipping the ~5 GB GPU stack up front: install torch CUDA +
seedvr2 on demand, the first time the user starts a LOCAL run, so a "Both"
install starts as light as Remote.

**Investigation result (it answers the original question).** The environment is
only heavy for on-device GPU work, and only torch is heavy. Measured on the dev
venv (2026-07-10): 4.68 GB total, **torch alone 4.21 GB (90 %)**; everything
else together ~470 MB. torch CUDA is irreducible (the wheel bundles the
cuDNN/cuBLAS/CUDA runtime DLLs, no slimmer official Windows build), and local
Tag & Rename needs it too (the `orientation.py` CNN). **Remote-only installs are
already light**: `bootstrap.ps1` in remote mode installs only pillow, piexif,
paho-mqtt, python-vlc, well under ~150 MB.

**Why dropped.**

1. The size problem does not exist where it would hurt: remote-only is already
   small, and a user who chooses Local is choosing the GPU stack.
2. Deferring it puts a multi-GB download **after** the user presses Start. That
   is the wrong moment: installation belongs at the very beginning, not as a
   surprise wait in front of the first run.

**What survives.** The messaging, which is strings only: the installer's
install-mode page and the first-start wizard should state the consequence
plainly ("Local processing adds a ~5 GB AI engine download; Remote keeps the
install under ~300 MB").

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Parallel jobs: an image tool + the Video Upscaler (coarse idea #3, 2026-07-21)

**The idea.** Let one image-side tool and the Video Upscaler run at the same
time, behind a GUI-enforced compatibility matrix.

**Investigation result.** The *pipeline* is already safe for it: per-tab
subprocesses with their own stop channels, log files, film strips and telemetry
rows; SQLite is multi-process safe (WAL, separate table families); pod names are
mode-aware (`image-toolbox-*` vs `video-toolbox-*`, 0.4.3) so an image run and a
video run can never fight over one pod. The conflicts were all reporting/UX: one
taskbar progress slot for two runs, the single MQTT `task/*` namespace
interleaving two runs, and two `RemoteSession` funds guards whose start-time
floor preflights do not know about each other.

**Why dropped.**

1. **Risk to a non-technical user outweighs the gain.** Two concurrent runs mean
   two ways to run out of money, two progress readouts fighting over one
   taskbar, and a Home Assistant view that needs re-architecting; a confusing
   state is a real cost, a parallel run is a convenience.
2. **The complexity is not paid for.** The compatibility matrix, per-tool MQTT
   namespaces and a joint funds estimate are permanent surface area for a
   workflow that is only occasionally wanted.
3. **The app has since gone the other way, deliberately.** 0.5.2 locks all other
   tabs while any run is active (run exclusivity). One run at a time is now the
   stated model, and it is the simple, predictable one.

Revisit only if run exclusivity itself proves to be the wrong call.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Pause for the Video Upscaler (2026-07-21)

Pause exists so the user can reclaim the GPU without losing the queue, and it can
only act at a safe boundary. For stills that boundary is the gap between images
(seconds); for video it is the gap between segments (minutes to hours), so the
button would not act when pressed, and acting mid-segment means discarding
partial work or building frame-level checkpointing. Even a two-second clip is
~50 frames. Stop already covers the need: a stopped run resumes at the first
unfinished **segment** (`db.py` `video_*` tables), which is the same machinery
the per-run minute/cost cap uses.

Consequence: "a pause frees every resident model" (0.5.2) applies to the Batch
Upscaler and Tag & Rename; the Video Upscaler has no pause at all.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Region pre-seed at first-run bootstrap

The idea was to ask the user's region during install and pre-seed
`data_center_ids`. After repeatedly checking the live list, there are so few
regions/data centers that auto-detecting one adds little: the Settings Region/DC
picker already lets the user pick directly, which is clearer than guessing for
them.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Automatic run-telemetry reporting (coarse idea #4, phase 2)

The community-database idea shipped as roadmap **#8, Benchmark sharing** (0.5.1),
but only its zero-infrastructure half: a curated CSV in the repo, auto-downloaded
at launch, contributed back through a browser-delegated GitHub issue, curated by
the maintainer `bench_share.py --merge` tool.

Dropped from the original plan:

- **Automatic post-run submission to an author-owned HTTPS endpoint** (a
  Cloudflare Worker + D1/R2 was the shape). It buys automation at the price of
  exactly the infrastructure the zero-infra design exists to avoid.
- **Per-run telemetry payloads with a random install-UUID**, and the wizard
  consent step / "preview what would be sent" dialog they required. The shared
  unit became a per-card measurement row, which carries no identifier, so the
  anonymization problem dissolved instead of needing to be solved.
- **A repo-side issue-form template + GitHub Action** parsing issues into a CSV.
  The maintainer merge tool (newest-wins dedupe + a physical-plausibility sanity
  gate + a reviewable git diff) replaced them and is stricter.
- **Seeding `db.gpu_perf` from imported rows.** `gpu_perf` is an accumulating
  store, so an imported rate would pollute the user's own measured average; the
  estimator already falls back to the author `RATES` table for an unmeasured card.

The curated CSV plus the curation script suffice for the foreseeable future. See
`docs/benchmark-sharing.md`.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

---

## Standing constraints

- **AMD GPUs (ROCm): not supported, filtered out.** The pipeline is CUDA-only
  (PyTorch CUDA build, SeedVR2, the orientation CNN, `nvidia-smi` telemetry), so an
  AMD card can't run any task. RunPod occasionally lists AMD Instinct cards (e.g.
  the MI300X in EU-RO-1, sometimes *cheaper* than comparable NVIDIA), so
  `available_gpus` drops them at the source via `is_amd_gpu` (0.4.0) rather than
  letting a user pick one that fails at run time. A ROCm port would be a separate,
  large effort and is not planned.
- **vast.ai as a second provider: investigated 2026-06-23, not pursued.** The
  goal was provider choice (price/availability/region) behind a thin interface.
  Two billing dimensions RunPod doesn't charge make vast.ai a poor fit for this
  app's stream-one-image-at-a-time, disposable-pod design: **storage** is
  ~$0.33-0.40/GB/mo (RunPod $0.07), and **bandwidth is metered both ways** at
  ~$40/TB (RunPod free), directly taxing the upload-every-image /
  download-every-result flow. It also has **no region-wide network volume**
  (host-local only), which defeats the availability gain that motivated the look.
  Reusable finding: the worker, streaming engine, dead-man's switch, and local
  queue/watchdog are provider-agnostic; a port would be a provider seam
  (`RunPodProvider` + `VastProvider`) plus a GUI selector, the GUI being the
  largest lift. Vet any future provider against this checklist before writing
  code: (a) free/cheap ingress+egress, (b) cheap region-wide persistent storage
  that mounts on disposable instances, (c) reliable SSH with key injection.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>
