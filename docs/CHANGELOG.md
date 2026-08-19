# Changelog

User-facing release notes ship as the **annotated git tag message** (CI publishes
them as the GitHub Release body, and the in-app updater shows them). This file is the
working draft those notes are distilled from, and it records **experimental**,
in-development versions before they are tagged. For released versions, see the GitHub
Releases page.

## Contents

- [0.6.0](#060)
- [0.5.9](#059)
- [0.5.8](#058)
- [0.5.7](#057)
- [0.5.5](#055)
- [0.5.4](#054)
- [0.5.3](#053)
- [0.5.2](#052)
- [0.5.1](#051)

---

## 0.6.0

### Steady up shaky old footage
A new **Video Stabilization** tab, next to Conciliation. Point it at a folder of shaky
videos, press **Scan folder**, then Start. It never touches your originals: each steadied
version is written as a new file.

Each video is steadied **whole**, one after another, because steadying works by watching the
camera move across the *whole* clip and then smoothing that path: chopped into pieces, every
join would jolt. It runs in two passes for that reason (the first watches, the second
writes), and together they take a little under the length of the video itself. There is no
GPU involved and nothing is rented.

**You can stop and come back.** Videos that already have a result are listed as
*Stabilised* and skipped, so scanning the same folder again simply picks up where you left
off, however long later, and however the run ended. If you stop mid-run, the video it was
working on goes back to *Queued* and the rest are untouched. One video that cannot be read
does not cost you the rest of the batch: it is marked *Failed*, with the reason on the
right-click menu, and the run carries on.

**Watch the before and after, with sound.** Double-click any finished video and the
original and the steadied copy play side by side. That is the view that answers "is it
actually steadier", because steadiness is something you can only see in motion: a frozen
pair mostly shows you that the frame has moved. The still before/after wipe is on the
right-click menu too, and it is the one for a close look at the *edges*, where the frame
is filled in from nearby moments.

You can also send a video straight over from the **Video Upscaler**: right-click it in the
list there and choose *Stabilize…*.

**It keeps your whole picture.** Steadying normally works by zooming in far enough that the
wobbling edges never show, which quietly throws away the outer part of every frame: measured
on real camcorder footage, about **a fifth of the picture**, and the amount is set by the
single worst jolt in the whole video. This app does not do that by default. Instead it keeps
the full frame and fills the edges in from nearby moments, so at worst a sliver at the very
edge looks a moment stale. If you would rather have the zoom, there is a tick-box, labelled
with what it costs.

**Steadiness** is adjustable. Higher steadies more but pulls harder on the picture; lower
follows the original camera movement more closely. Remember that a deliberate pan or a slow
zoom is not shake, so if a result looks like it is fighting the camera, lower it.

Interlaced footage (old MiniDV camcorder tapes) is de-interlaced automatically first, since
otherwise the measuring pass is reading two different instants woven into one frame.

**Stabilise first, then upscale**, if you want both: that way the Video Upscaler's
resolution target applies to the finished framing.

Each result is named after the video it came from, and **never over an existing file** —
if you deliberately steady the same video again (right-click → *Stabilise again*), you get
a second file, so the first one is still there to compare against. Two videos with the
same name in different folders get two results, not one.

**Where results go** is up to you: leave "Save results to" empty and each one is written
next to its own video, or give it a folder and they all collect there. A separate folder
is the tidier choice, because it keeps results out of the list the next time you scan.
Both fields have a **Save as Default** button, and **Settings → Default folders** has the
same pair. That whole section is now listed in the same order as the tabs.

One thing worth knowing: the app now downloads a **newer ffmpeg** than before. Every ffmpeg
in the 8.1 series has a bug in its stabilisation filter that makes it produce a slightly
different, and wrong, result every time it runs, usually with no crash and no warning. It
was fixed upstream in April. The app bundles a build with the fix, and it checks the filter
is behaving before it will touch your video, so it can never hand you a quietly corrupted
result. If you have an older install, run `Image Toolbox.cmd` once and it will replace the
bundled ffmpeg for you.

### A magnifier in the comparison window
The comparison window has a new **Lens** tick-box. With it on, the spot under your mouse
is shown twice, side by side: the original on the left, the upscaled version on the
right, both blown up. You see the difference in one look, instead of sliding the divider
back and forth and trying to remember what was there a second ago.

The right-hand half is shown at the upscaled file's **real pixels** (the labels say so:
"Upscaled · 1:1"), and the left half is blown up by exactly as much as the upscale was,
so what you are comparing is what you actually got.

**Scroll to zoom the lens**: 1x, 2x, 4x, 8x, and the two panels grow with it, so on a
big monitor you get a proper look instead of a postage stamp. The labels always tell you
where you are ("Original · 16.0×" / "Upscaled · 8:1"). The panels stop growing before
they cover the picture they came from; past that point zooming keeps going by showing a
smaller patch. If the picture is small (an old 320x240 video, say) the window is already
blowing it up, so the lens **starts at a zoom that is at least as strong as what you are
already looking at** rather than at a useless 1:1. Ctrl+scroll still zooms the picture
behind the lens.

**Click the picture to pin the lens** where it is, so you can take your hand off the
mouse, look properly, or take a screenshot. Click again, or press Esc, to release it.
Without a pin it simply follows the mouse and disappears when you move away. The divider
comes back the moment you untick Lens. The tick-box and the zoom are remembered for next
time, and the shortcut is **L**.

**Esc steps back out one level at a time**, starting with whatever you turned on last: it
releases a pinned lens, then turns the lens off, then closes the window. So from a pinned
lens it takes three presses to leave, and a stray press never throws away more than the
one thing you just did.

It works the same way on the video comparison window, on whichever frame you have
stopped at.

### Look through photos you upscaled earlier
Until now, comparing a photo with its original only worked **while the run that made it
was still on screen**. Close the app, or start another batch, and the thumbnails went
away with it, even though both files were still sitting on your disk. So the one view
that actually shows what the app did for you was also the one you could not go back to.

The Batch Upscaler tab has a new **Browse upscaled…** button. It opens a window over
your output folder: your folders on the left, a wall of thumbnails on the right, 200 at a
time (use the arrows to turn the page, and the +/- buttons to make the thumbnails bigger
or smaller). **Double-click any photo** and the comparison window opens on it, divider
and lens included, exactly as it does during a run.

It works out which original each upscaled photo came from **on its own**, including
photos that Tag & Rename has since renamed, and it does that by looking at the folders
themselves. So it still works on photos upscaled months ago, on a folder upscaled by
another PC, and even if the app's cache was deleted.

A green frame means the original was found and the photo can be compared. If you have
**moved or renamed originals** since upscaling them, tick **Match by content** and the
app will pair the leftovers by reading the files themselves instead of going by name.
It is a tick-box rather than something that always happens, because reading files takes
time and most folders do not need it. A photo with no original found is not a problem:
it simply has no frame, and double-clicking opens it in your usual image viewer.

The bottom of the window tells you what you are looking at ("263 upscaled image(s) in 4
folder(s)"). The main window steps aside while you browse and comes back when you close
the browser, and the button is unavailable during a run, so a batch in progress can never
disappear behind it.

One thing it does not do yet: if you have already **conciliated** a folder (replaced your
originals with the upscaled copies), the output folder is empty afterwards, so there is
nothing left there to browse. The window says so rather than looking broken.

---

## 0.5.9

Three fixes to things the app was quietly getting wrong with your files, plus an undo
for the one tool that could not be taken back. Nothing to turn on: it is all on by
default.

### Conciliation can be undone
Conciliation was the only tool in the app that changed your original folders, and the
only one you could not undo. Now every run is recorded as it happens, and **Undo last
run** puts everything back: each original comes back out of `__Archive__`, each
upscaled file returns to the processed folder, and your folders look exactly as they
did before. It still works after closing and reopening the app.

It will not overwrite anything to do it. If you have edited one of the files since the
run, or something else has taken a file's name, that one is left alone and named in the
log; the rest are still put back.

**A Delete run cannot be undone** and the button says so instead of pretending: deleted
files are gone. What the record can still tell you is exactly which originals were
removed, and it does.

### Your photo's details now survive the upscale
Until now an upscaled image came out with **no information at all**: no capture date,
no camera, no lens, no GPS, no copyright. That mattered most for the capture date,
because the upscaled copy then sorted by the day the file was written rather than the
day the picture was taken, and once Conciliation replaced the original the real date
was gone for good.

The Batch Upscaler now copies all of it across, and fixes two details while it does:
the sideways-photo tag is reset (the pixels are already upright, so leaving it would
make your viewer turn the photo twice), and the tiny stale preview thumbnail is
dropped instead of showing you the old image at the old size.

**Photos you upscaled before this** are not lost. Conciliation now repairs them at the
one moment it holds both files, just before it archives or deletes the original: it
copies in every field the upscaled version is missing and changes nothing it already
has, including the description Tag & Rename wrote. The preview tells you how many will
be repaired and repairs nothing itself. It works on JPEG and PNG without re-saving any
image quality.

Turn it off in **Settings → Batch Upscaler → Copy metadata from the original** if you
deliberately want scrubbed copies to share, with no GPS and no camera.

### Images the upscaler cannot reproduce are left alone
The upscaler works in plain 8-bit colour, so it cannot keep transparency (a see-through
PNG or WebP), the extra pages of a multi-page TIFF, or 16-bit colour depth. It used to
hand back a flattened copy **under the same name**, which Conciliation then treated as
a perfectly good replacement and archived (or deleted) the only copy that still had
those things.

Those files are now recognised, skipped with the reason spelled out ("would lose
transparency"), and listed individually. Conciliation refuses to replace them too,
including files you upscaled before this change.

### The app no longer re-processes its own output
The app writes its results inside the folder it scans (`__upscaled__`,
`__Archive__`). After an archive-mode conciliation, the Batch Upscaler found every
archived original and upscaled it all over again, Tag & Rename re-tagged them, and the
Video Upscaler re-queued them. Nothing was lost, but it wasted hours, and on a rented
GPU it wasted money. Every scan now skips the folders the app created itself. Pointing
a tool **at** one of those folders still works, and is the supported way to tag an
upscaled tree.

Because of that, Tag & Rename has to be pointed at the **upscaled** folder rather than
at your originals, so it now fills that folder in for you: it suggests whatever the
Batch Upscaler tab is set to save to, or the default folder you pinned in Settings.

<div align="right"><a href="#changelog">↑ Back to top</a></div>

---

## 0.5.8

### One "Run on" row on every GPU tab
The three tools that use a GPU (Batch Upscaler, Tag & Rename, Video Upscaler) now ask
the same question in the same place, in the same way: a **Run on** picklist (*Local GPU*
or *Remote: RunPod*) with a **GPU picker** beside it. It replaces the image tabs'
"Run on remote pod (RunPod)" checkbox and the Video Upscaler's Local/Remote radio
buttons.

The picker is now useful in **both** modes. Remote lists the live RunPod catalog as
before (prices, stock, cheapest first). Local lists **your own NVIDIA cards**, which is
new: on a machine with two cards you can send a run to a specific one and leave the
other free for something else. With a single card there is nothing to choose and nothing
changes.

If your install is Local-only or Remote-only, the picklist shows the one mode you can
run and explains why it is fixed. The GPU picker next to it stays live, because there is
still a card to pick.

- **Tab order** is now Batch Upscaler, Tag & Rename, Video Upscaler, Conciliation,
  Settings, RunPod: the three GPU tools sit together, and Conciliation (which runs after
  them) follows.
- **The window's minimum width went from 900 to 1200 px.** The video queue's columns
  need it; below that the *Segments* column was clipped.
- **Settings** got a layout pass around the same controls: the *Upscaling* section is
  now *Batch Upscaler*, its value boxes line up, and the Video Upscaler section is laid
  out three-across. The stray SeedVR2 *Model* row is gone: there is one **Model**
  picklist under **Method** that follows the method and lists SeedVR2 weights or
  Real-ESRGAN models. Your two choices are stored separately, so switching method never
  loses the other engine's model.

### The Video Upscaler now reports to Home Assistant
Video runs were invisible to the MQTT integration: while a queue was upscaling, Home
Assistant still read **idle**, the progress/ETA/runtime values sat frozen at whatever the
last image run left behind, and the "last run" summary was never written. So an
automation like "tell me when a run finishes" never fired for the longest, most worth-
notifying runs the app does.

A video run now publishes the same live state the other three tools do:

- **Current task** reads *video upscaling* while a queue is running, and goes back to
  *idle* when it ends.
- **Progress, ETA and runtime** update as the run goes. Progress counts **frames**
  (across the whole queue), since that is what a video run measures in, and the two
  per-item times are **seconds per frame**: the run's running average, and the live
  figure measured on the pod or on your own GPU.
- **Last run** gets a summary when the queue ends: jobs done and failed, how many files,
  how long it took, why it stopped (finished, your Stop, a per-run cap …) and, for a
  rented pod, what it cost.
- **This machine's CPU/RAM/GPU readings keep updating during a remote run.** They used
  to freeze for the whole run, since the app pauses its idle sampling while any task is
  running and a remote video run had nothing sampling in its place.

Nothing to configure: if MQTT is set up, video runs simply start appearing.

### A failed video run now alerts as loudly as it should
Alerts carry a severity: green finished cleanly, orange/yellow needs a look, red failed.
On ntfy that severity sets the notification's **priority**, and on Telegram it puts a
status emoji in front of the message.

The Video Upscaler was using a different set of colour values than the other tools, which
the notification layer did not recognise, so its alerts fell through to "no severity
known": a **failed** video run went out at normal priority with no tag and no emoji. In
other words the one alert most worth noticing, from the tool whose failures cost money,
was the quietest one the app could send. It now arrives red, at maximum priority.

The colours are named constants shared by every tool, and a test refuses any raw colour
value in a runner, so the two palettes cannot drift apart again. New
[`docs/notifications.md`](notifications.md) covers the three backends, which to pick, the
setup steps and what each alert contains.

### Home Assistant alerts without an MQTT broker
A fourth notification backend: Image Toolbox can now POST each alert straight to a
**Home Assistant webhook**, so a Home Assistant user who does not run an MQTT broker
still gets told when a run finishes or fails. Nothing leaves your network, there is no
account and no token, and your automation decides what to do with it: the payload
carries a ready-written `message` (so the automation is one line) and a `level` of
success / caution / warning / error, which is what makes "only buzz me for bad news" a
one-line condition. A paste-ready automation ships in
[`samples/home-assistant/automation-webhook.yaml`](../samples/home-assistant/automation-webhook.yaml).

Set it up in **Settings > Notifications**: your Home Assistant address, and a webhook ID
you invent. **Create the automation in Home Assistant first**, then fill those in. That
order matters, and the honest reason is worth stating: Home Assistant deliberately
answers "200 OK" to a webhook ID it has never heard of, so pressing Test before the
automation exists looks exactly like success. The Test button therefore says only what it
can prove: that Home Assistant answered. It never claims the alert was delivered, and no
"verified" tick is stored anywhere. [`docs/notifications.md`](notifications.md) has the
walkthrough, the payload, and a one-minute way to confirm it really works from the Home
Assistant side.

**If you already run a broker, prefer the MQTT integration.** It is a superset: live
progress, telemetry, dashboards, and the one thing a webhook can never do, telling you
the app **crashed** mid-run (that alert comes from the broker, and a crashed app cannot
send anything itself).

### Home Assistant: ready-made notification automations, and no more phantom alerts
There is now a [`samples/home-assistant/automations-ui.yaml`](../samples/home-assistant/automations-ui.yaml)
with five ready-made automations: **a run finished**, **a run finished badly**
(failures, an early stop, a degraded GPU), **the app died mid-run**, an optional **a
run started**, and an alternative form of the first one. Each is a self-contained
block you paste into Home Assistant's own automation editor (**Edit in YAML**), so
you never touch a configuration file: no leading dash, no `id`, nothing to reload.
Plus a Notifications section in that folder's README explaining the one trap in the
whole setup.

That trap, and what changed in the app because of it: the values Image Toolbox
publishes are *retained*, which is why your dashboard is correct the instant Home
Assistant restarts instead of blank until the next run. The flip side is that a
retained value is re-delivered on every Home Assistant restart and every reconnect,
so the obvious automation ("when the last-run summary changes, notify me") announces
a run that finished days ago, every single time you restart HA.

So the app now publishes a run's start and end **twice**: as retained state, as
before, and as a one-shot **event** that is not retained and never replayed. Trigger
your automations on `image-toolbox/event/run_finished` (it carries exactly the same
summary as `last_run`) and it can only ever fire when a run really finishes. The
sample also shows the retained route with the guards it needs, if you prefer it.

Timestamps the app publishes now carry their UTC offset, so a "did this just happen?"
condition is right even when Home Assistant runs in a different timezone than the PC
(a container defaulting to UTC would previously have made that check silently wrong by
hours, in either direction). And a **Conciliation** run's summary now reports the same
"processed / failed / how long" fields the other three tools do (its own
replaced/conflicts/errors counts are still there), so one automation genuinely covers
all four tools instead of reading "0 processed" for that one.

### Home Assistant samples: version markers, and a progress bar
The files in `samples/home-assistant/` are pasted into your own Home Assistant
configuration, so a new version of the app cannot update them for you. They now carry
**version markers**, so an upgrade means copying a marked block rather than diffing the
whole file against yours: `# --- Added in 0.5.3 ---` on a new sensor,
`# --- Changed in 0.5.3 ---` on one whose YAML must be replaced, `# NEW: version 0.5.8`
on an automation, and `# 0.5.8:` on something that already exists and only gained a
meaning (nothing to re-copy). The folder README has an *Upgrading* table saying which
files are patched and which are simply re-pasted.

For 0.5.8 itself: the sensors need **no** change (the `task/*` ones you already have
just start filling in during a video run, counting frames rather than files). There is
one new derived sensor, **Task Progress Percent**, which turns the `8412/95160` a video
run reports into something a gauge can show, and both dashboards gained a progress arc
that uses it plus last-run detail rows (processed, failed, and what the pod cost).

### A "Buy me a coffee" link
The bottom status bar now carries an optional support link next to **Report an issue**.
It is a link and nothing more: clicking it opens buymeacoffee.com in your browser, the
app never contacts that site by itself, and there is no counter, ping or tracking of any
kind. It appears in that one place only, never as a popup, a setup step or a prompt after
a run. The app is free and stays free.

---

## 0.5.7

Video Upscaler UI/UX polish, focused on the remote GPU picker and the queue.

### A job-aware remote GPU picker
The GPU list now reflects the video you are about to add, not just what is already
queued:

- It lists **only cards that can actually run the job**. For SeedVR2 that means the
  selected target's VRAM floor: a 16 GB card is no longer offered for a 4K SeedVR2
  upscale it can't run. A card you benchmarked for SeedVR2 still shows for the sizes
  it proved. (A Real-ESRGAN benchmark of the same card does not count: a GAN tiles on
  out-of-memory and reaches sizes SeedVR2's diffusion can't, so that success was
  wrongly whitelisting small cards.)
- It **no longer hides valid cards once the queue has items**, so you can pick a
  different pod for each video (which was the point of per-video GPU binding).
- **Prepare is disabled when no GPU is selected**, so a video can't be queued with no
  card to run it on.

### Queue and segment-extractor consistency
- The **"#" position column comes first**, and **Remove** now removes every selected
  row (multi-select works).
- The queue's **GPU column shows the local card's name** ("Local RTX 3090") for local
  jobs instead of being blank.
- The **Extract Segment** window now inherits the picked GPU and Method and offers
  only the targets that card can reach, and its clips are queued bound to that GPU
  (so they route to the right pod), instead of being queued with no GPU.

### Smaller clarifications
- The **Run on** switch (Local / Remote) **locks while the queue is non-empty**: a
  queue is one mode for now, so this prevents a half-built queue in the wrong mode.
- Clearer wording: the remote-ready line names the **network volume** region (only
  SeedVR2 needs it; Real-ESRGAN's pod is volume-free), queue tooltips no longer imply
  a strict run order (jobs are grouped by method + GPU at Start), and each telemetry
  row's hint says it expands to a usage graph only while a run is in progress.

---

## 0.5.5

Better Tag & Rename descriptions, and much smarter remote provisioning.

### New default vision model: qwen3-vl:8b-instruct
A 100-image benchmark (RTX 3090) had the **qwen3-vl** family beat the old
`qwen2.5vl:7b` / `minicpm-v` / `gemma3:4b` picks at every size, with clearer, more
detailed descriptions. The new defaults, chosen by the first-start wizard to fit
your card:

- **24 GB+**: `qwen3-vl:8b-instruct` (the clearest; the new overall default)
- **16 GB**: `qwen3-vl:4b-instruct`
- **8-12 GB**: `qwen3-vl:2b-instruct`

Every model stays selectable in Settings. See the benchmark table in the README and
`docs/tag-and-rename.md`.

### Faster tagging via an Ollama context cap
Newer vision models declare a huge native context (qwen3-vl = 256K), and Ollama
sizes its VRAM off that, so uncapped they grab almost the whole card and thrash
(the 8B model ran 9:11 per 100 images at 98% VRAM). Tag & Rename now caps the
context (`tagging.ollama_num_ctx`, default 8192): the same run drops to **2:37 at
43% VRAM** with no quality change. Applied automatically, local and remote.

### Smarter remote provisioning
Provisioning the RunPod model volume is no longer a wasteful all-or-nothing job:

- **Caches the common model set** so you can switch models with no re-provision:
  all three vision tiers **and** all three SeedVR2 upscale tiers (3B Q8 / 7B
  FP8-mixed / 7B FP16) now fit the 50 GB volume.
- **Follows your configured models** (`ollama.model` and `upscale.dit_model`) so the
  model you actually picked is guaranteed on the volume (previously it silently
  provisioned a fixed default).
- **Incremental & self-pruning re-provision:** keeps whatever is already valid and
  fetches only what changed (the python venv is skipped via a stamp, the cached
  Ollama runtime is reused, weights skip valid files), and prunes obsolete models to
  reclaim storage. So a model change or a minor update is a cheap re-provision, not a
  fresh volume and a full re-download.

### Note on the Remote Tag & Rename cost table
The README's Remote Tag & Rename cost figures were measured with the old
`qwen2.5vl:7b` and are now marked obsolete pending fresh remote benchmarks with
`qwen3-vl:8b-instruct` (the new model ran at essentially the same speed locally, so
costs should be close).

---

## 0.5.4

A small cleanup and fix release.

### Fix: the Video Upscaler now shows telemetry on a local run
Running the Video Upscaler on your **own GPU** (not a rented pod) showed no
telemetry at all: no CPU / RAM / VRAM / temperature row under the carousel, and
nothing to click for the usage graph. The row is now there and updates live while
a local run works, and clicking it opens the same per-run usage graph (0.5.3) the
other tools have. A remote-pod run is unchanged: it still shows the pod's own row.

### Telemetry graph window polish
- The graph window now opens at the **same size as the main window** and won't let
  you shrink it below that, so the four charts always have room.
- It **remembers its size and position** between openings and across restarts, like
  the log and comparison windows.
- The range bar is cleaner: the active range button is now shown in **bold** instead
  of a separate "Showing: last Xh" caption.

### Documentation
- The README and every document under `docs/` gained a **Contents** list at the top
  and "Back to top" links, so the longer ones are easier to navigate on GitHub.

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.3

### See what a run is doing to your machine: live telemetry graphs
The little telemetry row under the image carousel (CPU / RAM / VRAM / GPU) has
grown up. **Click any telemetry row** and a graph window opens, plotting the whole
run over time.

- **Four graphs:** GPU and CPU load, memory (VRAM and RAM against how much your
  card/PC actually has), GPU power, and GPU temperature. Move the mouse across them
  for an exact readout at that moment.
- **Honest scale:** the memory and power graphs are pinned to your hardware's real
  limits, so a run that fills the card sits right at the top of the chart, not
  rescaled to look half-empty.
- **Starts when work starts:** the graph begins at the first image or video
  actually processed, so the long "scanning your folders" phase at the start does
  not show as empty space.
- **Range buttons** (1h / 3h / 6h / …) let you zoom into the most recent stretch of
  a long run; they light up as the run gets long enough. When a run ends the graph
  freezes so you can still review it.
- Works for both a **local** run and a **remote pod** run (click the matching row).

### More detail in the telemetry
The row and the graphs now also show **GPU utilization** (how hard the card is
actually working, not just how full its memory is), plus **power draw** and **core
clock**. These also go out over MQTT for Home Assistant.

### Ready-made Home Assistant dashboards
If you use Home Assistant, there are now **paste-in dashboards** under
`samples/home-assistant/`: a simple one that works on any install with no add-ons,
and a fancier one using popular HACS cards. Each pastes into a single dashboard
card (no risky whole-dashboard editing). Screenshots and a step-by-step README are
included.

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.2

### Pause now frees your graphics card
Pause used to stop the queue but keep the AI models loaded, so the card stayed
occupied and you could not go and use it for anything else. It now unloads
everything and hands the memory back, then reloads when you press Resume. The
queue is kept, so nothing is re-scanned and no work is repeated.

- **Batch Upscaler:** pausing releases the upscaling models (measured: 16.6 GB
  returned on an RTX 3090). The first image after Resume takes a little longer
  while they reload.
- **Tag & Rename now has a Pause at all**, which it never did. It shares one
  button with the old "Resume after error": it reads **Pause** while tagging,
  **Resume** while paused, and **Resume after error** when a run is held because
  the vision model kept failing. Pausing unloads the vision model.
- A pause frees **every** loaded model, including the small auto-straighten one.
  No exceptions to remember.
- Remote runs are unchanged: those models live on the rented pod, so unloading
  them would free nothing on your PC.

### Hover help on every control
Every button, checkbox, picklist and list on all six tabs now explains itself on
hover. The wording avoids jargon, and anything that costs money or changes files
says so plainly: Conciliation's Delete cannot be undone, a RunPod volume keeps
billing monthly even when idle, the Video Upscaler's Stop abandons the segment in
progress. Settings' numeric boxes also state their recommended value.

Buttons that open a window you settle into and work in (Segments…, Benchmark
GPU…, Provision…, the setup wizard) are drawn in bold, to set them apart from
buttons that act where you are.

### Fixes
- The Tag & Rename remote checkbox showed two tooltips stacked on top of each
  other.
- Settings claimed the Video Upscaler runs only on a rented pod. That stopped
  being true in 0.5.0, when local video upscaling arrived, and the claim sat
  directly above the Local/Remote switch.
- Video Upscaler: the progress bar now advances within a long segment on a local
  run, instead of appearing stuck until the segment finished.

### Also in this release
- **Run exclusivity:** while any run is active, the other tabs are locked, so two
  runs can no longer fight over the same GPU or the same folders.
- **Video notifications** carry a per-file summary of what finished, rather than
  a bare "done".

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.1

### Benchmark sharing (feature #8, NEW)
Turns the per-card video benchmark into a **crowdsourced dataset**, so a GPU someone
else already measured is not re-swept locally (a sweep is slow and, on a rented pod,
billed). Zero infrastructure: a curated `docs/video-benchmarks.csv` lives in the repo,
the app pulls it anonymously from GitHub, and contributions are delegated to the user's
own GitHub account in the browser (no upload endpoint, no token, no backend).

- **Automatic updates.** The community dataset is pulled and merged in the background
  at every launch (silent, fail-safe, offline falls back to the shipped copy). No
  button, no prompt. Your own measured results always take precedence over downloaded
  ones, which stay advisory (the batch sizer self-corrects a slightly-wrong ceiling).
- **Contribute my results…** (Benchmark GPU window) opens a pre-filled GitHub issue
  with your data. A **multi-select** card picker lets you submit **several GPUs at
  once** in one issue. Two filters keep it clean: only cells you actually measured are
  offered (never other people's downloaded data), and only rows not already in the
  published set are submitted (so benchmarking a little more each day sends only the
  new rows).
- **Export…** saves your results to a CSV file.
- **Maintainer tool** `bench_share.py --merge` curates submissions (dedupe + a
  physical-plausibility sanity gate) into the committed master CSV.

### Video conciliation (feature #5)
Conciliation now handles **videos** as well as images: it matches upscaled video
outputs back into the source tree and archives or replaces the originals, exactly as
it does for photos. Videos are matched by content-hash lineage ONLY (a partial clip
can never be mistaken for a whole-video match); the "never touch originals /
archive-first" guarantees carry over unchanged.

### Benchmark GPU window
- **Benchmark both torch.compile modes in one run:** a "Torch Compile" ON/OFF column
  fronts the results table, and an "Also use Torch Compile" checkbox sweeps the
  compiled and uncompiled regimes back to back (stored under separate keys, so AUTO
  reads whichever matches the real run).
- **Filter + column-sort** for the results table (same UX as the Video Upscaler
  lists): a Torch Compile / Target filter bar, and click any header to sort.
- The main window is now **fully hidden** while the Benchmark window is open (was
  minimized, which a dialog could restore, leaving it reachable behind the benchmark).
- All controls (Start, Stop, Export, Contribute, Report an issue, Close) now share a
  **single button row**.
- The window's **first-ever default size matches the main window** (980x720); the
  remembered size/position still wins after that.

### Video Upscaler
- **Cross-install "already upscaled" detection:** the scan is destination-reconciled,
  adopting outputs that exist in the shared destination folder but are absent from
  this install's local cache. A second machine sharing the same source + destination
  no longer offers to redo videos another install already produced.

### Installer / packaging
- **One application icon everywhere** (`app.ico`): the setup executable, the
  uninstall entry, the Start-menu and desktop shortcuts, and the running window all
  use the same icon (previously the shortcuts used a different icon).
- Ships the seeded `docs/video-benchmarks.csv` so a fresh install has the community
  benchmark dataset offline.

<div align="right"><a href="#changelog">↑ Back to top</a></div>
