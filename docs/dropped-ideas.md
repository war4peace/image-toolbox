# Dropped ideas & constraints

Ideas that were investigated and **decided against**, plus the standing
constraints that rule whole classes of feature out. Kept because the reasoning
is the valuable part: without it the same idea comes back every few months and
gets re-investigated from scratch.

Nothing here is scheduled. If one is revisited, the trigger is named in its
entry ("revisit only if ...").

Source for the open roadmap: `docs/future-features.md`.

---

## Contents

- [Deferred local-engine install](#deferred-local-engine-install-2026-07-21)
- [Parallel jobs: an image tool + the Video Upscaler](#parallel-jobs-an-image-tool--the-video-upscaler-2026-07-21)
- [Pause for the Video Upscaler](#pause-for-the-video-upscaler-2026-07-21)
- [Region pre-seed at first-run bootstrap](#region-pre-seed-at-first-run-bootstrap)
- [Automatic run-telemetry reporting](#automatic-run-telemetry-reporting-phase-2)
- [Everything around the donation link](#everything-around-the-donation-link-2026-07-27)
- [Verifying the Home Assistant webhook](#verifying-the-home-assistant-webhook-2026-07-27)
- [UI localization / multi-language interface](#ui-localization--multi-language-interface-2026-07-27)
- [Light/dark theme](#lightdark-theme-2026-07-28)
- [Background removal for images and video](#background-removal-for-images-and-video-2026-07-28)
- [Archival / intermediate codec output](#archival--intermediate-codec-output-2026-07-28)
- [Processing alpha, multi-page and high-bit-depth images](#processing-alpha-multi-page-and-high-bit-depth-images-2026-07-29)
- [Managing the network volume via the RunPod S3 API](#managing-the-network-volume-via-the-runpod-s3-api-2026-07-29)
- [Folding a RAW render back into the source tree](#folding-a-raw-render-back-into-the-source-tree-2026-07-30)
- [Standing constraints](#standing-constraints)

---

## Deferred local-engine install (2026-07-21)

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

## Parallel jobs: an image tool + the Video Upscaler (2026-07-21)

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

## Automatic run-telemetry reporting (phase 2)

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

### One piece worth doing anyway: DONE (0.5.8)

The research turned up two places where **logic read a widget's displayed text**, both
working only because the UI is monolingual. They were worth fixing on their own merits and
**were fixed** when this idea was dropped:

1. `tab_upscale.py` looked its pause-button tooltip up by the button's own label
   (`PAUSE_TIPS.get(self.pause_btn.cget("text"))`). Now one `PAUSE_PHASES` table keyed by
   the phase, set through `_set_pause_phase`, so the label and the hint come out together
   and neither is derived from the other.
2. `tooltab.py` derived the run mode from the "Run on" combobox's *display* value
   (`run_on_var.get() == RUN_ON_REMOTE`). That was a money bug in waiting: an unrecognised
   label reads as "not remote", so a run aimed at a rented pod would have executed on the
   local GPU. Now a `_run_on_modes` label-to-mode table whose fallback is **the current
   mode**, never an implicit local. `tab_video` already did it this way (`mode_var` token +
   `mode_pick_var` label), so this only brought `ToolTab` in line.

`tests/test_display_text_is_not_state.py` pins both, including the unrecognised-label case
that no manual click-through would produce, plus a structural check that nothing in
`tab_upscale` reads a widget's text again.

One instance was left alone deliberately: `tab_conciliate` publishes its status label's
text to MQTT `task/details`. There the published value **is** the human status line by
design (that is what the topic is), so reading it back off the widget is merely
inelegant, not wrong. With localization dropped it is not a hazard either.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Light/dark theme (2026-07-28)

Prompted by [`upscayl-vs-image-toolbox.md`](upscayl-vs-image-toolbox.md) section 3.7,
where a themed UI is one of the few rows Upscayl wins outright. Researched against the
code and probed against the real Tk build (8.6.15), then **dropped**, for the same shape
of reason as [UI localization](#ui-localization--multi-language-interface-2026-07-27).

**The decision, in the author's words:** currently too expensive from a development
standpoint. And the usage pattern argues against it: **the UI is an enabler for
long-duration jobs, during which the user does not need to look at it** (which is what
every notification feature exists for). Using the app is minutes of UI work and many hours
of waiting for an upscale or a Tag & Rename run to finish. That makes theming a genuine
"nice to have, at some point", not something to spend the current budget on.

**Revisit if** the balance shifts: if the app grows work that keeps a user *in front of*
the window, or if the effort drops sharply (a vendored theme that turns out to be a drop-in
on this widget set).

### The blocking fact, measured

This is the part worth keeping, because it is not obvious from the source and it is what
makes the idea expensive.

The app calls `ttk.Style(self).theme_use("vista")`
([app.py:85](../scripts/gui/app.py#L85)). **`vista`, `xpnative` and `winnative` are
native-drawn themes**: the widget pixels come from the Windows UxTheme engine, not from Tk,
and they **cannot be recoloured**. Probed on this machine:

| Theme | `TButton` layout root | Meaning |
|---|---|---|
| `vista` (current) | `Button.button` | ONE native element paints the whole button. Colour options are ignored. |
| `clam` | `Button.border` | Tk-drawn border + padding + label, each independently styleable. |

So **dark mode is not "add colours to the existing UI". It requires abandoning the native
Windows theme** and moving the whole app to `clam` (or `alt`/`default`), which also changes
how the app looks in **light** mode. The decision hiding behind this idea was never
technical: it is whether losing the native Windows look is acceptable.

**A trap that will waste an afternoon if it is not written down:**
`Style.configure("X.TButton", background="#ff0000")` **succeeds silently** under `vista`,
and `Style.lookup(...)` dutifully reads `#ff0000` back, while the rendered button is
unchanged. The configured value is stored; the native element engine just never consults
it. So theming cannot be verified by `lookup` or by any unit test, only by looking at the
screen. Available themes on this build, for reference: `winnative, clam, alt, default,
classic, vista, xpnative`.

### The surface it would have to convert

| Thing | Count | Note |
|---|---:|---|
| Hard-coded hex colours in `gui/` | **160** literals, **36** unique, across 16 modules | Worst: `tab_runpod.py` 48, `tab_settings.py` 23, `widgets.py` 13, `comparison.py` 13. |
| Classic (non-ttk) widgets | 19 `tk.Label`, 16 `tk.Toplevel`, 7 `tk.Canvas`, 3 `tk.Text`, 3 `tk.Menu`, 2 `tk.Button`, 1 `tk.Listbox` | These **never** follow a ttk theme. Each needs explicit `bg`/`fg`/`insertbackground`/`selectbackground`, in both themes. |
| matplotlib figure | [telemetry_graph.py](../scripts/gui/telemetry_graph.py) | Its own hard-coded palette (`grid color="#e6e9ef"`, `mec="white"` markers, `#3a4250` labels). Needs a second palette plus figure/axes facecolors, or it renders a white slab inside a dark window. |
| Window title bar | n/a | Stays light unless explicitly told otherwise (see below). |

The 36 colours are **already semantic**, which was the one genuinely good finding:
`funds_color` ([common.py:275](../scripts/gui/common.py#L275)), the telemetry load bands
`TelemetryRow._band` ([widgets.py:249](../scripts/gui/widgets.py#L249): blue `#3a86ff`
<=25, green `#1a9e4b` <=65, dark yellow `#b58900` <=85, red `#d11a2a`), the film-strip
green/red outcome frames, the link blue `#3a86ff` with `#1a5fd0` hover. So the conversion
would be "route ~36 named roles through a `PALETTE[theme][role]` table in
`gui/common.py`", not "invent a design system".

**They cannot be inverted programmatically.** These were picked to read on a light
background; `#1a7f37` green (used 30 times) is close to unreadable on dark. Every one needs
a hand-picked dark counterpart checked for contrast. That is eyeballing, not an algorithm,
and it is the part that has no clear finish line.

### The other findings, kept so they are not re-derived

- **The title bar needs ctypes.** Windows draws it, and it stays light unless the app opts
  in: `DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE = 20, BOOL(1), 4)`
  (attribute 19 on pre-1903 builds). One small call, in exactly the style the app already
  uses ([taskbar_progress.py](../scripts/taskbar_progress.py), `single_instance.py`,
  `crash_logger.py`). Without it a dark window with a white title bar looks broken rather
  than themed, and there are **16 `tk.Toplevel`s**, so it wants a helper.
- **Detection is free.** Windows exposes the preference at
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\AppsUseLightTheme`
  (`0` = dark), readable with stdlib `winreg`. Checked on the author's machine during this
  research: **`AppsUseLightTheme = 0`** (the OS is already dark and Image Toolbox is the
  light outlier, which is why the idea came up). If it ever ships, offer **Auto / Light /
  Dark**, not Auto alone: a user who wants the app light while their OS is dark is not an
  edge case, and the extra cost is a three-value combobox.
- **Live switching is roughly double the work.** Re-theming a running tkinter app means
  walking every existing widget and reapplying colours, because classic widgets keep
  whatever they were given at construction. An honest v1 applies the theme at startup and
  says so.
- **Vendoring a modern ttk theme** (Sun Valley / Azure style `.tcl` + assets) buys a
  Win11-ish light+dark look largely for free, but costs a third-party asset bundle to ship
  and maintain, PNG elements that soften at non-100% DPI, and it lands on the same
  "not native" outcome anyway.
- **"Dark-ish without leaving `vista`" is not achievable.** Recorded explicitly so it is
  not attempted: you would get dark backgrounds behind light-grey native buttons.
- **Verification is manual, entirely.** No test can see a colour here. Every tab, both
  comparison windows, the wizard, the benchmark window, the segment picker, the log viewer
  and every dialog would have to be opened and looked at, twice.

### If it is ever revisited, start here

The first step is not code. **A throwaway branch that flips one tab to `clam` and
screenshots it next to the current `vista` UI** answers the only question that matters, in
a minute, and everything else is downstream of that answer.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Background removal for images and video (2026-07-28)

> Research background remover functionality for images
> (<https://huggingface.co/briaai/RMBG-2.0>), as well as video
> (<https://bria.ai/video-background-removal>), using API only (users provide their API
> key).

Asked for by **someone on social media**, not by the author. Researched 2026-07-27 against
BRIA's public docs and pricing pages, then **dropped**. Nothing was built.

**The decision, in the author's words:** Image Toolbox is not the right tool for this,
because **background removal exceeds the scope of the application**. If it were trivial to
implement it might have been worth a go anyway; it is not.

**Revisit if** there is *enough demand*: not one request, but a repeated ask from several
users. A second trigger would be the effort collapsing, which is plausible (see the licence
finding: the MIT model below runs on the stack Local/Both installs already have). Even then
the scope objection stands on its own and has to be answered first.

### Why "exceeds the scope" is the right call, not just a preference

This app revives **old family photo collections**: it upscales them, describes and renames
them, and puts the results back. Background removal is a product-photo / avatar / thumbnail
tool. It is a different job for a different person, and it would be the first feature here
that does not serve "make my parents' photos look good again".

It is also only half a feature. If the point is "cut the subject out and put it on a new
background", the second half is **compositing**, which the app has no UI for and no reason
to grow one.

### The API, as measured (re-check before any work: two BRIA pages already disagreed)

| | Image | Video |
|---|---|---|
| Endpoint | `POST https://engine.prod.bria-api.com/v2/image/edit/remove_background` | `POST .../v2/video/edit/remove_background` |
| Auth | `api_token: <key>` header | same |
| Input | JSON `{"image": "<base64 or public URL>"}` | video URL or file |
| Synchronous? | **No.** `202` + `request_id` + `status_url`, poll until terminal | no |
| Output | `result.image_url`, a link to download the PNG (alpha) | same shape |
| Extras | input **and** output content moderation, either can block the call | four endpoints incl. a 24 fps WebSocket streaming one |
| Price | **~$0.018/image** on the RMBG-2.0 tier; a self-service page said **$0.08/image** | **$0.14 per video second** |
| Free tier | ~100 trial calls | same account |

Base64 input matters: a desktop app can send a local file with no "host your photo publicly
first" step. The **result** is a URL to download, so every processed image round-trips
through BRIA storage.

**The video price kills the video half outright.** Against this repo's own measured figures
for a 1-minute clip on a rented pod ([video-upscaler.md](video-upscaler.md), section 7):

| 1-minute clip | Cost |
|---|---|
| SeedVR2 upscale to 1080p (RTX PRO 6000) | ~$0.77 |
| SeedVR2 upscale to **4K** | ~$6.90 |
| **BRIA background removal** | **$8.40** |

A 10-minute home video is **$84**. The app's whole video design exists to make a long job
affordable (per-run minute and cost caps, installments, a cheapest-card estimator), and a
fixed per-second cloud price defeats every one of those levers at once: there is no cheaper
card to pick and nothing to tune. If this is ever revived, **images only**.

### The licence finding, which is the genuinely useful one

`briaai/RMBG-2.0` on Hugging Face is **CC BY-NC 4.0** ("Commercial use is subject to a
commercial agreement with BRIA"), so the weights cannot be bundled. The API price includes
the commercial licence, which is exactly why the original idea said "API only". But:

1. **RMBG-2.0 is BiRefNet.** BRIA's own model card describes it as the **BiRefNet
   architecture** plus their proprietary dataset and training. `ZhengPeng7/BiRefNet` is
   **MIT**, 0.2B parameters, and runs on the torch + Pillow stack this app already ships on
   Local/Both installs. So "a local background remover" is **not blocked by licensing at
   all**; only *BRIA's* weights are. Quality is not the deciding factor either way: BRIA's
   own claim is that RMBG-2.0 beats stock BiRefNet because of their **training data**, on
   the same architecture. A refinement, not a category difference, and nobody here is
   compositing for print.
2. **"Non-commercial" may not be as simple as it looks.** The app is free, but the installer
   carries a RunPod **affiliate** referral link
   ([ImageToolbox.iss:17](../installer/ImageToolbox.iss#L17)) and the status bar a donation
   link. Whether that colours a CC BY-NC bundle is a question for BRIA or a lawyer.
   Recorded so nobody bundles the weights assuming "it is a free app" settles it.

So the cheap path, if demand ever justifies one, is **local MIT BiRefNet**, not the API: no
key, no account, no per-image cost, no moderation, nothing leaves the machine, works
offline. The trade is a ~1 GB model download, a second thing to keep loaded, and
Remote-only installs needing a pod path (small next to SeedVR2's 16 GB, so the pod worker
could serve it).

### The objections that would still apply on the API path

- **It breaks the "everything runs on your machine" promise** stated at the top of
  `CLAUDE.md` and in the README. Every other remote thing here rents *the user's own* GPU on
  demand and sends images to a pod the user controls and destroys. This would send them to a
  company, which stores the result at least long enough to serve a download URL. A different
  bargain, and one that should be made explicitly rather than discovered in a checkbox.
- **Content moderation can refuse a photo.** The endpoint moderates input and output. In an
  app built for family photos (beach photos, children, old prints) a false positive is not
  hypothetical, and there is nothing the app could do but report the picture as rejected.
- **The output is a PNG cutout, and it must never reach Conciliation.** Conciliation
  replaces originals with their processed counterparts, matching by content-hash lineage
  first and mirrored **name** second for images. A `photo.jpg` whose "processed" counterpart
  is a transparent `photo.png` cutout is precisely the case where a successful match is a
  disaster. Anything built here must **not** record lineage for cutouts, and the name
  fallback must not see them. The Video Upscaler already had to make this same call
  (lineage-only matching, no name guess; see `conciliate.py`).

### What it would have cost

The API client (submit / poll / fetch, stdlib + `net_ssl`), the Settings key + Test, the
secrets split (`bria.api_key` into `config_store.SECRET_FIELDS`) and the Conciliation
carve-out are each small. The runner + tab is medium, and mostly inherited: `ToolTab`
already provides the subprocess runner, the `@@TBX@@` event protocol, the film strip, the
progress bar and ETA, the taskbar progress, MQTT `task/*` publishing and the notification
fan-out. Local BiRefNet instead of the API is medium plus a model download.

That is the trap in this idea, and worth naming: it **fits the existing seams almost too
well**, which makes it look cheap. Fitting the plumbing is not the same as belonging in the
product.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Archival / intermediate codec output (2026-07-28)

> Batch Upscaler: extend input image processing to RAW and DNG files, as well as
> Archival / intermediate codecs (ProRes, FFV1)

Half of this idea turned out to be **already shipped**: ProRes, FFV1 and DNxHR *input*
works end to end and losslessly today, verified by measurement. That half is documented
in [`video-upscaler.md`](video-upscaler.md) section 6.5 and is not repeated here.

What was **dropped** is producing an archival or intermediate codec as the **output**, plus
adding `.mxf` to `VIDEO_EXTS`.

### Why

**The deliverable exists to play natively on a monitor or a TV.** That is the app's stated
purpose, and it is the same sentence that settled the RAW output-format question the same
day (`.jpg`, not 16-bit TIFF). No television plays FFV1, and ProRes playback outside the
Apple ecosystem is unreliable. An archival or intermediate output is therefore not the
deliverable at all: it is a second, optional artifact for a user who is going to do further
work to it in a video editor. That is a different job for a different person, which is the
same scope objection that closed background removal.

**The wording bundles two audiences who want opposite things**, and separating them is what
made it decidable:

| | **Intermediate** (ProRes, DNxHR) | **Archival** (FFV1) |
|---|---|---|
| The ask | "something my editor can scrub and grade" | "a lossless preservation master" |
| Lossless? | No (high-bitrate DCT) | Yes, bit-exact |
| 1 h of 4K output | ~155 GB | ~220 GB |
| Honest from this pipeline? | Yes, the claim is "easy to edit" | Weakly: the model input is 8-bit RGB and the existing encode is already visually transparent |

**Storage is the whole cost**, and it is the number that decides it. Measured on 5 s of
1080p25 (real content, RTX 3090), scaled to an hour:

| 1 hour of output | HEVC (today) | ProRes HQ | FFV1 lossless |
|---|---:|---:|---:|
| 1080p | ~4 GB | ~39 GB | ~55 GB |
| 4K | ~16 GB | ~155 GB | ~220 GB |

### The finding worth keeping: encode time is NOT the objection

The instinct that "CPU archival encoding will be slow" is **wrong at this scale**, and
measured to be wrong. Every archival encoder is intra-only and cheap, and all of them beat
the `libx265 -crf 12` the app already uses by default:

| Encoder (5 s of 1080p25) | Time | vs realtime | Size | Bitrate |
|---|---:|---:|---:|---:|
| `hevc_nvenc -cq 19` 8-bit | 0.8 s | 6.3x | 5.6 MB | 9 Mbps |
| `libx265 -crf 12` 10-bit (in use today) | 5.2 s | 1.0x | 4.5 MB | 7 Mbps |
| `prores_ks` standard | 0.9 s | 5.7x | 45.2 MB | 72 Mbps |
| `prores_ks` HQ | 0.9 s | 5.3x | 54.4 MB | 87 Mbps |
| `prores_ks` 4444 | 1.2 s | 4.1x | 68.2 MB | 109 Mbps |
| `ffv1` L3 10-bit 4:2:2 | 0.6 s | 8.4x | 76.7 MB | 123 Mbps |
| `utvideo` | 0.5 s | 10.1x | 111.2 MB | 178 Mbps |

Against SeedVR2 at seconds-to-minutes **per frame**, all of these disappear into the noise.
So if this is ever revisited, do not spend time on encoder performance: it is solved. Spend
it on the storage warning and on the path bookkeeping below.

### The implementation cost, for whoever revisits it

Not the encoding. The **bookkeeping**, because `_output_path` hard-codes `.mp4`:

- The precedent is good: `_engine_tag` already appends `_realesrgan` for a non-default
  engine and leaves the default engine's paths byte-for-byte unchanged, so a format suffix
  plus a format-dependent extension follows a tested pattern.
- The cost lands in `reconcile_outputs_from_disk` (cross-install "already upscaled"
  adoption), which loops `for target in ve.ALL_TARGETS` and calls
  `_output_path(output_root, rel, target)` **with no engine argument**. So it only ever
  adopts default-engine, default-format outputs. A format dimension **compounds an existing
  gap rather than creating a new one**: Real-ESRGAN outputs are already invisible to
  cross-install adoption today. Either that loop becomes a product of
  (target x engine x format), or the gap gets documented as deliberate.
- Conciliation matches videos by content-hash lineage only, so a format change does not
  break matching there.

### `.mxf`

Skipped for a simpler reason: it is a broadcast container, and a home user reviving family
footage is unlikely to have one. Nearly free to add (`.mxf` demuxes fine and its ProRes/DNxHD
payload stream-copies into the `.mkv` segments like the others), so it costs one line
whenever someone actually turns up with one.

**Revisit if** users ask for an editable master, i.e. people are upscaling footage in order
to *edit* it rather than to watch it. That is a change in what the app is for, and it would
be visible in the asking. If it happens, build **ProRes only** (the intermediate half): it
serves the real request, and FFV1's "nothing was lost" claim cannot be made honestly while
the model path is 8-bit RGB.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Processing alpha, multi-page and high-bit-depth images (2026-07-29)

**Actually supporting** the image variants the pipeline currently mangles: preserving
transparency through an upscale, handling every page of a multi-page TIFF, and writing 16-bit
output for a 16-bit source.

**What shipped instead:** roadmap **#17** (0.5.9-experimental), which treats these files as
**not images** and leaves them untouched, the same way non-media files are already left
untouched. That is a deliberate holding position, not a verdict on the formats, which is why
this entry exists.

**The decision, in the author's words:** at this stage of development it is safer to skip
these file types than for the author to decide on an aspect he knows too little about. The
app already ignores non-image files during Conciliation, so the mechanism is there.

That is the right instinct and it is worth writing down why: the failure being avoided is
**silent**. Today a transparent PNG is upscaled to an opaque one with the *same name and
extension*, so Conciliation's mirrored-name fallback matches it with full confidence and
reports an ordinary "replaced". A wrong guess about how to handle alpha would not look like a
bug; it would look like a completed run.

### What each one would actually require

Kept because each is a different problem, and lumping them together is what makes the work
look small:

| | The real question | Not just plumbing because |
|---|---|---|
| **Alpha (RGBA/LA PNG, WebP)** | Does SeedVR2 upscale the alpha channel, or is alpha upscaled separately and recomposited? | The model takes 3-channel RGB. Upscaling alpha separately (bicubic, or a second pass) gives a matte that no longer matches the generated colour edges, and a generative model *invents* edge detail, so the mismatch is worst exactly where alpha matters. The premultiplied-vs-straight distinction also has to be got right or edges fringe |
| **Multi-page TIFF** | Is a page a separate image, or a document? | `_load_image` takes `tensor[0]` today. Scanned documents, bracketed exposures and layer stacks are three different intents behind the same container, and the right output (N files? one multi-page TIFF? page 0 only?) differs for each |
| **16-bit TIFF / `I;16`** | Is there anything to preserve? | The model path is **8-bit internally** (`_load_image` normalises to float from 8-bit; the video engines feed 8-bit RGB). Writing a 16-bit output from an 8-bit-sourced result is a container claim, not extra information. Honest 16-bit support means changing the model I/O path, which is a much larger job than the file format |

### The cheap half that is real, if this is revisited

**Alpha is the only one with a genuine consumer use case** for this app (a logo or a cut-out
someone wants enlarged), and the *pragmatic* version is much smaller than the correct one:
upscale RGB through the model, upscale the alpha channel separately with plain Lanczos, and
recomposite. It is wrong at the edges by construction, which is precisely why it should not
be the silent default, but as an explicit, labelled option it would serve the case.

**Revisit if** users actually ask, i.e. someone reports skipped PNGs as a problem rather than
as the expected message #17 prints. A second trigger would be the model I/O path moving
past 8-bit for some other reason, which would make the 16-bit row worth revisiting on its own.

Until then the skip is correct and, importantly, **reversible**: nothing is written, nothing
is lost, and the skip reason is printed per file so the affected users are self-identifying.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Managing the network volume via the RunPod S3 API (2026-07-29)

> Research the RunPod S3 API (<https://docs.runpod.io/storage/s3-api>). This method might
> allow adding or removing models from the RunPod network volume without having to spin up
> a pod.

Researched 2026-07-29 against RunPod's S3 and network-volume docs, and against
[`pod/provision.sh`](../pod/provision.sh). **Dropped.** Nothing was built.

**The decision, in the author's words:** not worth the effort to implement, for this
particular application. **Removing models is rare enough.**

That is the right weighing, and the frequency argument is the load-bearing one: the two
things the API does well (inspect the volume, delete a weight file) are both things this
app does a handful of times per install, while the cost is permanent surface area plus a
second credential.

**Revisit if** either the volume becomes something users touch often, or a *free* variant
appears: if the S3 credentials ever merge into the ordinary RunPod API key, the main
objection below disappears and the read-only half becomes close to free.

### What the API is, so it is not re-derived

An S3 front end onto an existing network volume, usable with **no compute attached**.
Endpoint `https://s3api-<DATACENTER>.runpod.io/`, `--region <DATACENTER>`, bucket name =
the network volume ID, and pod path `/workspace/x/y` maps to `s3://<VOLUME_ID>/x/y`.
Supported: `Get/Put/Head/Copy/DeleteObject`, `ListObjects(V2)`, the full multipart set.
**Not** supported: batch `DeleteObjects`, pre-signed URLs, versioning, bucket
create/delete, ACLs, tagging. 500 MB single-PUT ceiling, so every DiT weight needs
multipart. Fifteen datacenters, EU-RO-1 among them.

### Why "add models without a pod" fails, in descending order of how fatal it is

**1. Provisioning is not file placement.** This is the ceiling on the whole idea, and no
API feature moves it. `provision.sh` produces on the volume: a Python venv built
`--system-site-packages` **against the pod image's exact torch** (stamped by that torch
version, thousands of files, Linux `.so`s, absolute paths, exec bits); the ollama runtime
as Linux ELF binaries plus its `lib/ollama` GPU runners; and a `chmod +x`'d static ffmpeg.
S3 cannot set an exec bit, and none of it can be constructed on Windows. **A from-scratch
provision will always need a pod.**

**2. The bandwidth direction reverses, and that is the expensive part.** Today the pod
pulls ~40 GB (three DiT tiers ~26 GB plus three vision tiers ~11 GB) from HuggingFace and
ollama.com over a datacenter link, while a cheap card (RTX 2000 Ada, ~$0.24/hr) bills for
20 to 40 minutes: **$0.10 to $0.16 in total**. Via S3 the same bytes route HuggingFace to
the user's house to RunPod, on a home upload link. That trades roughly fifteen cents for
hours. The measurement that would decide any upload-side variant is the **WAN upload**
throughput (not the 10G LAN): ~1 Gbit symmetric makes 40 GB a 6-minute job and flips the
calculus; 100 Mbit makes it an hour and settles it.

**3. Ollama's store is undocumented and blob-shared.** `OLLAMA_MODELS` is content-addressed
(`models/blobs/sha256-<hex>` plus `models/manifests/.../<model>/<tag>`), a layout Ollama
has changed before, and blobs can be shared between manifests. `ollama pull` / `ollama rm`
on a pod handle that by construction; raw S3 object deletes do not, so deleting the wrong
blob silently breaks a *different* model. **Ollama models stay a pod job**, permanently.

The one S3-friendly artifact is **SeedVR2 weights**: flat `.safetensors`/`.gguf` files in
`models/seedvr2/`, no exec bit, no layout magic, and `download_weight` already skips files
that are present and valid.

### What was genuinely on the table, and is what would be built if this returns

Recorded because it is not what the question asked for, and it is the better half:

1. **Pod-free volume inspection.** `ListObjectsV2` plus `HeadObject` answer, for zero cost
   and no compute: which DiT and vision tiers are cached, how much of the 50 GB is free,
   and **whether the model just picked in Settings is actually on the volume**. That last
   one is money-adjacent: today a mismatched pick is discovered at run start, on a billed
   pod, and three HTTPS calls would turn a billed failure into a dialog.
2. **Pod-free deletion of obsolete SeedVR2 weight files.** One `DeleteObject` per path,
   versus deploying a billed pod to run a two-second `rm`. This is the half the author's
   "rare enough" applies to directly.

### The costs that decided it

- **A second credential, which is the main objection.** The S3 key is separate from the
  RunPod API key, created on a different console page, and shown only once (access key =
  the RunPod user id `user_***`, secret `rps_***`). It would mean new config keys, another
  entry in `config_store.SECRET_FIELDS`, Settings fields plus a Test button, and one more
  onboarding step for a **non-technical user**, which cuts against what the first-start
  wizard exists to do.
- **`boto3` is not an option.** botocore ships ~100 MB of service JSON and would become the
  heaviest dependency in the project, for four HTTP verbs. The right shape is hand-rolled
  SigV4 over `urllib` (~120 lines for List/Head/Delete/Get, `hmac` + `hashlib` + `datetime`,
  `context=net_ssl.ssl_context()`), in the same spirit as the ctypes `ITaskbarList3` and
  the urllib GraphQL client. Cheap, but it is still a new module to own.
- **The pain it would remove was already mostly removed.** Incremental provisioning (0.5.5)
  keeps every valid artifact and re-fetches only what changed, so a re-provision is already
  cheap. What is left is the 20-minute pod for the inspect and prune cases, which is
  exactly the "rare enough" workload.

### Traps, if anyone does pick this up

- **Never list the whole bucket.** `ListObjects` degrades past 10,000 files or 10 GB in a
  directory (slow, or repeated-next-token errors while ETags are computed). `/workspace/venv`
  is precisely that shape. Always list under a narrow prefix.
- **The S3 datacenter list is not the storage-capable datacenter list.** The region picker
  offers storage-capable DCs from the live GraphQL list; the S3 endpoints are a different,
  shorter fifteen. CA-MTL hosts volumes but has no S3 endpoint. So the endpoint needs a
  lookup table with a graceful "not available in this data center" miss, shaped like
  `region_of`, never an assumption. The volume's DC is already known, so no new user input
  is needed to derive it.
- **Concurrency is undocumented, and there is already a scar here.** A volume cannot attach
  to two pods, and nothing states whether S3 writes are safe while a pod holds it.
  `provision.sh` already carries a workaround for a venv held open by another pod's file
  handles. Any mutation would have to be gated on "no toolbox pod running", reusing
  `_find_existing_pod`.
- **Smaller ones:** requests with more than 1 h of clock drift are rejected (more lenient
  than AWS's 15 minutes, but a wrong system clock is a plausible support ticket, so the
  error should name it); uploads do **not** pre-check free space and fail partway on a full
  volume, which List makes pre-emptable; `aws s3 sync` is documented as unreliable at
  scale; object names containing `#` need URL encoding.
- Storage pricing confirmed at **$0.07/GB/mo** for the first TB, so the 50 GB model volume
  is ~$3.50/mo. That is the figure behind the standing "a volume bills monthly even when
  idle" tooltip.

<div align="right"><a href="#dropped-ideas--constraints">↑ Back to top</a></div>

## Folding a RAW render back into the source tree (2026-07-30)

Roadmap **#19** decision 8 had two halves. Conciliation must never archive or delete a RAW
original (**shipped**, explicit and tested), and it should **fold the render in alongside**
the negative, ending with `IMG_1234.CR2`, `IMG_1234.JPG` (the camera's own) and the app's
render in the same folder. The second half is **dropped**. Nothing was built for it.

**Why.** Two independent reasons, and the second is the one that actually settles it.

**1. It is a new destructive-tool mode bought for a filing convenience.** Conciliation has
only ever *replaced*. Adding "move this extra file in alongside" means a new preview count, a
case-insensitive collision check (Windows: a pre-existing `_raw.jpg` and `_RAW.JPG` are one
file), idempotence so a second run cannot stack suffixes, and a new action type in the #18
undo journal. That is real surface area inside the app's only destructive tool, and its
entire payoff is that a file the user can already see sits in a different folder.

**2. RAW input turned out to be a scaffold, not a workflow.** The measurement behind #19
(`raw-preview-survey.csv`, 24 CC0 camera files) found that at the shipped 4K target **zero of
them would ever be upscaled** - RAW is a high-resolution format and this app targets
low-resolution photos. So what the feature actually does today is **render**: it makes
negatives viewable, which they otherwise are not, anywhere. The output of a render belongs in
the output folder, next to every other thing the app produced. Folding it back into the
source tree is a gesture that only makes sense for the *upscale* workflow, and for RAW that
workflow currently has no members.

**Where it leaves things.** The render stays in the processed folder, which is exactly where
it is today and where `Browse upscaled…` already shows it. The user copies it out if they
want it filed with the negative - a one-time drag, against a permanent code path.

**Revisit if** the upscale half of RAW input starts firing for real, which needs a reason for
a RAW to be below target. The concrete one is an **8K resolution target**: at 7680x4320 most
of the survey corpus becomes eligible overnight, RAW stops being a render-only format, and
"the upscaled version should live beside the negative" becomes a real request rather than a
tidiness argument. A second, weaker trigger is a user actually asking for it.

**One naming decision is parked with this**, and must be re-taken rather than inherited if it
returns. The render is `<stem>_raw.jpg`, chosen so a RAW and its sibling camera JPEG cannot
map to the same output. #19's original text said the folded-in file should be
`<stem>_upscaled.jpg`, which was written when the plan assumed RAW files get upscaled; on the
measured behaviour that name would be **false on nearly every file**.

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
