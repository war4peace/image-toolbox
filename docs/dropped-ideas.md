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
- [Everything around the donation link](#everything-around-the-donation-link-2026-07-27)
- [Verifying the Home Assistant webhook](#verifying-the-home-assistant-webhook-2026-07-27)
- [UI localization / multi-language interface](#ui-localization--multi-language-interface-2026-07-27)
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

## Everything around the donation link (2026-07-27)

The "Buy me a coffee" link shipped in 0.5.8 as **one label in the bottom status
bar**, and that placement is the whole design. Rejected, so it is not
re-proposed: a Settings "Support" section, a first-start-wizard step, an
installer post-install checkbox, a README badge or Support section, and **any
form of click counting** (the app never contacts that site by itself).

The reasoning is the project's own premise: it is a free personal tool, and the
difference between a link someone can choose to notice and a thing that asks is
the difference between the two kinds of software this is not trying to be. One
place, no telemetry, no prompt after a run.

Still available if ever wanted: a repo-page **Sponsor** button is
`.github/FUNDING.yml` with `buy_me_a_coffee: <username>`, a two-line file that
touches no app code. Noted so the option is not lost; it was not part of the
work.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Verifying the Home Assistant webhook (2026-07-27)

The webhook notification backend (0.5.8) **cannot confirm its own delivery**, and
three ways of trying were rejected. Home Assistant answers `200 OK` to a webhook
id it has never heard of, on purpose ("Always respond successfully to not give
away if a hook exists or not"), and also to a request its `local_only` refused.
So there is no positive signal to read, and the Test button says exactly that.

Rejected:

- **Checking through HA's REST API with a long-lived access token.** A second
  credential and a different auth model, and it still would not prove the id maps
  to an automation.
- **Probing `/api/config` to at least confirm "this is Home Assistant".** Proves
  the host, never the hook, and invites precisely the false confidence the honest
  wording exists to avoid.
- **Any round-trip where HA calls back into the app.** That needs the app to
  listen on a port, which it does not and should not do.
- **A "skip certificate check" toggle** for a self-signed HTTPS Home Assistant: a
  permanent hole in every HTTPS call the app makes, to avoid one config change on
  a LAN where plain HTTP already works.

The verification is the **user's**, on the HA side (the automation's Traces, or a
temporary `persistent_notification.create` action), and takes under a minute. See
`docs/notifications.md`.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## UI localization / multi-language interface (2026-07-27)

Prompted by [`upscayl-vs-image-toolbox.md`](upscayl-vs-image-toolbox.md) section 3.7,
where "Localised UI (many languages)" is one of the few rows Upscayl wins outright.
Researched against the code, then **dropped**.

**The decision, in the author's words:** Image Toolbox is a single-developer project, a
passion-born side job. Multi-language is too much work for too little reward for the
foreseeable future.

**Revisit only if** the app matures to hundreds or thousands of users, at which point the
reach it buys might justify the ongoing cost. Not before.

### Why the cost is higher than it looks

- **The surface is ~2,130 candidate user-visible strings in `scripts/gui/` alone**
  (AST-counted: 4+ chars, contains letters, not an identifier/path/URL/format key, so an
  upper bound with maybe 20-30% noise). Worst offenders: `tab_video.py` 492,
  `video_benchmark.py` 250, `tab_settings.py` 220, `tab_runpod.py` 198. The runners and
  backend add ~4,120 more.
- **Upscayl's entire `en.json` is a few hundred keys**, for one screen plus a settings
  panel. This app has six tabs, a wizard, a benchmark window, a segment picker, two
  comparison windows and ~160 deliberately wordy tooltips. The comparison row is real but
  the two jobs are not the same size, and that asymmetry is the finding.
- **The ongoing cost is the problem, not the mechanical work.** Wrapping the strings,
  building a catalog and adding a picker is days of focused work. Keeping ~2,130 strings
  translated across releases, on a one-maintainer project, is forever.
- **A machine-translated catalog nobody proofreads would be worse than English here.**
  The tooltips carry money and data-loss warnings ("Delete is not undoable", "a pod bills
  by the second"). Getting one subtly wrong in a language the author cannot read is a
  hazard, not a cosmetic risk.

### What the research found, for whoever picks this up later

Kept because it is the expensive part to rediscover:

- **Scope line:** GUI chrome only (~2,130 strings, one process, no protocol impact).
  **Logs should stay English permanently**, and be documented as a deliberate support
  decision: a user pasting a translated log into a GitHub issue hands the author text he
  cannot read.
- **Machine-readable strings must never be translated.** Some strings that look like UI
  text are a wire format: MQTT `task/details` is literally the status label's text
  (`tab_conciliate.py` publishes `self.status_lbl.cget("text")`), `task/name` values are
  matched by the shipped Home Assistant automations, and the `@@TBX@@` events and
  `last_run` payload keys are parsed, not read. The boundary is **the presentation layer
  translates, the integration layer never does**, and where a string is currently both
  (the `task/details` case) it has to be split into an English state value plus a
  translated display string. That is a refactor, not a search-and-replace.
- **Layout will break, and it cannot be machine-translated away.** tkinter does not
  reflow; the GUI has 97 hard-coded `width=N` values and a fixed tooltip wrap width.
  Romanian, German and French run 20-35% longer than English. The 0.5.8 rows are the most
  exposed, having deliberately optimised for tightness (the SeedVR strip is commented
  "fit every SeedVR control on ONE row even at the app's minimum width"). Expect a
  per-language layout pass, and some labels shortened in *English* to leave headroom.
- **Technology:** a JSON catalog plus a `t(key)` helper, falling back per key to English.
  Stdlib only, hand-editable by a non-programmer translator, no compile step, no installer
  change, and it is what Upscayl does. `gettext` also works but adds an `msgfmt` build step
  and binary catalogs; `babel` fails the dependency rule outright.
- **Pilot, if it ever proceeds:** one second language, **Romanian**, GUI chrome only. The
  author is a native speaker and can proofread it, which removes the hazard above, and it
  produces an honest per-language cost figure before any promise of "many languages".
- **Test coupling:** `tests/test_settings_recommended_values.py` asserts literal English
  substrings in the module source to pin a tooltip's advice to the runner's coded default.
  A catalog needs its own equivalent guard, or that drift protection is lost per language.
- **Also confused with this, deliberately:** Tag & Rename's **description language**
  (`resolve_language`) sets the language the vision model *writes photo descriptions in*.
  That is output data, not interface. If a UI language setting is ever added the two sit
  next to each other in Settings and must be labelled "Interface language" vs "Description
  language".

### One piece worth doing anyway, independently of this

The research turned up two places where **logic reads a widget's displayed text**. Both
work only because the UI is monolingual, and both are worth fixing on their own merits:

1. `tab_upscale.py`: `self.PAUSE_TIPS.get(self.pause_btn.cget("text"))` looks a tooltip up
   by the button's displayed English label. Under any relabelling this silently returns
   `None` and the tooltip disappears.
2. `tooltab.py`: `remote = self.run_on_var.get() == RUN_ON_REMOTE` derives the run mode
   from a combobox's *display* value. It survives today only because both sides come from
   one constant, which is fragile by construction: the moment the label and the comparison
   diverge, the app runs local jobs remotely or vice versa. That is a money bug.

The correct pattern is a stable internal key plus a separate display string. An audit for
"logic keyed on displayed text" is small, self-contained, and improves correctness whether
or not localization ever happens.

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
  **Note: this rejected vast.ai, not multi-provider support.** A second provider
  is planned via **packet.ai**, which clears the checklist on advertised
  behaviour: roadmap **#15** in `docs/future-features.md`, evaluation in
  `docs/packet-ai-secondary-gpu.md`.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>
