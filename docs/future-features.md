# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

One entry is listed first despite being **built**, because it is the only one with **dates**
on it: #25, the RunPod API v2 migration. The code shipped in 0.6.1, but the old transports must
be deleted after **2026-11-15** and in **early 2027**, and nothing else in the project will
remember that. #26 is half-shipped for the same reason: its easy half (list the five SeedVR2 weights the app
already pinned but never offered) went out in 0.6.3, while what is left is an easy follow-on
(three VRAM constants that still describe only the heaviest weight, which is what currently
stops the new ones being useful) and a GPU measurement nobody has run, whose entry records a
claim that half made which turned out to be false.
#27 (GIF, both phases) is built and is kept in place rather than moved to the legend, because
its real content was never the input format: it is a data-loss guard, and the reason that guard
is needed is the same reason the next format added will need one. Its phase 2 also carries two
corrections to its own earlier measurements, which is exactly the kind of thing a pointer line
cannot hold.
The rest are unbuilt: one measurement-gated one (#21
denoising, gated on a measurement that has not been run), a remote-side pair covering the same
harm at
different cost (#28, a pre-built public template, plus the cheap idle-volume mitigation it
carries that needs no new primitives), a Video Upscaler feature (#12 mixed local+remote queue)
and one blocked on funds rather than design (#15 a second GPU provider). Two lower-priority ones each introduce a new process model, networking, or packaging
(HTTP interface #3, Unraid #4). The **shipped** milestones are kept below as a numbering
legend, after the open work.

---

## Contents

- [25. RunPod API v2 migration](#25-runpod-api-v2-migration-built-two-deletions-still-dated) (built; two dated deletions left)
- [26. The five unreachable SeedVR2 DiT weights](#26-the-five-unreachable-seedvr2-dit-weights-part-a-built-the-vram-constants-next-part-b-gated-on-a-measurement) (Part A built; the model-blind VRAM constants next; Part B needs a measurement)
- [27. GIF input, static and animated](#27-gif-input-static-and-animated-built-in-063) (built, both phases)
- [28. Pre-built public RunPod template](#28-pre-built-public-runpod-template-medium-plus-a-cheap-mitigation-worth-doing-first)
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

## 26. The five unreachable SeedVR2 DiT weights: Part A BUILT, the VRAM constants next, Part B gated on a measurement

Found 2026-08-23 while surveying alternative upscaling engines (that survey is finished
and every candidate was dropped: `docs/dropped-ideas.md`, "A second upscaling engine").
This is what the survey turned up instead, and it is entirely inside the engine already
shipped.

**`upscale_engine._SEEDVR2_WEIGHTS` hash-pins TEN DiT variants. The GUI offered FIVE.**
The other five were already downloadable, already SHA-256 verified, already handled by
`ensure_seedvr2_weights` and already plumbed to the pod via `DIT_MODEL`. The only thing
missing was a list entry.

| Weight | GiB | Offered before 0.6.3 | Offered now |
|---|---|---|---|
| `7b_fp16` | 15.35 | wizard + video | all three |
| `7b_sharp_fp16` | 15.35 | wizard + video | all three |
| `7b_fp8_e4m3fn_mixed_block35_fp16` | 7.88 | **wizard only** | all three |
| `7b_sharp_fp8_e4m3fn_mixed_block35_fp16` | 7.88 | **no** | all three |
| `7b-Q4_K_M.gguf` | 4.43 | **no** | all three |
| `7b_sharp-Q4_K_M.gguf` | 4.43 | **no** | all three |
| `3b_fp16` | 6.32 | wizard + video | all three |
| `3b-Q8_0.gguf` | 3.41 | wizard + video | all three |
| `3b_fp8_e4m3fn` | 3.16 | **no** | all three |
| `3b-Q4_K_M.gguf` | 1.86 | **no** | all three |

Sizes measured 2026-08-23 from each pinned file's HuggingFace `Content-Length`; they are
recorded in `scripts/seedvr2_models.py` because they answer the network-volume question
below. **No new dependency:** `gguf` arrives via `seedvr2/requirements.txt`, which
`bootstrap.ps1` installs for Local/Both, and the quantised path has been in production
since 0.4.6 (`3b-Q8_0.gguf` is the wizard's <=12 GB tier). The loader guards on
`GGUF_AVAILABLE` and fails with a named message. Only the **4-bit** and **FP8** variants
were never listed.

### Part A: BUILT in 0.6.3

All ten are now selectable in the wizard, in Settings and on the Video tab. Four things
came out of building it, and three of them were not in the plan.

**There were THREE hand-kept lists, not two, and they had already drifted.** The plan
named the wizard's `SEEDVR_OPTIONS` and Settings' `_VIDEO_MODEL_OPTIONS`; the Video tab's
`_SEEDVR2_METHODS` is a third, with its own "keep in sync with tab_settings" comment. The
drift was already real in two directions: `7b_fp8_e4m3fn_mixed_block35_fp16` was in the
wizard but in NEITHER video list (so a 16 GB card was recommended a model for images that
it could not select for video), and the two lists that did agree on contents disagreed on
ORDER. So the fix is **not** the pinning test the plan proposed. A test only detects the
next drift; `scripts/seedvr2_models.py` is one torch-free catalog that all three derive
from, which cannot drift at all. That is the same shape as `esrgan_models.py`, whose
catalog Settings and the Video tab already share, so it is a pattern the codebase had
already proven on this exact problem.

**The learned-batch and benchmark key could not tell these weights apart, and that had to
be fixed in the same change.** `video_vram_sizer.model_tag` was family-only, so all SIX
7B variants shared one key while spanning **4.43 GiB (Q4) to 15.35 GiB (FP16)** of
resident weights. That is precisely the error `tile_tag` and `compile_tag` exist to
prevent, and it fails in the dangerous direction: a ceiling measured on Q4 is higher, a
learned value legitimately bypasses `BATCH_CAP`, so replaying it into an FP16 run OOMs. It
stayed mostly latent only because a user could not switch models, which is exactly what
Part A changed. `model_tag` now carries precision, and the two historical spellings are
kept **byte-identical** (`7b` = 7B FP16 and the unknown-model fallback, `3b` = 3B Q8) so
every row already in `video_bench` / `video_batch_learn` keeps its key and its meaning.
One case is knowingly imperfect and is not migrated: a pre-0.6.3 row written by a 16 GB
user whose wizard recommendation was FP8-mixed is filed under `7b` and can no longer be
told apart from an FP16 row. The information to migrate it with does not exist; splitting
FP8 out from here on at least stops that pool growing, and the sizer's OOM back-off is the
net under the rows already in it.

**Sharp variants deliberately share their twin's key.** Same architecture, byte-identical
file size, so the same VRAM ceiling. Splitting them would orphan rows for no measurement
gain. That was already true before this change.

**The tests reach for the effect, not the list** (D5/D6): one builds the real Settings tab
and reads the combobox's realised `values`, and one round-trips a newly-listed model
through `_video_section()` to prove the picked label is what gets SAVED. A label that
displays but maps back to the default on save is exactly the silent failure this codebase
has now been bitten by twice. It earned its keep immediately: the equivalent test on the
Benchmark window caught a `model=` parameter added to `resolve_bench_keys` but not to
`resolve_bench_key`, which made every REMOTE multi-model window raise `TypeError` inside a
bare `except` and collapse to a single row. That looked exactly like "the sweep only found
one model", and no test of the module constants could have seen it.

### The multi-model sweep (built 0.6.3, the half that makes Part B possible)

Listing the models was the easy half. The measurement half needed the benchmark to sweep
SEVERAL of them, which is what Real-ESRGAN has always done (`esrgan_all_tiers`, one run and
one pod for every tier) and SeedVR2 never did: the model came from `CFG["video"]["dit_model"]`
with no flag and no picker, so ten models meant ten rounds of Settings-Save-reopen and, on a
pod, **ten deploys**. Five decisions shaped the fix.

**The pod is deployed ONCE and the DiT is swapped on it.** That is the whole feature. A
redeploy per model pays pod creation, volume mount and cold start each time, all billed; a
swap pays only the weight reload. It needed a real change on the pod, because the worker
loaded one DiT at startup and `process_job` explicitly withheld the model from SeedVR2 jobs
(its comment said "a SeedVR2 worker ignores `&model=` entirely"). The worker now reloads on a
MISMATCH only, so an omitted or matching model costs nothing and every older client behaves
exactly as before. `_sweep_one_mode` grew an injected-engine path and does not close what it
did not create, so the shared pod has exactly one teardown whatever happens in between.

**The default is ONE model, deliberately unlike Real-ESRGAN's "all tiers".** An ESRGAN tier
probe takes seconds; a SeedVR2 sweep takes hours per model and is billed on a pod. Sweeping
ten because a default changed under somebody is a money bug, so `--models all` and the
Models… picker have to be asked for.

**A single-model sweep emits byte-identical tokens** ("off"/"on", not "7b_fp16/off"). Those
tokens are what a reopened window matches its saved rows against and what every
BCELL/BPROBE/BCEILING echoes, so decorating them would orphan every row an older build wrote.
The `multi` flag lives inside `mode_token` rather than at each call site, because the runner
and the GUI briefly disagreed about when a token grows its model half and the result was a
one-model sweep emitting rows nothing could match.

**It fixed a production bug on the way past, not just a benchmark limitation.**
`job_group_key` is `(engine, gpu)` with the model deliberately absent, so two SeedVR2 jobs
picked with different models on one card were already ONE group on ONE pod, running whatever
`_worker_cfg` had sent. That was a wrong output reporting success. The LOCAL path never had
it: `LocalEngineRouter` caches by `(etype, model)` and evicts on a change, which is the same
policy the worker now implements. Remote was the odd one out.

**The learned-batch key had to change on BOTH sides at once.** Remote keyed it under the bare
RunPod id, which held only while one pod ran one DiT; a multi-model sweep would leave the last
model's ceiling as every model's seed, and a learned value legitimately bypasses `BATCH_CAP`,
so replaying a 3B-Q4 ceiling into a 7B-FP16 run OOMs. `video_benchmark` writes it and
`batch_video_upscale` reads it, and if those two ever drift a remote sweep writes rows no run
will ever read, which looks precisely like a benchmark that did nothing. They are pinned
together by a test.

**The orphaned rows were then recovered, on evidence rather than on a guess**
(`db._migrate_learn_keys_add_model`). Those rows are converged batches measured on rented
hardware, so throwing them away has a price. A row is re-keyed only when that install's OWN
`video_bench` probes for the same card agree on exactly one model tag, because the learn row
and those probes were written by the same sweeps and runs; where the evidence is absent or
two models were measured, the row is LEFT orphaned and the sizer falls back to the seed plus
the OOM back-off. A blanket "assume 7B FP16" was rejected: the wizard writes FP8-mixed into
`video.dit_model` for every 16 GB card, so an install whose LOCAL card is 16 GB holds remote
rows measured on FP8, which is invisible from the key and knowable only from the probes. A
guessed key is worse than no key, because a learned value legitimately bypasses `BATCH_CAP`,
so a ceiling filed against the wrong model can OOM a real run rather than merely mis-seed it.
On the author's own corpus all five orphaned keys resolved, and three independent lines of
evidence agreed (reachability: no benchmarked card was 16 GB, and FP8-mixed was in no video
picklist; config: `video.dit_model` is FP16; and physics: `peak_alloc` at the smallest batch
of the smallest output IS essentially the resident weights, and a 3090 read 15.8 GB against
FP16's 15.35 GiB, where FP8-mixed would have read about half that).

**The probe rows needed no migration at all**, which is the reassuring half: `model_tag` was
deliberately left byte-identical for the two historical spellings, so every `7b` row is still
`7b` and every `3b` row still `3b`. The expensive corpus (406 SeedVR2 probes, hours of GPU
time, some of it rented) was never at risk.

### The network volume: listing a model costs it nothing

The question this raised is a fair one, because a fresh RunPod volume is created at 50 GB
and provisioned with **26.6 GiB** of DiT weights (three tiers) plus ~11 GB of vision
models and the venv, so it is already about 40/50 used. Pre-caching all ten DiTs would
need **70.1 GiB** of weights alone. That does not fit, and it never will.

**It does not have to, because availability and pre-caching are separate axes.** What the
GUI offers is `seedvr2_models.py`; what the volume pre-caches is `pod/provision.sh`'s
`DIT_MODEL_LIST`, and the second is deliberately the shorter list:

- the configured `DIT_MODEL` is **always appended** to the provision download list and
  de-duplicated, so whatever the user picks is on the volume after a re-provision;
- and a model switched to **without** a re-provision is downloaded to the volume on first
  use. That path is not new and not incidental: `remote_run`'s health wait budgets 15
  minutes for exactly it, and the Settings tooltip has always said so.

So `DIT_MODEL_LIST` is unchanged at three tiers. The only real cost of an off-list pick is
a slow first run, plus **volume free space**, which is the one thing that did need
attention: the volume-size prompt still itemised "SeedVR2 ~16 GB" from the single-model
era, understating the cached set by 11 GB on the screen where the user chooses how big to
make the thing. It now quotes the measured ~27 GB and says to leave room for another
model. `provision.sh` carries the per-weight sizes so the next person to consider widening
the cached set can check the budget without a network call.

**Not built, and deliberately:** a free-space check on the pod before an on-demand weight
download. It is the honest failure (a full volume fails the download mid-way rather than
at a decision point), but it belongs to the provisioning path rather than to a picklist
change, and the sizing prompt now makes the situation avoidable rather than merely
detectable.

### Next: the three model-blind VRAM constants (Easy; the fourth item is DONE)

Found 2026-08-29, when the question was asked whether #26 could simply ship as-is and let
an OOM tell the user to pick something smaller. It can, and it did. But that framing
assumes the failure mode is an OOM, and **on the newly-listed light models it is not.**
Part A made ten weights selectable; the three constants standing in front of them still
describe only 7B FP16, so a user who picks `7b-Q4_K_M` gets a run that is sized, gated and
advised as if they had picked a model three times the weight.

**Nothing here needs Part B.** These are arithmetic and one cheap probe, not a quality
judgement on photographs. They are also what converts ten *selectable* models into ten
*useful* ones, so they are the better use of an evening than the measurement below.

**Item 4 shipped in 0.6.3**; items 1 to 3 have not. Item 4 was separated out because
researching it found a live false positive rather than a latent one, and because it needed
no measurement at all.

#### 1. `video_vram_sizer._WS_FIXED_GB` (14.85): the local batch comes out too small

The predictive curve is `peak ~= 14.85 + 0.289 * batch * mp`, fitted on this project's
3090 running **7B FP16** (the fit is exact on its two anchors, so the constant is not a
round number someone chose). The batch-independent term **alone is about 10 GB larger than
the whole of `7b-Q4_K_M`** (4.43 GiB). Since the sizer only ever steps DOWN from the seed
against live free VRAM, an over-large fixed term can only shrink the window. The run
succeeds. There is no OOM, so no advice fires, and the user's reward for choosing the
lighter model is a slower run than the card could give.

That is the important asymmetry to hold on to: **for the light models the failure is silent
underuse, not a visible error**, which is exactly the outcome a fail-with-a-recommendation
net cannot catch.

#### 2. `pod/worker._vram_profile` (`"7b": (16.3, ...)`): the same thing, billed

`_vram_profile` maps every 7B variant to one profile whose FIXED is `16.3`, which is
transparently the FP16 weight size (15.35 GiB = 16.48 GB). Same over-prediction, on rented
hardware.

**The fix is already demonstrated inside this very table, which is what makes it an
evening's work.** The 3B family is ALREADY split by precision: `"3b": (6.40, ...)` against
`"3b_fp16": (9.40, ...)`. That step is **3.00 GB**, and the measured file-size step between
`3b-Q8_0.gguf` (3.41 GiB) and `3b_fp16` (6.32 GiB) is **3.13 GB**. They agree to within 4%.
So within a family the fixed term moves with the weight file, and the remaining precisions
can be **derived from `seedvr2_models.py`'s measured sizes** rather than swept for.

Two honest caveats for whoever does it. The offset is per-FAMILY, not global: 3B's FIXED
sits about 2.7 GB above its weights while 7B's sits marginally below, so derive **within**
a family and never across one. And confirm rather than trust, which is cheap here:
`video_bench.peak_alloc` at the smallest batch of the smallest output IS essentially the
batch-independent term, so **one low-batch probe per model reads the constant off directly.**
That is one probe, not a ceiling sweep, and the multi-model sweep built in Part A already
runs several models on one pod.

#### 3. `video_estimate.VRAM_FLOOR` (32 / 80 / 90): the light models cannot reach the cards they exist for

The per-target floors gate which GPUs the picker will even offer, and they were measured on
7B FP16. Q4's entire value proposition is running where FP16 cannot, and this refuses those
cards before a run can start. So there is no OOM to advise on, because there is nothing to
run. **Until this is model-aware, the 4-bit weights are listed but unreachable in video**,
which also means Part B's headline question cannot be answered on the cards it is about.

#### 4. `db.max_feasible_output_mp`: FIXED in 0.6.3, and the research changed the diagnosis

This is the bypass that lets a proven small card through the VRAM floor above, and it was
reading its proof without asking what regime the proof was measured under. Researching it
turned the priority around, so both halves are recorded.

**The model half was the one Part A made reachable.** Ten weights spanning 1.86 to 15.35
GiB are now selectable, so a 4K probe under `3b-Q4_K_M` could qualify a card for a 7B FP16
4K job. It is the same argument the function already made about Real-ESRGAN ("would wrongly
mark that card feasible"), applied inside SeedVR2. **Theoretical, though**: every SeedVR2
probe in this project's corpus is `7b`, so nothing false had been claimed from a light
model yet.

**The compile half was already biting, on hardware in this project.** Measured from the
real corpus:

```
RTX 3090   7b    (uncompiled)  proves 2.07 MP
RTX 3090   7b|c  (compiled)    proves 1.55 MP   <- probes: ok=0 and an OOM at 1920x1080
```

Taking the max over both told a **compiled** run that 1080p (2.07 MP) was proven, when that
card's compiled measurements say it OOMs there. `learn_tag` had already established that
compile moves the ceiling (125 -> 53 at 540x720) and had joined the learned-batch key for
exactly that reason; the finding never reached this function. So the fix scopes BOTH halves:
proof now counts only when its full bench key (`model_tag + learn_tag`) proves the job's.

**The model order is PARTIAL, and a size comparison would be wrong.** Within a family
weights order it soundly (same architecture, same activations, so the lighter weight leaves
strictly more VRAM). Across families 7B outranks 3B on both axes. But `3b_fp16` (6.32 GiB)
is HEAVIER than `7b_q4` (4.43 GiB) while having SMALLER activations, so neither proves the
other, and a size-only rule would let a 3B proof qualify a 7B job -- precisely the false
positive being removed. `video_vram_sizer.model_outranks` states it; the weight table is
derived from `seedvr2_models` so it cannot drift, and is well-defined only because the two
`sharp` variants are byte-identical to their twins.

**The regime half requires an exact match rather than an order.** A compiled proof almost
certainly covers an uncompiled run (compile lowers the ceiling), and tiling has the same
shape in the other direction, but "almost certainly" is not what a guard admitting a
below-floor card should rest on. Requiring a match can only shrink the answer back toward
the VRAM-tier seed, never inflate it, which is what made this safe to tighten at all.

**Three properties made it cheap.** Omitting the regime keeps the pre-0.6.3 behaviour, so
any caller that cannot know its regime is unchanged. Every existing row is `7b`, the
heaviest weight and also `model_tag`'s unknown-model fallback, so no corpus needs migrating
and no card loses a target it had measured. And filtering only ever lowers the answer
toward the seed, so a test asserts that as a property.

**Where it actually matters is narrower than it looks.** `video_estimate.max_output_mp`
takes `max(seed, proven)`, and on all six measured cards the seed already exceeds proof, so
proof is inert there. The path with no seed underneath is `tab_video._seedvr2_gpu_ok`, which
is the ONLY way a card below its target's floor is offered at all -- and the only card below
a floor in the corpus is the 24 GB 3090, which is exactly where the compiled/uncompiled
mix-up lands.

A note for whoever touches the Benchmark window: it scopes its target-cell cap by the
**lightest** swept model, not the heaviest. That cap only decides which cells are pre-ticked
and which are greyed out, and a multi-model sweep should offer the union of what any of its
models might reach; the sweep then discovers the truth per model, which is its job. Asking
as the heaviest would hide cells a light model could have measured.

While there: `db.py`'s `video_bench.model` column comment still reads
`-- model family tag (7b / 3b / 3b_fp16)`, which Part A made stale.

#### What to keep from the fail-with-advice idea

Keep it, but as the net rather than the plan, because the case it catches is the rarest of
the three. Two things are worth doing on their own:

- **Images already do this and it is fine.** `upscale_engine` offloads, so a heavy model on
  a small card is slow rather than failed, and `ToolTab.confirm_small_gpu` already names
  "a smaller model (Settings) or a lower Resolution Target" before the first local run.
- **One message is now sometimes wrong.** `batch_upscale`'s OOM handler attributes a hard
  OOM to pipeline degradation (VRAM fragmentation, sysmem fallback). That was correct when
  one model shipped. With ten selectable it is often just "this model does not fit this
  card", and the text should say so as an alternative rather than asserting degradation.

**Difficulty:** Easy, and independent of Part B. Do it first.

### Part B: the tier question (gated on a measurement, do NOT guess)

The interesting weight is **`7b-Q4_K_M`, not the 3B one.** `recommend_models` jumps from
3B Q8 at <=12 GB straight to 7B FP8-mixed at 16 GB, and a 4-bit 7B sits in that gap. The
question that decides it is the one that always decides quantisation: **does a 4-bit 7B
beat a full-precision 3B at equal VRAM?** If yes, the 12 GB tier recommendation is
currently wrong for every install, which is worth knowing. Secondary: `3b-Q4_K_M` is
roughly half of Q8, so it is the candidate for whether the documented **8 GB minimum**
can actually come down.

**The rig already exists and is better than anything built ad hoc.**
`video_benchmark.py` sweeps a target to its measured VRAM ceiling on the real card and
persists per-probe rows to `video_bench` keyed by `bench_key` (model + tile + compile).
Quality is a human call on real photographs, and the tool for that also exists: the
`ComparisonWindow` **lens** (#14) puts the same patch from both images side by side at
the true ratio.

**One claim in the original plan here was WRONG, and Part A is what fixed it.** The plan
said "model is already part of the key, so a Q4 sweep cannot collide with an existing
one". It was not: the key was built from `sizer.model_tag`, which was family-only, so a 7B
Q4 sweep would have resumed a 7B FP16 sweep's rungs and published its seconds and its
ceiling as if they were the same model. Part B was therefore **unmeasurable** as written.
It is measurable now, because `model_tag` carries precision. Anything else in this section
that assumes the key separates two regimes should be checked the same way before it is
relied on.

**One prior measurement says do not assume the answer.** The PRO 6000 4K sweep found the
wall is **activations, roughly 80% of peak, not weights** (memory: `pro6000-4k-7b-infeasible`;
it is also why `blocks_to_swap`, a weight lever, was not the answer). If that holds here,
a 4-bit weight lowers the **floor** (the model fits at all) without raising the **batch**,
which makes Q4 a reach-more-cards change rather than a speed one. That is still worth
having, but it is a different claim and should not be sold as the other one.

**Do not move a threshold in `recommend_models` before Part B produces a clear winner.**
Those tiers are the first thing every new user is handed, they are calibrated in
`docs/first-start-wizard.md`, and CLAUDE.md's wizard line plus its 8 GB requirement both
quote them. Part A changed nothing about them on purpose: it only made the models a user
can knowingly choose match the models the app can actually run.

**Difficulty:** Part A done, including the multi-model sweep Part B runs on. The three
model-blind VRAM constants above are Easy, need no measurement campaign, and should be done
FIRST: without them the light weights are selectable but cannot reach the cards they exist
for, which is also what stops Part B being answerable on those cards. Part B itself is a
measurement job rather than a coding job, and its cost is GPU hours plus a judgement call on
real photos.

---

## 27. GIF input, static and animated: BUILT in 0.6.3

Researched 2026-08-23, built 2026-08-29 in two phases. **Phase 1 is the static GIF**
(an image, handled by the Batch Upscaler) and **phase 2 is the animated one** (an
animation, handled by the Video Upscaler, recorded at the bottom). Phase 2 was
deliberately unscheduled when phase 1 shipped; it was built when its measurements were
re-taken against the app's own code, and two of them did not survive that.

**Before this, `.gif` appeared in no extension list at all** - not `IMAGE_EXTS` (all
three copies), not `VIDEO_EXTS`, not `RAW_EXTS`. The app ignored GIF completely, so this
was a clean feature and not a latent bug.

### The guard is not a detail of this feature, it IS this feature

Adding `.gif` to `IMAGE_EXTS` **alone reproduces #17's data loss exactly, and the #17
guard does not fire.** Measured on a 6-frame GIF carrying a transparency index:

```
VARIANT_CANDIDATE_EXTS: ('.png', '.webp', '.tif', '.tiff', '.bmp')
image_variant_reason -> None
```

`.gif` was not a variant candidate, so the check returned None. The engine is RGB
frame-0 only (`_load_image` does `convert("RGB")`, `_save_image` writes
`arr[..., :3]` of `tensor[0]`), so the output would be a flattened first frame under a
mirrored name, and **Conciliation's mirrored-name fallback would then match it with full
confidence and archive or DELETE the animated original.** That is precisely the
failure #17 exists to prevent, in a format that was simply never on the list.

**So `.gif` joined `IMAGE_EXTS` and `VARIANT_CANDIDATE_EXTS` in the SAME change, never
in sequence**, and `test_the_two_lists_moved_together` is what keeps them that way.
An animated GIF is now reported and skipped ("would lose 5 of 6 frames"), which is
correct, protective behaviour from day one and needed no decision about animation at
all. The noun is per format: GIF and animated WebP measure time, a TIFF really does
have pages.

### As built: the output is `<stem>_gif.png`, and Conciliation inverts it

The one real decision was what a static GIF is written AS. GIF is a 256-colour palette
with 1-bit transparency, so saving the 4K result back to `.gif` re-quantises and
re-dithers it, discarding most of what was just computed. It is written as **PNG**.

**The `_gif` marker is #19's RAW+JPEG collision in a second costume.** `logo.gif` and
`logo.png` in one folder would both want to become `logo.png` in the mirrored output
tree, and two sources mapping to one output is not a crash: the first processed wins
and the second is silently counted "already upscaled", with the film strip and the
lineage row pointing at a file produced from the other source. The suffix is
**unconditional**, never "only when it would collide", because a name that depends on
what else is in the folder changes when a sibling is added later, which breaks every
inverse after the fact.

**Unlike a RAW render, this output IS a replacement for its source**, and that is the
difference that shaped the rest. A static GIF holds at most 256 colours and 1-bit
transparency, all of which a PNG carries losslessly, so Conciliation is EXPECTED to
match it and move it in. `conciliate.resolve_by_name` therefore inverts the naming rule
explicitly rather than leaving it to the content-hash lineage. Lineage would usually
match it, and "usually" is not good enough for the one tool that archives or deletes
originals: a tree upscaled by another install, or one whose `db/cache.db` was deleted,
has no lineage row at all, and working without one is the entire point of that fallback.
The tag index is consulted for the GIF name as well as the mirrored one, so an output
that Tag & Rename has since renamed is still found.

**The conciliated file keeps its marker** (`<stem>_gif.png` lands in the original tree,
not `<stem>.png`). Stripping it on the way in looks tidier and is not safe: `logo.gif`
and `logo.png` can both be conciliated into one folder, and `_move_processed_in` moves
with `shutil.move`, which overwrites without asking. The suffix that stops two sources
sharing one OUTPUT is the same suffix that stops them sharing one destination.

Three smaller calls, each recorded at its site:

- **Tag & Rename ignores `.gif`**, the same call RAW gets: it writes a description into
  the file's own metadata and GIF has nowhere to put one. Nothing is lost, because the
  documented workflow points that tab at the UPSCALED folder, where a GIF is already a
  PNG.
- **The upscaled-image browser (#22) pairs it**, unlike a RAW render (which is excluded
  from pairing outright, because the browser cannot draw a RAW as the "before" half). A
  GIF is an ordinary Pillow image, so inverting the name is what gives it Compare long
  after the run ended.
- **`.gif` is NOT in the browser's own extension list.** The upscaler accepts a GIF but
  never writes one, so an output tree holds no `.gif`; listing them would only surface
  files this app did not produce.

Metadata needed nothing: `exif_copy.exif_for_upscaled` returns None for a GIF source and
`pending_backfill` returns 0, so both #13 halves are already the correct no-op.

### What building it found: BOTH fixtures collapse silently

This is the part worth carrying forward, because it makes a test file look right while
testing nothing. **Pillow's GIF writer discards the exact properties under test**, in two
independent ways, measured 2026-08-29:

- `save(transparency=N)` writes **no transparency block** unless index N is actually
  USED in the pixel data. A uniform fill saved with `transparency=0` reloads with info
  keys `['background', 'version']` and nothing else. Three separate constructions failed
  this way before one stuck.
- `append_images=[im.copy()] * 5` collapses to **`n_frames == 1`**, because identical
  frames are optimised away. The "animated" fixture is then a 105-byte static GIF.

A naive fixture is therefore quietly the opposite of what it claims. Every builder in
`tests/test_gif_input.py` asserts its own result before a test relies on it, and one
test pins both collapse modes so a future Pillow that stops doing this is noticed rather
than silently making the guard tests vacuous.

The generalisable version: **when a test's subject is a property the WRITER may
optimise away, the fixture has to be read back and checked, not trusted.** That is the
same instinct as the "present is not working" trio in `known-defects.md`, applied to
test data instead of to an installed component.

### Phase 2: animated GIF out to MP4, BUILT in 0.6.3

Built 2026-08-29 on the measurements below, which were taken in 2026-08-23 research and
then RE-taken against the app's own code. Two of them did not survive contact, and both
corrections are recorded here rather than quietly fixed.

#### The tool that owns it was not a judgement call

The open question was whether an animated GIF belongs to the Batch Upscaler (it is an
image file) or the Video Upscaler (it produces a video). It is settled by one fact:
**`upscale_engine.upscale()` draws a FRESH RANDOM SEED for every image**
(`self.args.seed = random.randint(...)`, no setting pins it), while the video path fixes
one stable seed per source (`batch_video_upscale.per_video_seed`). On a photo that is
invisible. On ten consecutive frames of one scene it is generative FLICKER, and no
encoder fixes it afterwards. The video path also brings temporal batching, which is what
makes an animation look coherent in the first place.

So the Video Upscaler owns it, `.gif` is kept OUT of `VIDEO_EXTS` as its own case (the
same call `raw_decode.RAW_EXTS` gets for the same reason: it is not interchangeable
anywhere downstream), and **`gif_video.is_animated` is the whole of the static/animated
split**: static to the Batch Upscaler, animated here. That split is an implementation
detail a user must never have to hold, which is why the Batch Upscaler's skip line for
an animated GIF now names the Video Upscaler (`batch_upscale.variant_next_step`).

#### The shape: prep, the existing pipeline unchanged, re-time

`gif_video.prepare()` turns the GIF into an ordinary constant-rate video with exactly one
frame per source frame, in the work area; everything downstream (plan_split, split, the
engine, concat, the drift check) treats it as any other short source; then
`gif_video.retime()` puts the original per-frame timing back, duplicating frames at
ENCODE time. Structurally it is the same move the CLIP branch already makes, and it sits
right beside it.

**Correction 1: the inflation is real but the recorded model was wrong.** The entry said
`plan_split` normalises to `r_fps` = 1/shortest-delay and that "one 10 ms tick among 100
ms frames costs 9.6x". Measured through this app's own `probe()`, ffmpeg does not report
r_fps that way: that exact GIF reads `r_fps=59/6` and would cost **0.9x**, not 9.6x. The
conclusion still holds and the penalty is still worth avoiding, just for a different
distribution of cases:

| timing | source frames | CFR-normalised |
|---|---|---|
| uniform 100 ms | 10 | 10 (1.0x, no penalty) |
| messy real-world | 10 | 38 (3.8x) |

Every one of those duplicates is a full diffusion pass and nothing in the UI shows the
waste. The generalisable rule is unchanged and is the reason for the whole shape:
**duplicate after upscaling, never before.**

**Correction 2: neither concat form is exact, and the entry only caught one half.** It
recorded that `duration` directives plus the repeated last frame the demuxer needs ran
+60 ms long. Reproduced exactly. Adding `-t` to trim then **overcorrected to -40 ms**.
Both leave the trailing frame's length to ffmpeg. `gif_video.plan_timing` removes the
decision instead: GIF delays are centisecond-quantised, so take the GCD as a tick and
list each frame `delay/tick` times with no duration directives at all. Total frames
becomes arithmetic. Measured **zero drift on all four timing shapes**, including the
pathological one. It is a pure function, so that arithmetic is unit-tested without
ffmpeg.

#### The matte works, and the first version of it silently did not

Measured: a transparent GIF decodes as `bgra`, a bare conversion composites it to
**black** (confirming the entry's reasoning for that default), and compositing onto an
explicit colour source produces black, white, magenta and `#336699` correctly.

The bug worth recording is how the first implementation failed. Compositing in the
filtergraph needs a second input, a `color` source, and **an overlay takes its output
RATE from that background input**: `color=c=black` with no rate generated 25 fps, and a
10-frame GIF came out as **17 PNGs**. Resampling before the model runs is the one thing
this feature exists to avoid, and it was reintroduced by the matte. It was caught
immediately, and only because `prepare()` asserts its own frame-exactness and refuses
rather than continuing. So the work is split deliberately: **ffmpeg explodes the GIF**
(frame disposal and coalescing are real semantics its decoder implements and a naive
per-frame read gets wrong) and **Pillow composites the matte** (exact, no rate involved,
no second input).

The matte setting lives in **Settings -> Video Upscaler**, which is the user's own call
and the right one: upscaling an animated GIF is a video upscaler's job, and whether a
thing is "a video or a series of images" is a technical detail nobody should have to hold
in order to find a setting.

#### Naming, and the third encounter with one collision

Output is **`<base>_gif_<target>.mp4`**. The video upscaler names outputs
`<base>_<target>.mp4`, so `logo.gif` and `logo.mp4` in one folder would both claim
`logo_4K.mp4` -- one silently becomes "already upscaled" with its lineage row pointing at
a file made from the other source. That is #19's RAW+JPEG collision and phase 1's
GIF+PNG collision for the third time, and the answer is the same each time: an
UNCONDITIONAL marker. The rule lives in `gif_video.OUTPUT_MARKER` and is applied inside
`batch_video_upscale._output_path`, which already owns naming for all four of its
callers; a test pins that the two agree.

#### Conciliation never replaces an animated GIF, three times over

An MP4 is **not a superset** of an animated GIF: looping is gone and transparency has
been composited onto a matte. Both are accepted losses for a DERIVED file the user asked
for; neither is acceptable when the original is about to be archived or **deleted**.
Three independent guards, because the failure is irreversible and quiet:

1. **No lineage row is recorded for a GIF source.** This is #23 item 5's decision applied
   again, and for exactly its reason: `db.lineage` is not a provenance log, it is what
   Conciliation MATCHES ON, and video conciliation is lineage-only. No row means the
   question is never even asked.
2. **An explicit refusal in `conciliate.build_plan`**, listed and reported separately from
   RAW (same reason, different files, and a user needs to know which is which).
3. The #17 variant guard still catches it as a side effect. It is deliberately **not**
   relied on: its reason ("would lose 5 of 6 frames") describes the Batch Upscaler
   flattening it, which is no longer what the app does with one, so a future change that
   taught it about animation would silently remove the protection.

A **static** GIF is unaffected and is still conciliated normally, because there the
processed PNG genuinely is a superset. The guard is about ANIMATION, not about the
extension, and a test says so.

#### What is still accepted rather than solved

Looping semantics are lost in a plain video container. The `<base>_gif_` marker survives
into the output name, which is the honest cost of never letting two sources claim one
name. And `retime()` explodes the upscaled video to PNG in the work area, so a long GIF
at 4K needs transient disk proportional to its frame count; GIFs are short, and the work
area already holds multi-GB video segments, so this was not worth optimising before
anyone hits it.

See `gif_video.py` and `tests/test_gif_video.py`.

---

## 28. Pre-built public RunPod template: Medium, plus a cheap mitigation worth doing first

Researched 2026-08-23. The idea is to replace the per-user network volume with a
**pre-built public template** whose image already carries the model weights, so a
remote user deploys instead of provisioning.

### The motivating harm, which is the real reason this is here

**An abandoned network volume bills forever.** A network volume costs $0.07/GB/month
**billed continuously, independent of pod state**, so the app's 50 GB volume is
**~$3.50/month whether or not anything ever runs**. The app itself creates that volume
through one-click provisioning, and then **never mentions it again**. A user who tries
Image Toolbox, provisions a volume, and stops using it is left paying indefinitely for
storage they have forgotten exists, with nothing in the app to remind them.

The amount is small. The shape is not: this is the app spending a user's money after
the user has stopped using the app, as a direct consequence of a button the app
offered. That is a user-harm argument, not an optimisation argument, and it is what
sets the priority.

### The cheap mitigation, which does NOT need the template

**Everything needed already exists**, so this should be considered on its own and
probably done first regardless of what happens to the template:

- `runpod_client.list_network_volumes` / `get_network_volume` / `pods_using_volume` /
  `delete_network_volume` are all already implemented.
- `account_spend` (added by #25 P4) already returns a **`storage`** component, so the
  app can state what storage has actually cost over the last 30 days rather than
  estimating it.

Candidate shapes, cheapest first: surface the volume and its monthly cost on the
RunPod tab instead of leaving it invisible; say the recurring cost **at the moment of
provisioning**, in the tooltip style where money-affecting controls lead with the
consequence; and offer a plain "delete this volume" with the usual confirmation. None
of that is architecture, and it addresses the actual harm directly. A template removes
the volume for **new** users; only this helps the ones who already have one.

### The template itself: what the research found

**Licensing is clean, which was the likeliest blocker and is not one.** Every weight
the volume holds is Apache-2.0 or better and ungated: SeedVR2 (`numz/SeedVR2_comfyUI`,
`AInVFX/SeedVR2_comfyUI`, `ByteDance-Seed/SeedVR2-3B` all apache-2.0), Qwen3-VL 2B/8B
(apache-2.0), Real-ESRGAN (BSD-3), the `ternaus/check_orientation` CNN (MIT).
Redistribution inside a public image is permitted for all of them.

**The precedent is already in this codebase.** esrgan pods already run volume-free, and
`remote_run.py` records the reason in exactly these terms: *"there is no model volume
to bind and no region to lock to. That is what lets it deploy the cheapest card
anywhere ... a volume would pin it to one datacenter."* esrgan manages it by
self-downloading a few hundred MB per pod; SeedVR2's ~26 GB cannot. A pre-baked image
is the third option, neither a persistent volume nor a per-run download.

**Secondary wins beyond the billing harm.** The volume pins every SeedVR2 run to one
datacenter and the GPU picker is filtered to that region, with EU-RO-1 already recorded
as routinely out of stock on all four tag cards; a template deploys region-wide against
the whole live catalog. And it collapses onboarding from "pick a storage-capable DC,
create a volume, run a billed provisioning pod, wait for ~40 GB" down to "deploy".
Publishing costs nothing: public templates appear in the console's Explore section,
RunPod Hub publishes from a GitHub repo (with a compute revenue share up to 7%), and
public image hosting is free on both Docker Hub and GHCR.

### The open number: pull time (no longer a gate, see "offer both" below)

**Large-image pull time versus volume mount** is the one thing unmeasured. The volume's
value is that a pod starts with weights already present; if pulling is slower, the
template costs a wait on every deploy that the volume pays once. Known: community
reports put ~35 GB as the largest baked image known to work, with RunPod noting slower
initial rollout for very large ones, and **container disk defaults to 5 GB** and must be
raised explicitly.

**This was originally framed as the go/no-go and it is not one any more.** Once both
paths are offered (decided below), pull time stops deciding *whether* to build the
template and becomes an input to *advising which path a user should pick*. It can
therefore be answered later, including from real use.

### Scope: do not bake 40 GB

The volume caches ~26 GB of SeedVR2 tiers plus ~11 GB of Ollama tiers because a
re-provision is a billed pod, so caching everything up front is the cheaper trade. **A
template inverts that**: everything baked is paid for on every cold start, by everyone.
The sensible shape is a small family holding one tier each (an image-mode template with
a single DiT plus the orientation CNN, a tag-mode one with a single vision model), not
one large image. `RemoteSession(mode=...)` already selects a pod image per mode, so
this is a change of constant rather than of architecture.

Also note what a template makes obsolete: 0.5.5's incremental re-provision, the
`ollama rm` pruning and the SeedVR2 stale-weight pruning all exist to make **volume**
updates cheap, and are replaced by "rebuild the image" - simpler, but coarser, and a
multi-GB build/push is a slow maintainer loop on every weight change.

### The #26 relationship is weaker than it looks

#26 was expected to depend on this. **It mostly does not.** `provision.sh` already
states the configured `DIT_MODEL` is always included and de-duplicated, so a
newly-listed weight **reaches the pod today regardless of the 50 GB budget**; it just
costs a download on first use instead of being pre-cached. The budget constrains
**pre-caching**, not **availability**. So #26 Part A is not blocked by this and should
not wait for it. The dependency runs the other way and weakly: if a template ships,
decide what to bake **after** #26 Part B says which tiers are worth recommending, or a
tier the measurement later demotes is paid for on every cold start forever.

**Borne out.** #26 Part A shipped in 0.6.3 without touching `DIT_MODEL_LIST` or the volume
size, exactly as this predicted. What it DID need was a correction to the volume-size
prompt, which still itemised the pre-caching set at "SeedVR2 ~16 GB" when the measured
figure is ~27 GB. Worth noting for this entry: a template's baked size has the same
failure mode, a number written once and then silently outgrown.

### DECIDED: offer both, because they serve opposite users

Settled 2026-08-23. A template is **not** a replacement for the volume. The two suit
genuinely different users, and the split is clean enough to state as a rule:

- **A recurring, multi-GPU user prefers the VOLUME.** Someone running (say) a 5090 for
  images, an RTX 6000 for video and a small card for Tag & Rename provisions **once**
  and all three modes mount it: verified, `_create_pod` reads a single
  `runpod.network_volume_id` for image, tag and video alike (only esrgan is volume-free).
  A template would make that user pull a per-mode image instead, so the one cost is
  amortised across three modes on a volume and paid three times on templates.
- **A one-off user prefers the TEMPLATE.** Someone upscaling 100 images and never
  returning wants nothing left behind. For that user the template is not merely
  cheaper, it is **structurally safe**: there is no artifact to abandon, so the harm
  this entry opens with cannot occur at all. The volume's idle billing and the one-off
  user are the same problem seen from two ends.

**The region lock looks like a tension against the first bullet and mostly is not**,
for a reason that is easy to miss and worth stating so it is not re-raised. The volume
is region-locked, so a multi-GPU user's cards must all live in **one** datacenter. But
**the app is single-task**: `refresh_tab_exclusivity` disables every other tab while any
run is active, and `active_tool_tab` is the single place that answers "is anything
running". So that user needs their three cards **sequentially, never concurrently**, and
"all three in stock at the same instant" is not a requirement the app can generate.

That is not incidental, it is decided. Concurrent runs were investigated and **dropped**
on 2026-07-21 (`docs/dropped-ideas.md`, "Parallel jobs: an image tool + the Video
Upscaler"), which records the conclusion directly: *"0.5.2 locks all other tabs while any
run is active. One run at a time is now the stated model."*

Field observation from the author, which is what makes the sequential requirement
comfortable rather than merely survivable: a well-developed datacenter generally offers
**at least one cheap, one standard and one big GPU tier**, and EU-RO-1 has reliably held
all three. Poorer datacenters have fewer and richer ones have more, but that is the
user's choice at provisioning time and the picker already shows live stock per region.

**So the residual risk is a stock miss on one tool at one moment, not a topology
problem.** The volume's weight-sharing advantage for a multi-GPU user stands.
**Revisit this only if run exclusivity is reversed**, which is the same trigger the
Parallel jobs entry already carries: concurrent runs would make "all cards in one DC at
once" a genuine constraint for the first time.

**What "offer both" does to the pull-time measurement.** Under an either/or decision,
pull time was the go/no-go. Under "offer both" it **stops being a gate** and becomes an
input to advice: the app would need it to tell a user which path suits them, not to
decide whether to build the template. That is a weaker requirement and it can be
answered later, including from real use.

It is still worth knowing that the go/no-go form of the measurement **does not require
the feature**: a Dockerfile with one DiT, pushed to a public registry, deployed once
through the existing `runpod_client` and timed, needs no app changes at all. What
genuinely does need the shipped feature is the production picture (variance across
datacenters, host-side image caching, behaviour under repeated deploys), and that is
tuning rather than go/no-go.

**Difficulty:** Medium for the template. **Easy** for the mitigation, which needs no new
primitives and is the only half that helps users who already have a volume.

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

- **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #19, #20, #22, #23, #24
  and #27 are complete** (remote upscaling + funds-floor; RunPod video; video conciliation;
  self-healing remote runs; local video; benchmark sharing; telemetry usage graphs; Home
  Assistant dashboard samples; Real-ESRGAN engine; metadata copy + backfill; the comparison
  lens; derived-directory pruning; skipping image variants the pipeline cannot round-trip;
  Conciliation Undo; RAW input; video stabilization; browsing already-upscaled images; the
  Video Stabilization workflow; the diagnostics bug report; GIF input, static and
  animated), so the
  remaining sequencing is only among the open milestones below.
- **Open milestones: #21, #28, #12, #15, #3, #4, plus #25 and #26, which are BUILT** and open
  only for what is left of them: #25 for its two dated deletions, #26 for the model-blind VRAM
  constants (Easy) and then Part B's measurement. **#27 is also built** and stays in place
  rather than moving to the legend, because it carries the measured phase-2 record for
  animated GIF, which is deliberately unscheduled.
- **#25 (RunPod API v2) was the only milestone with a deadline, and it is done**: all five
  phases on 2026-08-20, months ahead of both dates. Nothing else sequences behind it any more.
  What remains is a **calendar item, not a task**: delete the v1 half after 2026-11-15 and the
  GraphQL balance island when it 410s in early 2027. Neither can be pulled forward, since each
  is the escape hatch for the thing that has not died yet. The design record is
  `docs/runpod-notes.md`.
- **#24 (richer bug reports) shipped in 0.6.1** and the estimate was wrong in an instructive
  way: almost none of the work was in `gui.common._issue_url` or in gathering the fields, and
  nearly all of it was in the **redactor** and in proving the redactor right. The prediction
  that held was that no after-the-fact regex can bound a Windows path containing spaces. The
  one nobody made was that the danger runs the other way too: **loosening a fail-closed rule
  converts drops into leaks, silently**, and it took an adversarial pass over 198,511 real log
  lines to catch it. Anything that touches those rules later should re-run that pass rather
  than trust the unit tests, which is why the sampled lines are pinned verbatim in
  `tests/test_diagnostics.py`. It also needed **no `db.py` change**, as planned, and ended up
  reading `db/cache.db` for the opposite reason: not to report from, but to redact with.
- **#26 Part A shipped in 0.6.3** and cost one thing the entry did not predict: making the
  five stranded weights selectable exposed that `video_vram_sizer.model_tag` was
  family-only, so all six 7B variants shared one learned-batch / benchmark key across a
  4.43 to 15.35 GiB spread of resident weights. It had stayed latent purely because nobody
  could switch models. The generalisable rule: **widening what a user can choose turns every
  key that collapses those choices into a live bug**, and the collapse is invisible until
  the choice exists. Check the key before shipping the choice. The rule has a second half,
  found on 2026-08-29: it is not only KEYS that collapse the choices, it is **constants**.
  Three VRAM numbers in front of the picklist still describe 7B FP16 alone, so the light
  weights are selectable but are sized, gated and advised as the heaviest one. That failure
  is SILENT (a batch too small, or a card the picker never offers) rather than an error, so
  no fail-with-advice net catches it. Those are Easy and come before Part B.
- **#27 (GIF input) shipped in 0.6.3, both phases**, and confirmed its own premise: the
  feature was never the input format, it was the **guard**, and the entry was right that adding `.gif` to
  `IMAGE_EXTS` alone would have reproduced #17's data loss in a format nothing was watching.
  What it did not predict is where the cost actually landed, and the lesson generalises past
  GIF. **Pillow's GIF writer silently discards the exact properties the tests are about**: a
  transparency index that is not used in the pixel data is dropped, and identical
  `append_images` collapse to one frame, so the obvious fixture is a static opaque GIF that
  reads as an animated transparent one in the source. The rule to carry forward: **when a
  test's subject is a property the WRITER may optimise away, the fixture has to be read back
  and checked, not trusted.** That is `known-defects.md`'s "present is not working" aimed at
  test data instead of at an installed component, and it is the third distinct place that
  shape has bitten this project. The other cost was the one the entry flagged: the output
  cannot keep its extension, so it needed #19's collision reasoning a second time
  (`<stem>_gif.png`) and then a THIRD time in phase 2 (`<base>_gif_<target>.mp4`), plus an
  explicit inverse in `conciliate.resolve_by_name`, because
  lineage is not available on every install and Conciliation is the tool that deletes things.
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

Roadmap **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #19, #20, #22, #23,
#24 and #27** are done and live. **This section is a pointer list, not a record.** Each entry says what the
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
- **#24: Make a bug report actionable without asking.** Shipped 0.6.1. **Report an issue**
  now builds a pre-filled body and a redacted diagnostics zip, and hands the user the file to
  drag in. Read the record before touching any redaction rule: they are the kind that get
  "simplified" later, and it documents what each one cost. Two are load-bearing beyond this
  feature - **collecting less is the only protection a later bug in a rule cannot undo**, and
  **an app that generates text about the user's data has a disclosure channel no structural
  rule will find** (the vision model's descriptions were still in the zip after every path
  rule passed). See `docs/bug-reports.md`, `CLAUDE.md` (Report an issue) and
  `tests/test_diagnostics.py`.
- **#27: GIF input, static and animated.** Shipped 0.6.3, both phases. A STATIC GIF is an
  image the Batch Upscaler upscales to `<stem>_gif.png`; an ANIMATED one is an animation the
  Video Upscaler turns into `<base>_gif_<target>.mp4`. The split is forced rather than chosen:
  the image engine draws a fresh random seed per image, which on consecutive frames of one
  scene is generative flicker. The feature is really the **guard** in both phases: `.gif` in
  `IMAGE_EXTS` without `VARIANT_CANDIDATE_EXTS` reproduces #17's data loss, and Conciliation
  must never replace an animated GIF with a video that has lost its looping and flattened its
  transparency. The entry stays in the open list above rather than shrinking to this line,
  because it holds two measurement corrections and the reason a matte cannot be built in an
  ffmpeg filtergraph. See `CLAUDE.md` (GIF input), `tests/test_gif_input.py` and
  `tests/test_gif_video.py`.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, the deferred local-engine install, parallel jobs (an image tool
alongside the Video Upscaler), the automatic-telemetry half of benchmark
sharing, UI localization, a light/dark theme, background removal, and the
standing constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
