# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

One entry is listed first despite being **built**, because it is the only one with **dates**
on it (#25, the RunPod API v2 migration: the code shipped in 0.6.1, but the old transports must
be deleted after **2026-11-15** and in **early 2027**, and nothing else in the project will
remember that). One is small (#24, enriching what a bug report auto-fills). The rest are medium
or larger: one measurement-gated processing capability (#21 denoising, gated on a measurement
that has not been run), a Video Upscaler feature (#12 mixed local+remote queue) and a remote-side
one blocked on funds rather than design (#15 a second GPU provider). Two lower-priority ones each
introduce a new process model, networking, or packaging (HTTP interface #3, Unraid #4). The
**shipped** milestones are kept below as a numbering legend, after the open work.

---

## Contents

- [25. RunPod API v2 migration](#25-runpod-api-v2-migration-built-two-deletions-still-dated) (built; two dated deletions left)
- [24. Make a bug report actionable without asking](#24-make-a-bug-report-actionable-without-asking-small-medium)
- [21. Denoising before upscaling](#21-denoising-before-upscaling-medium-gated-on-a-measurement-deferred)
- [12. Local+remote mixed queue](#12-localremote-mixed-queue-medium)
- [15. Second remote GPU provider (packet.ai)](#15-second-remote-gpu-provider-packetai-medium)
- [3. HTTP interface](#3-http-interface-hard-low-priority)
- [4. Unraid Community Apps integration](#4-unraid-community-apps-integration-hardest-low-priority)
- [Sequencing & dependencies](#sequencing--dependencies)
- [Shipped milestones (numbering legend)](#shipped-milestones-numbering-legend)
- [Decided against / constraints](#decided-against--constraints)

---

## 25. RunPod API v2 migration: BUILT, two deletions still dated

**The migration shipped in 0.6.1** (P0-P4, 2026-08-20): the RunPod integration moved off the two
transports RunPod dated for shutdown onto `https://api.runpod.io/v2`, with v1 + GraphQL kept as a
manual escape hatch. **The record of what was decided, measured and verified is
`docs/runpod-notes.md`** ("The API v2 migration"), per this file's rule that a shipped design
moves to the document that owns the feature.

This entry survives because the milestone is **not finished on a calendar**. Two deletions are
dated, and nothing else in the project will remember them:

| When | Delete | Why it cannot happen sooner |
|---|---|---|
| **After 2026-11-15** | The whole v1 half: `_V1_LIFECYCLE` and the v1 branches in `runpod_client`, `_DEPLOY_MUTATION`, `_GPU_AVAIL_QUERY`, `_DC_QUERY`, `_PODS_MACHINE_SELECTIONS`, `CREATABLE_GPU_IDS`, `KNOWN_CUDA_VERSIONS` + `allowed_cuda_versions` + `deploy_cuda_versions`, the `list_pods_detailed` fallback ladder, and the `runpod.api_version` switch itself | It is the escape hatch for v2 beta churn until v1 stops serving. An escape hatch missing half its code is not one |
| **Early 2027**, when the balance query starts answering 410 | The GraphQL island: `_graphql`, its browser-User-Agent workaround, `_BALANCE_QUERY` | It is the only source of the account balance in either API, and it costs nothing to leave working until it dies |

**The second one retires a feature, and that is the part to get right.** `funds_guard`'s start
floor and balance floor go with the island. They already fail **open**, so nothing breaks on the
day: `floor_unenforced` starts saying the retired wording and the readout starts saying "Funds:
Not published". The one deliberate act needed is **removing the balance-floor field from
Settings** so the app stops offering a guard it can no longer apply. The per-run session cap is
unaffected: it is derived from the pod's own billed rate and needs no balance.

**One thing to check first if anything ever forces a return to the v1 branch:**
`KNOWN_CUDA_VERSIONS` is already stale. Measured 2026-08-20, the catalog offers an RTX 4090 on
CUDA **13.2**, past the end of that hand-written table, so v1 deploys silently exclude every host
on the newest driver. v2 sends a numeric floor instead and cannot go stale.

**Also worth re-reading before either date**, because the whole entry was built on it: RunPod
announced `Sunset` headers and **does not serve them** (measured on both hosts). The dates are
hard-coded and a **410 is the only signal**, which the app turns into "RunPod has retired the API
this version of Image Toolbox uses. Update the app."

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 24. Make a bug report actionable without asking: Small-Medium

Enrich what `gui.common._issue_url` auto-fills, so a terse report still carries enough to
diagnose. There is already an in-app **Report an issue** path that opens a pre-filled GitHub
new-issue page: it fills the app version, the OS, the Python version, the GPU **name**, and a
"please attach" line pointing at the newest crash log.

**The trigger.** A real report, in full: title "not working", body "the output folders seem
empty", plus the auto-filled `GPU: NVIDIA GeForce RTX 2060`. Nothing there is enough to
answer it, yet **the app knew the answer at the time and threw it away**: an empty output
folder is almost always a completed run that skipped everything as already near the target, or
a tree that has been conciliated (both correct behaviour), and the run summary said so. The
user is not going to write that down. The app can.

The premise: **a user who writes two words is the normal case, not a failure of the user.**
The lever is the automated half, and everything below already exists somewhere in the process.

### What to add, in order of what it would have settled

| # | Field | Settles |
|---|---|---|
| 1 | **The last run's summary per tool**: which tool, when, and its counts (processed / skipped / failed / duration), from the same dict already published to MQTT as `last_run` | "The output folder is empty" in one line, without a round trip. The single highest-value item here |
| 2 | **VRAM total, not just the GPU name** (`sample_gpu` already returns it, and a card name does not imply its memory: the RTX 2060 shipped in 6 GB and 12 GB) | Whether the card is under the 8 GB minimum, i.e. `known-defects.md` D4 |
| 3 | **Install mode** (Local / Remote / Both, from `install_mode.txt`) | Which half of the app is even in play. A Remote-only install has no local GPU stack at all |
| 4 | **The relevant settings for the tool that was last used**: Resolution Target, skip-cutoff, model, "Run on" mode | The most common "not working" is a correct run the settings explain |
| 5 | **The ffmpeg build stamp** (`ffmpeg/build.txt`) and whether `.venv` looks healthy | D1 and the vidstab pin, both of which are invisible from the outside and both of which we have now hit |
| 6 | **The tail of the newest run log, inline** rather than "please attach" | Users do not attach files. Bounded, see the cap below |

### Constraints that shape it

- **The URL cap is the real limit.** A pre-filled new-issue URL must stay under ~8 KB
  (`_MAX_ISSUE_URL = 7800` already encodes this for benchmark contributions). So the body gets
  a **budget**, filled in the order above, and the log tail is what shrinks. The benchmark path
  already has the escape hatch to copy: over the cap, fall back to pointing at a file the app
  wrote to disk.
- **A "Copy diagnostics" button is the other half**, and probably the better one: it writes
  the full block to the clipboard with no cap at all, and it is also what a user can paste into
  a forum, a chat, or an email that never becomes a GitHub issue.
- **Nothing may be sent anywhere.** This is a pre-filled browser form the user reads and can
  edit before submitting, which is the whole reason it is safe to put more in it. That
  property must not be traded away for convenience.
- **Paths carry the user's name and their folder structure.** Include what is needed
  (`ffmpeg/build.txt`'s contents, yes; the full path of every photo folder, no), and never any
  value from `config.local.json` (`config_store.SECRET_FIELDS` is the list: API key, MQTT
  password, notification tokens and webhook URLs, where a webhook id IS the credential).
  Redaction has to be a rule about what is collected, not a filter applied afterwards.
- **Where the last-run summaries come from is a design choice**, not obvious. They are
  currently published and forgotten. Persisting a small ring of them (a few rows keyed by tool)
  is one option; scraping the newest per-tool log file is another and stores nothing new.

### The human half, and the trap in it

A GitHub **issue template** would improve the other side, and the repo has none today
(`.github/` holds only workflows). The trap: GitHub's structured **issue forms** (YAML) and a
`?body=` pre-filled URL do not compose the way you would expect, and the app's whole
auto-filled body depends on `?body=`. Check how a form's fields are pre-filled by query
parameter before committing to one, or the enrichment above quietly stops arriving the day a
template lands.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 21. Denoising before upscaling: Medium (gated on a measurement, deferred)

Optionally denoise a source before it reaches the model, as a **checkbox** in the Batch
Upscaler (images) and the Video Upscaler (videos).

> **Deferred 2026-08-19, and the thing it was deferred behind is done.** The RunPod API v2
> migration (#25) took priority because it had a deadline and affected a shipped, money-spending
> feature; it shipped on 2026-08-20. This one is still unproven, so what now gates it is the
> measurement below, not the queue.
>
> **Do not build this before the A/B harness reports.** Unlike everything else on this list,
> the *value* here is unknown rather than the cost. SeedVR2 is already a restoration model
> trained on degraded inputs, so denoising first may add nothing, or may remove detail the
> model would have used as evidence. If the answer is "no visible benefit", this milestone
> moves to `dropped-ideas.md` and nothing is built. **That is a successful outcome, not a
> wasted afternoon.**
>
> The harness is written out in full below, in this tracked file, so the procedure survives
> whatever happens to the untracked scratch notes it used to live in.

### The measurement that gates this: the A/B harness

Nothing here can be done from code. It is downloading sample files, producing comparison
sets, and looking at results. It needs no development time and never had to wait for anything
else on this list.

**What does not exist yet is the third leg.** Originals and their upscaled results already
exist; **denoised-then-upscaled** does not, because the denoise stage is not built. The
script below writes the denoised copies as ordinary files, so the shipped Batch Upscaler can
be pointed at them with no code change at all.

#### Read this first: the seed confound

`upscale_engine.upscale()` draws a **fresh random seed for every image**
(`self.args.seed = random.randint(0, 2**31 - 1)`), and there is no setting to pin it. Two
upscales of the same file therefore differ from each other. Consequences:

- **Do not reuse upscaled files from an earlier run** as the "original" leg. They carry a
  different seed lottery than the denoised leg, and the comparison would measure seed
  variation as much as denoising. Re-upscale the originals in the same session, same
  Resolution Target, same model.
- **Judge across the whole set, not per image.** With 20 images a systematic effect separates
  from per-image seed noise; with 3 it does not. If mild denoising helps, it should help on
  *most* of the set, not spectacularly on one.
- Video does not have this problem: the Video Upscaler uses one fixed seed per source video.

#### Step 1: build the test set

About **20 images** in one folder, chosen deliberately across the three degradation types,
because they are unrelated problems and will not have one answer:

| Pick roughly | Type | What it looks like |
|---|---|---|
| 7 | **Sensor noise** | old digicam or high-ISO shots: random speckle, worst in shadows and flat sky |
| 7 | **JPEG artifacts** | heavily compressed or resaved files: blocking in gradients, ringing around edges |
| 6 | **Scan defects** | scanned prints: dust, scratches, paper grain |

The third group is the **control**. No denoiser touches dust and scratches, so if those come
out looking identical across all three legs, the test is working. If they come out
*different*, something else changed and the run is suspect.

Keep the originals outside any folder the app scans, so nothing is picked up by accident.

#### Step 2: produce the denoised copies

Run with the app's venv: `\.venv\Scripts\python.exe make_denoised.py <originals folder>`.
It writes sibling folders `<src>_denoised-mild` and `<src>_denoised-strong` and never
modifies the sources.

```python
"""Denoised copies of a folder of images at two strengths, for the #21 A/B harness."""
import os, sys, time
import cv2
import numpy as np
from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

# (label, h_luma, h_colour) for cv2.fastNlMeansDenoisingColored.
# MILD is what a shipped feature would plausibly default to: enough to lift speckle, not
# enough to erase fine texture. STRONG exists to show the failure mode, so the amount of
# detail at stake is visible if the default were ever set too high.
STRENGTHS = [("mild", 3, 3), ("strong", 10, 10)]

def main(src_root):
    src_root = os.path.abspath(src_root)
    files = [f for f in sorted(os.listdir(src_root))
             if os.path.splitext(f)[1].lower() in IMAGE_EXTS]
    if not files:
        print(f"No images found in {src_root}")
        return
    print(f"{len(files)} image(s) in {src_root}\n")
    for label, h, hc in STRENGTHS:
        out_root = f"{src_root}_denoised-{label}"
        os.makedirs(out_root, exist_ok=True)
        t0 = time.time()
        for i, name in enumerate(files, 1):
            src = os.path.join(src_root, name)
            # Match the upscaler's own loader (EXIF orientation applied, RGB) so the only
            # difference between the legs is the denoising itself.
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                arr = np.asarray(im)
            den = cv2.fastNlMeansDenoisingColored(arr, None, h, hc, 7, 21)
            dst = os.path.join(out_root, os.path.splitext(name)[0] + ".jpg")
            Image.fromarray(den).save(dst, "jpeg", quality=98, subsampling=0)
            print(f"  [{label}] {i}/{len(files)}  {name}")
        print(f"  -> {out_root}   ({time.time() - t0:.1f}s)\n")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

**One caveat to carry into the judging:** the script writes JPEGs, so the denoised leg takes
one extra encode the original leg does not. At q=98 with no chroma subsampling that is far
below what denoising changes, but it is not *nothing*, and the real implementation would keep
the image in memory and never write it (decision 2, the #19 prepare pipeline). If a
difference looks marginal, this is one reason to distrust it.

#### Step 3: upscale all three folders

Three Batch Upscaler runs, **in the same session with identical settings** (same Resolution
Target, same SeedVR2 model, same skip-cutoff, auto-straighten in the same state): the
originals, the mild copies, the strong copies.

#### Step 4: judge, and write it down

Open each pair in the app's own comparison window, so the images are seen the way a user sees
them. Four questions:

- **Does the denoised leg lose fine texture** the original leg kept (fabric, hair, foliage,
  skin)? That is the cost.
- **Does the original leg show invented texture** where there was only noise? Flat sky, walls,
  shadow. That is what denoising is supposed to prevent, and the reason for decision 1.
- **Is strong distinguishable from mild?** If not, the effect is small and the feature is
  probably not worth building.
- **Do the scanned prints look the same across all three?** If not, the test is broken.

The deliverable is a CSV in the style of `docs/tag-rename-benchmarks.csv`, e.g.
`docs/denoise-benchmarks.csv`:
`file, degradation_type, best_leg(original|mild|strong), texture_lost(0-3),
invented_noise_texture(0-3), notes`.

That file decides this milestone either way, and if the answer is "no", it is also what stops
the idea coming back in six months.

#### The video half

Same shape, cheaper to judge because the video seed is fixed per source. Two or three
genuinely noisy old clips, one mild `hqdn3d` copy of each:

```
ffmpeg -i "in.avi" -vf "hqdn3d=2:1.5:3:2.25" -c:v hevc_nvenc -preset p5 -rc vbr -cq 16 -pix_fmt p010le -c:a copy "in_denoised.mkv"
```

Then run the Video Upscaler on the originals and on the denoised copies, same target and
engine, and compare in the playback window. Do the post-upscale temporal experiment described
at the end of this milestone in the same sitting: it uses the same clips.

### Settled decisions (conditional on the measurement)

| # | Decision | Why |
|---|---|---|
| 1 | **Denoise BEFORE the model, not after** | After the model, the noise is no longer noise: SeedVR2 reads it as evidence of texture and reconstructs **plausible structure** from it at 4x scale, correlated and edge-consistent. A denoiser then has nothing to key on and can only blur everything uniformly. Cost also scales with output pixels (4-16x more), and the pre-split `-vf` seam already exists |
| 2 | **A checkbox in both upscalers, not a tab** | The seams already exist: for images it is a stage in the prepare pipeline #19 built (decode -> straighten -> **denoise** -> upscale, all on one in-memory array), for video it is a `denoise` flag on `SplitPlan` appending to the same `-vf` chain that already carries `bwdif`. A tab is a whole new surface for an unproven feature |
| 3 | **One implementation, at most two entry points** | Two independently-tuned filter chains spelled the same way will drift. A shared module with a checkbox calling into it is fine |
| 4 | **Fixed conservative `hqdn3d`, no strength UI** | Over-denoising an old tape removes the grain **and** the detail, and the model then invents something else entirely. A conservative default is the honest v1; expose a knob only if the measurement shows people need to tune it |
| 5 | **`nlmeans` is refused outright** | Measured at **0.06x realtime** (79 s for 125 frames of 1080p), i.e. 16x the clip duration, to feed a model that will re-invent the detail anyway |

### Why stabilization (#20) gets a tab and this does not

The distinction is technical, not aesthetic:

| | **Stabilise (#20)** | **Denoise (this)** |
|---|---|---|
| Temporal scope | **Global.** Needs the whole file; per-segment jolts at every boundary | **Local.** A few frames of window, so segment boundaries are a non-issue |
| Fits as a pipeline stage? | **No.** That is the whole finding | **Yes**, into a re-encode that already runs |
| Destructive side effect | **Yes**, ~10-21% of the frame, invisible in the output | None: a filter, reversible by re-running without it |
| Needs per-item review? | **Yes**, hence the per-video lever | No, a conservative default is honest |

### Measured filter costs (1080p, 125 frames, decode + filter + null sink)

| Filter | fps | vs realtime |
|---|---:|---:|
| `removegrain=1` | 266 | 10.7x |
| `atadenoise` (temporal) | 224 | 9.0x |
| **`hqdn3d`** (spatial + temporal) | 199 | **8.0x** |
| `fftdnoiz` | 106 | 4.3x |
| `bm3d` (basic) | 12 | 0.5x |
| `vaguedenoiser` | 22 | 0.9x |
| `nlmeans` | **1.6** | **0.06x** |

Images, CPU, `cv2.fastNlMeansDenoisingColored`: 0.34 s at 0.8 MP, 0.51 s at 3.9 MP, 2.09 s at
12 MP. Negligible against a SeedVR2 upscale either way.

### Things that must be decided as part of building it

- **Turning denoise on forces a re-encode of a video that would otherwise stream-copy**,
  converting a free lossless split into a full transcode whose intermediate is
  `yuv420p` 8-bit. Irrelevant for a noisy VHS capture, but it should be stated rather than
  discovered.
- **Remote-only installs have no `cv2`** (the Remote bootstrap installs pillow, piexif,
  paho-mqtt, python-vlc, matplotlib, certifi). **Decision: serve `cv2` from the RunPod
  network volume**, the same way the volume already caches the Ollama runtime and the SeedVR2
  weights, rather than adding ~40 MB to the Remote bootstrap for a feature most remote users
  may not enable. `provision.sh` is the place; it already does incremental,
  self-pruning provisioning, so this is an addition to an existing mechanism.
- **Three unrelated problems hide under one word**, and they will not have one answer:

  | Problem | What it actually is | Right tool |
  |---|---|---|
  | Sensor noise (old digicam, high ISO) | random per-pixel noise | a denoiser. SeedVR2 may already handle it |
  | JPEG compression artifacts | structured, not random | a deblocker, or nothing |
  | Scan defects: dust, scratches, mould | sparse localised damage | **inpainting**, not denoising |

  The third is what people actually complain about with old photo collections, and no
  denoiser touches it. The A/B set is deliberately built to separate these three.

### The separate experiment worth running at the same time

A mild **temporal** filter applied **after** upscaling would act on the model's *own*
instability rather than on the source's noise. SeedVR2's documented temporal jitter of fine
detail on slow pans (the 4x causal temporal VAE, `docs/video-upscaler.md`) is exactly what a
filter like `atadenoise` is built to suppress, and no pre-pass can touch it because it does
not exist yet at that point. Different feature, different target, same test clips.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 12. Local+remote mixed queue: Medium
Let a single Video Upscaler queue run some jobs on local GPU(s) AND others on
rented RunPod pods in one Start, instead of the whole run being local **or**
remote.

- **Today's constraint:** the "Run on" switch is one mode for the entire run
  (`_start` branches to `_start_local` for the whole queue, or the remote
  single-/multi-pod path). Per-item GPU binding only distinguishes among
  **remote** cards; a local job stores no GPU (there is one implicit local card).
  As of 0.5.7 the selector is **locked while the queue is non-empty**, so a queue
  can't be half-built in one mode and switched, which is the correct interim
  behaviour until mixing exists.
- **Foundation already in place:** the `(engine, gpu)` queue grouping
  (`job_group_key` / `group_queue_order` / `distinct_group_keys`), the multi-pod
  orchestrator `_start_grouped` (one runner per group), the GPU picker combobox,
  and the per-item GPU column (which now renders the local card as
  "Local <name>", 0.5.7).
- **Work needed:** (a) a local GPU **identity** scheme so a job can bind a
  specific local card (e.g. `local:0` / `local:1` from `nvidia-smi -L`), not just
  an implicit single GPU; (b) let the GPU picker offer local card(s) as bindable
  options alongside live remote cards; (c) a launcher that dispatches **local
  groups to the in-process/subprocess local engine and remote groups to pods,
  concurrently** (the current grouped path is remote-only and serial); (d)
  per-source telemetry rows + estimates that already exist, wired per group; (e)
  scope the funds guard / confirm-before-rent to the **remote** groups only.
- **Clean stepping stone:** **multiple local GPUs within Local mode** alone
  (bind + run local groups on several local cards) is a smaller, self-contained
  first step that exercises (a)+(b)+(c-local) without any remote concurrency.
  Rare on consumer hardware but real (e.g. a multi-card workstation).
- **Risks:** concurrent orchestration of heterogeneous runners (a local
  in-process engine holding the GPU + N remote pods) is more moving parts than
  the current pendulum; a degrading local card (the watchdog) must not stall the
  remote groups; VRAM feasibility is per-card.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 15. Second remote GPU provider (packet.ai): Medium
Let a remote run rent its GPU from a provider other than RunPod, starting with
[packet.ai](https://packet.ai/), behind a thin provider interface.

> **Blocked on funds, not on design.** The three unknowns below can only be
> answered by signing up and running one real deploy/terminate cycle, and vetting
> the cards costs billed GPU time. See `docs/packet-ai-secondary-gpu.md` for the
> full evaluation (2026-07-14).

- **Why a second provider:** price, stock and region coverage. The app already
  refuses to substitute a GPU type the user did not pick (0.4.0), so when a card
  is sold out in the chosen region the run simply fails and the user re-picks.
  A second catalog is the honest fix for that, and packet.ai's sample pricing
  (RTX 4090 ~$0.39/h, L40S ~$0.92/h, A100 80 GB ~$1.43/h) undercuts RunPod on
  several cards. Its catalog includes the **RTX 6000 Pro 96 GB** already
  benchmarked for video.
- **Why packet.ai and not vast.ai:** vast.ai was investigated 2026-06-23 and
  rejected on billing shape, not on principle: metered bandwidth **both ways**
  (~$40/TB) directly taxes the stream-every-image design, storage is ~5x RunPod's,
  and it has no region-wide network volume. See `docs/dropped-ideas.md`. That
  entry's vetting checklist is the standard packet.ai has to clear: (a) free or
  cheap ingress+egress, (b) cheap region-wide persistent storage that mounts on
  disposable instances, (c) reliable SSH with key injection. On advertised
  behaviour packet.ai clears all three; none is confirmed.
- **Gate before any code (from the evaluation note):** (1) is there a documented
  customer REST API, or is programmatic use CLI-only? (2) can a volume be created
  once and reattached to new pods via API, and is it region-locked? (3) is stock
  on the needed cards reliable, given it is a much smaller provider? Each answer
  changes the interface shape, so the ~15-minute account + `packet gpus --json` +
  one launch/terminate cycle comes first.
- **The known integration risk:** RunPod's GraphQL schema is inspectable
  anonymously, which is how `runpod_client.py` was built at all. packet.ai's API
  reference is login-gated (`dash.packet.ai/docs` returns 403) and the real
  orchestration API underneath is hosted.ai's provider-side REST, which may not be
  fully exposed to customers. So `packet_client.py` may have to **shell out to the
  `packet` CLI** rather than talk HTTP, which is a different seam (subprocess,
  parsing `--json`, a binary to locate) than `runpod_client.py`'s.
- **Work needed:** (a) a provider interface covering what `remote_run` actually
  uses (list GPUs with live price/stock, deploy with an injected public key and a
  mounted volume, inspect, terminate, account balance); (b) `packet_client.py`
  behind it, HTTP or CLI-backed; (c) a provider selector in the GUI plus
  per-provider credentials in `config_store.SECRET_FIELDS`; (d) provisioning the
  model volume a second time on the new provider (`provision.sh` is portable, the
  volume lifecycle is not); (e) the funds guard reading a second balance API.
- **The largest lift is the GUI, not the client.** Provider choice touches the
  RunPod tab, the per-tab GPU pickers, the cost estimator's rate tables, the
  benchmark corpus keys (a card's rate is per provider once prices differ), and
  every "is this remote" branch. Scope it deliberately: a first version that
  supports packet.ai for **video only** (one tab, one flow) is far cheaper than
  making every remote path provider-aware at once.
- **Risks:** a second provider doubles the surface that can break silently at a
  distance (stock, pricing, API drift) on a vendor stack one layer deeper than
  RunPod's. The dead-man's switch, worker, streaming engine and resume logic are
  all provider-agnostic already, so the blast radius is the control plane only.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

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

- **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #19, #20, #22 and #23 are
  complete** (remote upscaling + funds-floor; RunPod video; video conciliation; self-healing
  remote runs; local video; benchmark sharing; telemetry usage graphs; Home Assistant dashboard
  samples; Real-ESRGAN engine; metadata copy + backfill; the comparison lens; derived-directory
  pruning; skipping image variants the pipeline cannot round-trip; Conciliation Undo; RAW input;
  video stabilization; browsing already-upscaled images; the Video Stabilization workflow), so
  the remaining sequencing is only among the open milestones below.
- **Open milestones: #24, #21, #12, #15, #3, #4 — plus #25, which is BUILT and open only for its
  two dated deletions.**
- **#25 (RunPod API v2) was the only milestone with a deadline, and it is done** — all five
  phases on 2026-08-20, months ahead of both dates. Nothing else sequences behind it any more.
  What remains is a **calendar item, not a task**: delete the v1 half after 2026-11-15 and the
  GraphQL balance island when it 410s in early 2027. Neither can be pulled forward, since each
  is the escape hatch for the thing that has not died yet. The design record is
  `docs/runpod-notes.md`.
- **#24 (richer bug reports) is independent of everything else and is the cheapest
  item on this list.** It touches one function (`gui.common._issue_url`) plus wherever the
  last-run summaries end up coming from, and it pays off on the NEXT bad report rather
  than at some later milestone. It is also the only entry here whose value grows with the
  number of users, so it is worth doing before a release rather than after one.
- **#21 (denoise) inherits #19's prepare pipeline**, which is built and in use: a RAW is
  decoded into an in-memory image, straightened in memory, and written to **exactly one**
  lossless temp only when it is actually upscaled (`batch_upscale._write_upscale_input`,
  `orientation.analyse_image`). Denoise slots in as a stage on that array, before the temp.
  The rule that matters is already enforced there and must not be relaxed: **no JPEG temp**,
  because it would spend a generation of quality before SeedVR2 sees a pixel.
- **#21 (denoise) is gated on the A/B harness and may never be built at all.** Its deferral
  behind the RunPod API work (#25) is spent: that migration shipped on 2026-08-20. It is the only open
  milestone whose *value* is unknown rather than its cost. Do not start it before the
  measurement; a "no visible benefit" result moves it to `dropped-ideas.md`, which is a
  successful outcome. The harness itself needs no development time and does not need to wait
  for the RunPod work: it is an afternoon at the keyboard, and running it early is what keeps
  the deferral from turning into a re-litigation later.
- **#20 (Video Stabilization) shipped in 0.6.0** and cost one thing nobody predicted: it
  forced the app-wide **ffmpeg pin off the 8.1 release branch onto master**, because every
  8.1.x corrupts memory in `vidstabtransform`. Anything else built on a less-travelled ffmpeg
  filter should assume the same risk and measure the filter's *determinism* early, not just
  whether it runs.
- **#23 (Video Stabilization workflow) shipped in 0.6.0** and settled the one question it
  had been holding open: a stabilised output is **not** recorded as lineage, so Conciliation
  can never act on it. That precedent generalises - **a new pairing is not automatically a
  lineage row**, and any future "record what came from what" should ask whether the app's
  one destructive tool should be allowed to see it before choosing where it lives.
- **#12 (mixed local+remote queue)** is a medium, self-contained Video Upscaler feature that
  builds on the shipped `(engine, gpu)` grouping; #3 and #4 are lower priority and larger,
  each introducing a new process model, networking, or packaging. With Home Assistant already
  done over MQTT, the old telemetry coupling no longer drives sequencing.
- **#15 is gated by spend, not by other features.** It needs a paid account and
  billed GPU time to answer three questions no public page answers, so its
  ordering is set by when that spend happens, not by #12. Nothing else
  depends on it, and it does not depend on anything else. Note the overlap with
  #12: both add a dimension to "where does this job run", so whichever lands
  second inherits the other's grouping/selector work (a job would then carry
  engine + provider + GPU).
- **#12 has a clean stepping stone** (multiple local GPUs within Local mode)
  that can land first without any remote-concurrency work.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-on from the shipped #6/#7:** generalise the Auto-resume supervisor from
  video to the image runners (batch upscale / tag) (not yet scheduled).
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

## Shipped milestones (numbering legend)

Roadmap **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #19, #20, #22 and
#23** are done and live. **This section is a pointer list, not a record.** Each entry says what the
number meant and where the design of record actually lives; nothing is described in full
here. The numbers survive because code and other docs cite the roadmap by them (`remote
#1`, `Video Upscaler #2`, `local #7`), so deleting the entries outright would strand those
references.

When a milestone ships, its rationale moves to the document that owns the feature and the
entry here shrinks to one of these lines. That rule is the point of the section: a design
kept in two places drifts, and the stale copy is the one that gets read.

- **#1: Remote upscaling (RunPod).** Shipped 0.3.1-0.4.2. The Batch Upscaler and Tag &
  Rename on a rented pod: disposable pod, resident streaming worker, dead-man's switch.
  See `CLAUDE.md` (Remote upscaling) and `docs/runpod-notes.md`.
- **#2: Video upscaling (experimental).** Shipped 0.4.x. The Video Upscaler:
  probe / split / stream / reassemble on a rented pod, with segment-level resume.
  See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`.
- **#5: Video conciliation.** Shipped 0.5.1. Conciliation matches and replaces VIDEO
  originals alongside images, by content-hash lineage only (no name fallback, so a partial
  clip can never be taken for a whole-video match). See `CLAUDE.md` (Conciliation) and
  `conciliate.py`.
- **#6: Self-healing remote runs.** Shipped 0.5.0, video only. An opt-in Auto-resume
  supervisor survives losing the pod mid-run: reconnect a blip, or wait for the identical
  card and redeploy. See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`
  section 17.
- **#7: Local video upscaling.** Shipped 0.5.0. The same SeedVR2 video work in-process on
  the user's own GPU, with a predictive VRAM sizer, a per-card benchmark and optional
  `torch.compile`. See `docs/local-video-upscaler.md`.
- **#8: Benchmark sharing.** Shipped 0.5.1. The per-card video benchmark as a crowdsourced
  corpus: pulled from GitHub at launch, contributed back through a pre-filled issue, curated
  by a maintainer `--merge` tool. See `CLAUDE.md` (Benchmark sharing) and
  `docs/benchmark-sharing.md`.
- **#9: Telemetry usage graphs.** Shipped 0.5.3. A per-run usage-graph window behind each
  telemetry row, one shared instance per source. See `CLAUDE.md` (Telemetry usage graphs)
  and `docs/telemetry-design.md`.
- **#10: Home Assistant dashboard samples.** Shipped 0.5.3. Ready-made Lovelace dashboards
  over the MQTT topics the app already published; docs and samples only, no pipeline change.
  See `samples/home-assistant/` and `docs/mqtt-integration.md`.
- **#11: Real-ESRGAN engine.** Shipped 0.5.6. A second video engine (a fixed-ratio 2X/4X
  GAN) local and remote, plus the general queue change it rides on: per-item GPU binding +
  grouped multi-pod Start. See `CLAUDE.md` (Real-ESRGAN engine cluster),
  `docs/video-upscaler.md` section 18 and `docs/local-video-upscaler.md` section 23.
- **#13: Copy metadata from the original.** Shipped 0.5.9. 13a writes the source's metadata
  onto the upscaled file wherever it is written; 13b backfills the already-upscaled backlog
  inside Conciliation, at the last moment both files exist. See `CLAUDE.md` (Metadata
  carried across) and `tests/test_exif_copy.py`.
- **#14: Hover magnifier ("lens view").** Shipped 0.6.0. Both comparison windows magnify
  the patch under the pointer as original AND upscaled side by side, at the real upscale
  ratio, with a wheel-zoomed and pinnable lens. See `CLAUDE.md` (Comparison) and
  `tests/test_lens_view.py`.
- **#16: Derived directories must not be re-scanned as input.** Shipped 0.5.9. One shared
  name rule prunes the app's own output folders (`__upscaled__`, `__Archive__`,
  `.imgtbx_video`) from every input walk. See `CLAUDE.md` (Derived-directory pruning) and
  `tests/test_derived_dirs.py`.
- **#17: Skip image variants the pipeline cannot round-trip.** Shipped 0.5.9. Transparency,
  several pages and 16-bit depth are detected from the header and skipped with a named
  reason, in the Batch Upscaler and in Conciliation (which checks the ORIGINAL, so the
  protection is retroactive). See `CLAUDE.md` (Image variants left as-is) and
  `tests/test_image_variants.py`.
- **#18: Conciliation Undo.** Shipped 0.5.9. Every file action is journalled before it
  happens, and an archive run can be reversed from that journal; a delete run is refused
  rather than attempted. See `CLAUDE.md` (Conciliation Undo) and
  `tests/test_conciliate_undo.py`.
- **#19: RAW and DNG input.** Shipped 0.6.0. The Batch Upscaler accepts ten RAW formats and
  renders each to a viewable JPEG, from the camera's own embedded preview where there is one
  and a LibRaw demosaic where there is not. Two findings are worth knowing before touching it:
  a RAW is **never eligible for upscaling** at the shipped target (measured 0 of 24, which is
  why it is exempt from the size skip and renders regardless), and a RAW extension must
  **never reach Pillow**, which answers confidently and wrongly for a TIFF/EP container. So
  what shipped is in practice a **RAW renderer**, and the upscale half is scaffolding waiting
  for a target high enough to make a RAW small - see the 8K revisit trigger in
  `docs/dropped-ideas.md`. See `CLAUDE.md` (RAW and DNG input),
  `docs/raw-preview-survey.csv` (the measurement) and `tests/test_raw_input.py`.
- **#20: Video Stabilization (new tab).** Shipped 0.6.0. A tab after Conciliation that
  steadies ONE shaky video into one new file with two-pass `vidstab`: no GPU, no pod, no
  network. It defaults to `optzoom=0` + `crop=keep` rather than the `optzoom=1` every ffmpeg
  tutorial copies, because that default discards a measured ~17-21% of the picture and the
  amount is set by the single worst jolt in the clip. The thing to know before touching it:
  **every ffmpeg 8.1.x corrupts memory in `vidstabtransform`** (fixed upstream by
  `316531e61cf`, on master, not on `release/8.1`), usually with no crash at all - just
  different pixels on every run - which is why `bootstrap.ps1` pins a master build and why
  the tool runs a determinism self-test before it will process anything. See `CLAUDE.md`
  (Video Stabilization) and `tests/test_video_stabilize.py`.
- **#22: Browse already-upscaled images.** Shipped 0.6.0. A **Browse upscaled…** window
  pairs an output tree back to its originals long after the run ended, by inverting the
  upscaler's own mirror. See `CLAUDE.md` (Browse upscaled) and
  `tests/test_browse_upscaled.py`.
- **#23: Video Stabilization tab improvements.** Shipped 0.6.0, all six items. The workflow
  around #20's foundation: a folder loader and a queue of whole-file jobs, a hand-off from
  the Video Upscaler's scan list, playback-first comparison, "Save as Default" on both
  folder fields, and a pair record. Two decisions are worth knowing before touching it.
  **The queue does not reverse #20's "not a batch tool" finding** - that finding is about
  the ALGORITHM being whole-file, and N independent whole-file jobs preserve it exactly, so
  nobody should later "simplify" the queue into segmenting one video. And the pair record
  **is deliberately not a lineage row**: `db.lineage` is what Conciliation matches on, and
  video conciliation is lineage-only, so a row there would make the app's one destructive
  tool offer to replace originals with stabilised copies. It lives in `db.stab_pairs`,
  which no conciliation query reads. The item-5 sub-decision that was left open ("may
  Conciliation ACT on it") was answered **no**, explicitly and with a test. See `CLAUDE.md`
  (Video Stabilization) and `tests/test_video_stabilize.py`.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, the deferred local-engine install, parallel jobs (an image tool
alongside the Video Upscaler), the automatic-telemetry half of benchmark
sharing, UI localization, a light/dark theme, background removal, and the
standing constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
