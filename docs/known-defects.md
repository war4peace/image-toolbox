# Known defects

Shipped bugs that are **confirmed and not yet fixed**, with the root cause and the shape of
the fix, followed by a short **Fixed** section for ones whose diagnosis is worth keeping (the
code comments point back at these ids). Design questions live in `docs/future-features.md`; ideas that were rejected live in
`docs/dropped-ideas.md`. A defect leaves this file when it is fixed, and the fix is described
in `CLAUDE.md` where the feature is documented.

---

## Contents

- [D1: the app will not start after the system Python is uninstalled or reinstalled](#d1-the-app-will-not-start-after-the-system-python-is-uninstalled-or-reinstalled)
- [D2: NVENC is chosen because the build lists it, not because the machine has it](#d2-nvenc-is-chosen-because-the-build-lists-it-not-because-the-machine-has-it)
- [D4: a local GPU below the 8 GB minimum is offered with no warning](#d4-a-local-gpu-below-the-8-gb-minimum-is-offered-with-no-warning)
- [D3 (fixed): a finished-but-failed Stabilization run looked like a hung one](#d3-fixed-a-finished-but-failed-stabilization-run-looked-like-a-hung-one)

---

## D1: the app will not start after the system Python is uninstalled or reinstalled

**Found:** 2026-08-19, on a VM running 0.5.5, after Python was uninstalled and reinstalled for
an unrelated tool. **Severity: high.** The app does not start, shows nothing at all, writes no
log, and the documented recovery path does not recover it.

### What the user sees

Double-click, and **nothing happens**. No window, no error, no crash log. The app is simply
gone as far as the desktop is concerned.

### Root cause: three layers, each of which alone would be survivable

1. **A venv is not self-contained on Windows.** `.venv\Scripts\python.exe` is a stub; the
   real interpreter DLL and the standard library live at the base installation named by
   `home =` in `.venv\pyvenv.cfg`. Uninstalling that base Python removes them, and
   reinstalling to a different path or a different minor version does not restore them, so
   every executable in `.venv\Scripts` stops working. The app's own files are untouched and
   look perfectly healthy.

2. **The launcher never re-runs the bootstrapper.** `Image Toolbox.cmd` branches on
   `.setup_complete` existing, and that marker is still there, so it goes straight to
   `start "" .venv\Scripts\pythonw.exe`. The process fails at the OS level, **before any of
   our code runs**, which is why `crash_logger` cannot help: it is armed at the top of
   `toolbox_gui.py`, and `toolbox_gui.py` is never reached. Under `pythonw.exe` there is no
   console for the loader error to be printed to either, so the failure is completely
   invisible. `start` returns success regardless.

3. **The bootstrapper would not repair it even if it did run.** Step 3 is
   `if (Test-Path ".venv\Scripts\python.exe") { "Already exists - keeping it." }`. The file
   does exist. It just cannot run. Bootstrap keeps the broken venv and then fails later, in
   step 5, with a pip error that names neither Python nor the venv.

**This is the same class of bug as the ffmpeg pin**, and the second time it has cost us: the
old `if (Test-Path ffmpeg.exe)` check would have kept a memory-corrupting ffmpeg build
forever, and it was fixed with a stamp file plus a *behavioural* health check
(`vidstab_health`). **An existence check cannot detect a broken artifact.** Anywhere the
bootstrapper decides "already done" from a path existing is a candidate for the same failure.

### Shape of the fix

- **Bootstrap step 3 must run the interpreter, not stat it.** Replace the `Test-Path` with an
  actual `& ".venv\Scripts\python.exe" -c "import sys"` and treat a non-zero exit (or no
  output) as "broken", exactly as `vidstab_health` treats non-determinism.
- **Repair cheaply first, recreate only if that fails.** If a compatible Python 3.12 is
  present (`Find-Python312` already answers this) and `pyvenv.cfg`'s `home =` points
  somewhere else, rewriting that one line is enough, because the installed packages were
  built against the same ABI. Re-verify by running the interpreter again. Only if that still
  fails should `.venv` be deleted and recreated, and the user should be **told what that
  costs** (a Local/Both install re-downloads PyTorch CUDA and the rest, which is the long
  part of a first run). Silently spending an hour of downloads is not an improvement on
  silently doing nothing.
- **The launcher must not fail invisibly.** `Image Toolbox.cmd` should verify that the venv
  interpreter runs before starting `pythonw.exe`, and fall back to `bootstrap.ps1` (which now
  repairs) when it does not. This is the layer that turns the bug from "an error message" into
  "nothing happens", and it is three lines of batch.
- **Consider making `.setup_complete` a stamp rather than a marker**, recording the base
  Python path and version the venv was built against, the same way `ffmpeg/build.txt` records
  the ffmpeg build. Useful for diagnosis, but it is not the fix on its own: a stamp still
  cannot tell you the interpreter is broken, only that something changed. Run it.

### Manual workaround for an install that is already broken

Delete `.venv` **and** `.setup_complete` from the app folder, then launch normally. The
launcher will re-run the bootstrapper, which rebuilds the environment from scratch. On a
Local or Both install this re-downloads the whole GPU stack, so expect a long first run.
Nothing in `config.json`, `db\cache.db` or the logs is affected.

### Not yet known

- Whether a **same-path, same-version repair** (uninstall then reinstall 3.12.9 to the
  identical location) leaves the venv working. It probably does, which would mean the trigger
  is narrower than "Python was reinstalled", but it does not change the fix.
- Whether the installer's own upgrade path can land in this state on its own. There is no
  reason to think so: the installer never touches Python or `.venv`.

---

## D2: NVENC is chosen because the build lists it, not because the machine has it

**Found:** 2026-08-19, on a Remote-only 0.6.0 install on a VM with a virtual GPU. Video
Stabilization pass 2 died instantly with `ffmpeg failed (exit 4294967295)` and an empty stderr
tail. **Severity: high on any machine without an NVIDIA GPU**, which on a Remote-only install
is the normal case rather than an edge case.

### Root cause

`video_pipeline.pick_encoder` decides like this:

```powershell
encoders = ffmpeg -hide_banner -encoders
if ("hevc_nvenc" in encoders) { return "hevc_nvenc", ... }
```

`hevc_nvenc` is **compiled into every GPL ffmpeg build**, whether or not the machine has an
NVIDIA card, so the string is always there. The function's own docstring says it picks NVENC
"when the bundled ffmpeg exposes it **and a CUDA GPU is present**"; the second half was never
implemented. Docstring and code have disagreed since the function was written.

**Why it took until now to bite.** Every previous caller runs where a CUDA GPU is a
precondition: the local Video Upscaler path only runs on a card, and a remote run encodes on
the pod. **Video Stabilization is the app's first deliberately GPU-free feature**, so it is
the first code to call `pick_encoder` on a machine with no NVIDIA hardware. The feature is
not wrong to exist on such a machine: `vidstab` is CPU work and a Remote-only user can
legitimately want it. Only the encoder choice is wrong.

This is the same shape as D1 and as the ffmpeg pin before it: **listed is not available, and
present is not working.** Three for three now.

### Shape of the fix

- **Probe, do not parse.** Attempt a one-frame encode and cache the answer for the process:
  `ffmpeg -f lavfi -i nullsrc=s=64x64:d=1 -c:v hevc_nvenc -f null -`, non-zero exit means no
  NVENC. Roughly 200 ms once, and it is behavioural, exactly like `vidstab_health`. Checking
  `nvidia-smi` instead would answer a different question (a card exists) than the one that
  matters (this ffmpeg can drive it).
- **Fall through to `libx265`/`libx264`, which already exist** in the function. Nothing else
  changes: `_DELIVERY_PIX_FMT` already maps libx265 to `yuv420p10le`, so a CPU fallback still
  delivers 10-bit.
- Fix it **in `pick_encoder`**, not in `video_stabilize.py`. Every caller benefits, and a
  second copy of the probe would drift.

---

---

## D4: a local GPU below the 8 GB minimum is offered with no warning

**Found:** 2026-08-19, while looking into a user report that turned out to say nothing about
VRAM (see below). **Severity: low.** Not urgent, and there is a decision to make before coding
it.

### What was actually observed, and why it does not evidence this defect

The whole report: title **"not working"**, body **"the output folders seem empty"**, plus
`GPU: NVIDIA GeForce RTX 2060`. That is not enough to act on, and the VRAM reading of it is
weak twice over. The RTX 2060 also shipped in a **12 GB** version (2021), which is a fine
card, so the name does not even establish 6 GB. **And an empty output folder has at least
three explanations that are more likely than VRAM, two of which are the app working
correctly:**

| Explanation | How likely | What it means |
|---|---|---|
| Every image was **skipped as already near the target** | most likely | Working as designed. A folder of photos already at or above 4K produces no output at all, and the run summary says so ("N skipped") in a line the user evidently did not read as the answer |
| The tree had been **conciliated** | likely | Also by design: conciliation moves the processed files back into the original tree, so the output folder is legitimately empty afterwards. `BrowseUpscaledWindow` names this case in its own empty state precisely because it reads as a bug |
| The user looked in the **source** folder, or beside it, rather than in `__upscaled__` | plausible | A navigation problem, not a failure |
| Every image **failed**, e.g. OOM on a small card | possible | The only reading that supports this defect, and the report gives nothing that distinguishes it from the three above |

So: **this defect stands on its own merits, not on that report.** The gap below is real and
worth closing whether or not the user ever had anything to do with VRAM. Recorded here because
it is what prompted the look, not because it is evidence.

**What the report actually argues for** is different and cheaper: an empty output folder is
the app's most confusable success state, and "the output folders seem empty" is what a user
says when a completed, correct run looked like nothing happened. The end-of-run summary
already carries the answer ("0 upscaled, 240 skipped"); it is evidently not landing. Making a
zero-output run state its reason **in the place the user is looking** would close more real
confusion than any VRAM gate, and it applies to every card.

### The gap

The remote GPU picker gates by VRAM and always has: it offers only cards clearing the task's
floor (32 GB upscale, 16 GB tag), so a user renting a pod cannot pick something that will not
work. **The local picker applies no floor at all.** `ToolTab._populate_local_gpus` lists every
card `system_telemetry.list_gpus` returns, formatted `"<name>, <N> GB"`, and `N` is right
there in the dict already. So the app is careful about a card the user pays for by the hour
and silent about the one they own.

Two halves of the original suggestion are worth separating:

- **"not NVIDIA" needs nothing.** `list_gpus` shells out to `nvidia-smi`, so a non-NVIDIA
  machine already lists nothing and the picker says "no NVIDIA GPU detected".
- **"under 8 GB" is the real gap**, and the data to close it is already in hand.

### The decision to make first: warn, or forbid?

Greying the card out conflicts with a stance the app already took deliberately. The
first-start wizard recommends a model tier by VRAM but keeps **every** option selectable,
because SeedVR2 offloads: a small card is *slower*, not incapable, and the wizard's own
comment says the recommendation is "a suggestion, not a gate". Hard-greying the only GPU a
user owns, on a machine where the work would in fact complete, would be the app refusing to
try.

**Recommendation: mark and warn, do not forbid.** Label the entry with the reason
(`"RTX 2060, 6 GB - below the 8 GB minimum"`), and warn once on Start with what to expect
(slow, and the Video Upscaler's larger targets may not fit at all) plus the two ways out:
Remote, or a smaller Resolution Target. Reserve an actual gate for combinations that are
certain to fail rather than merely slow, which is a per-target question the video path already
knows how to ask (`video_estimate` drops cards below a target's floor).

### The other half of this, now its own milestone

The report was terse because nothing invited detail, and the app knew the answer at the time
and threw it away. That is **`future-features.md` #24** (make a bug report actionable without
asking): auto-fill the last run's summary, the VRAM total rather than just the card name, the
install mode and the ffmpeg build stamp. Item 2 there is what would settle THIS defect from a
report, without a round trip.

## Fixed

Kept because the code comments and tests reference these ids.

### D3 (fixed): a finished-but-failed Stabilization run looked like a hung one

**Found:** 2026-08-19, on the same VM run as D2. Reported as "the process is now stuck".
**It was not stuck.** The run had ended, correctly, in well under a second. Three separate
things conspired to make a finished run look like a hung one, and the third is the one that
mattered:

1. **The progress bar kept its last value.** `on_exit` set it to 100% only on a clean
   success, so a single video that died in pass 2 left it frozen at **50%** (one of two
   passes). A bar stopped half way is the most direct way an app can claim to still be
   working.
2. **Stop was greyed** because the run was over. Correct on its own.
3. **Start was greyed too**, because Start acted on `Queued` rows only and the one row was
   now `Failed`. So the failed video **could not be retried at all**, short of removing the
   row and adding it back, and the two greyed buttons together read as a frozen tab.

The runner was never implicated: forcing `pick_encoder` to return an encoder that cannot run
reproduced the failure exactly and `run_queue` returned a correct summary in 0.5 s.

**Fixed** by making `Failed` a runnable state (`ST_RUNNABLE`, since a stabilise fails for
environmental reasons the user can go and fix), clearing last run's verdict from a row when it
is queued again, and hiding the progress bar on any ending that is not a clean success. Three
regression tests drive the exact VM sequence.

**And a fourth thing, which is why it took a screenshot to work this out:** the log could not
distinguish "finished" from "died", because `_report_completion` wrote nothing to it. That is
fixed too, with one `Run ended: ...` line written before the GUI event and before any
notification.
