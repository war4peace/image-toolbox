# Known defects

Confirmed bugs, each with its root cause and what was done about it. Open ones come first;
**Fixed** entries are kept because code comments cite them by id and because the diagnoses are
worth more than the patches. Design questions live in `docs/future-features.md`; ideas that
were rejected live in `docs/dropped-ideas.md`.

> **Nothing is open as of 2026-08-22.** D1 to D4 were all found while testing 0.6.0, D5 and
> D6 while testing 0.6.1; all six were fixed, D6 after v0.6.1 had already shipped. Three of
> the first four are the same mistake in different clothes, which is the reason to read them
> together: **present is not working.** An ffmpeg binary that is there and corrupts memory,
> an encoder the build lists that the hardware cannot run, a `python.exe` that exists and
> cannot start. Every one of them was a check asking whether something EXISTED when the
> question was whether it WORKED, and every one was invisible to the test suite because a
> healthy developer machine answers both the same way.
>
> **D5 and D6 are the GUI pair, and they rhyme**: a button that exists, draws and enables,
> wired to nothing; and a command that launches, succeeds, and opens the wrong window. Both
> are silent by construction, and the reason both reached a user is that **the test suite
> could see the code but not the effect.** Where a defect's only symptom is what appears on
> screen, the check has to reach for the screen: D5 is guarded by a structural scan, D6 by
> reading the opened window back through Shell.Application.

---

## Contents

- [D6 (fixed): "show me the file" opened Documents, for every install](#d6-fixed-show-me-the-file-opened-documents-for-every-install)
- [D5 (fixed): a button that drew, enabled and did nothing](#d5-fixed-a-button-that-drew-enabled-and-did-nothing)
- [D4 (fixed): a local GPU below the 8 GB minimum was offered with no warning](#d4-fixed-a-local-gpu-below-the-8-gb-minimum-was-offered-with-no-warning)
- [D1 (fixed): the app would not start after the system Python was uninstalled or reinstalled](#d1-fixed-the-app-would-not-start-after-the-system-python-was-uninstalled-or-reinstalled)
- [D2 (fixed): NVENC was chosen because the build lists it, not because the machine has it](#d2-fixed-nvenc-was-chosen-because-the-build-lists-it-not-because-the-machine-has-it)
- [D3 (fixed): a finished-but-failed Stabilization run looked like a hung one](#d3-fixed-a-finished-but-failed-stabilization-run-looked-like-a-hung-one)

---

## Fixed

Kept because the code comments and tests reference these ids.

### D6 (fixed): "show me the file" opened Documents, for every install

**Found:** 2026-08-22, by the user, within minutes of installing the released v0.6.1.
**Reported precisely, including the cause:** "Report with this file" opened Explorer in
**Documents** rather than at the diagnostics zip, and *"I suspect this is caused by Image
Toolbox being installed in `C:\Image Toolbox` which contains a space which is not properly
escaped."* That was exactly right.

The command was built as a list:

```python
subprocess.Popen(["explorer", "/select,%s" % norm], ...)
```

Python's `list2cmdline` quotes any token containing a space, so the switch and the path went
out as **one quoted token**:

```
explorer "/select,C:\Image Toolbox\issues\imgtbx-diag-20260822-203537.zip"
```

Explorer does not recognise a switch that arrives inside the quotes. Its response to a single
argument it cannot parse is to **open the user's Documents folder**: no error, no diagnostic,
and an exit code nobody reads (Explorer returns 1 on success anyway). The app did exactly what
it was told and told the user, wordlessly, to look in the wrong place.

**This was not an edge case: it was every install.** `DefaultDirName` is
`{localappdata}\Programs\Image Toolbox`, so the space is in the application's own name and no
install path can avoid it.

#### Why it had been latent for a year

The same spelling had been in `FilmStrip._open_folder` since 0.3.0, feeding the film strip's
*Open image folder* / *Open upscaled image folder* entries, and it was broken there the whole
time for any photo path containing a space. Nobody noticed because the developer's own library
lives at `X:\Personale\Poze`, where no component has one. **The diagnostics zip is the first
file the app ever asked Explorer to reveal underneath its own install directory**, which is
the one path that is guaranteed to contain a space. The feature did not introduce the bug; it
was the first thing to stand where the bug could be seen.

#### Four call sites, three spellings, two of them wrong

The fix is not the interesting part. What matters is that the same gesture had been written
four times, and the three variants do not behave alike. Measured on Windows 11 by launching
each form and reading the resulting window back through `Shell.Application`, checking the
folder **and** the selection, because selecting the file is the entire purpose of `/select,`:

| Form | Where it was | Folder | Selection |
|---|---|---|---|
| `["explorer", "/select,PATH"]` | `gui/common.py`, `gui/filmstrip.py` | **Documents** | none |
| `["explorer", "/select,", "PATH"]` | `gui/tab_video.py`, `gui/tab_stabilize.py` | correct | **none, if the file name contains a comma** |
| `'explorer /select,"PATH"'` | (nowhere) | correct | correct |

The middle row is the trap that would have caught a partial fix. Splitting the token repairs
the space, and it looks right, and it is right until a file name contains a comma, because
`/select,` is comma-delimited and the path is no longer quoted as a unit. `Poze, Vacanta
2019\foto, 01.jpg` is not a contrived name for this application's users.

**Fixed** by building one raw command string in `gui.common.open_in_explorer` and routing all
four call sites through it. A raw string is safe here: `Popen` without `shell=True` hands it
straight to `CreateProcess`, so nothing reinterprets `&` or `^`, and a double quote cannot
occur in a Windows path.

`tests/test_open_in_explorer.py` pins the surviving form against both the space and the comma,
and pins that **no other module builds an explorer command at all** - which is the guard that
actually matters here, since the defect's real cause was four implementations rather than any
one of them being wrong.

### D5 (fixed): a button that drew, enabled and did nothing

**Found:** 2026-08-22, by the user, on the first hands-on run of the new "Report an issue"
review dialog (feature #24). **"Report with this file" did nothing when clicked. "Report
without it", the button next to it, worked.**

The cause is one name used twice. `DiagnosticsDialog.__init__` sets `self._report = None` (the
gathered report, filled in later by the background thread), and the handler for the button was
also called `_report`. `_build` runs immediately after, so `command=self._report` read the
attribute, not the method, and bound the button to **`None`**.

**Nothing anywhere says so.** Tkinter accepts `command=None` without a warning: the button
draws normally, `state="normal"` enables it normally, pressing it animates normally, and no
callback runs. There is no traceback, no log line and no visual difference from a working
button, which is why the working button beside it is what made the report possible at all.

Two things made it survive to a user. The obvious one is that the collision is invisible in
review: the assignment is in `__init__` and the `def` is two hundred lines below it, and each
reads correctly on its own. The subtler one is that this is **not** the ordinary Python
mistake, which is loud: shadowing a method usually blows up with `TypeError: 'NoneType' object
is not callable` at the first call. Here the attribute is read at BIND time, long before any
call, and tkinter's tolerance of `None` converts the crash into silence.

**Fixed** by renaming the handler to `_report_with_file`. The guard is
`tests/test_gui_command_bindings.py`, which is structural rather than behavioural, because a
headless test cannot press a button and a test that could press it would still see nothing
happen:

- no instance attribute in a GUI class may share a name with a method of that class;
- every `command=self.x` must name a method that actually exists, resolving the base chain
  through this repo's own classes and then through tkinter's (the first cut skipped any class
  with an unresolved base, which excluded `DiagnosticsDialog` itself, since it derives from
  `tk.Toplevel`: the one class the test existed for);
- and one pin on the specific names, since renaming the method back is precisely the change
  that reintroduces the defect.

The scan is clean across the whole `gui/` package, so this was the only instance.

### D4 (fixed): a local GPU below the 8 GB minimum was offered with no warning

**Found:** 2026-08-19, while looking into a user report that turned out to say nothing about
VRAM (see below). **Severity: low.** Not urgent, and there is a decision to make before coding
it.

#### What was actually observed, and why it does not evidence this defect

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

#### The gap

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

#### The decision, taken 2026-08-19: WARN

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

#### The other half of this, now its own milestone

The report was terse because nothing invited detail, and the app knew the answer at the time
and threw it away. That is **`future-features.md` #24** (make a bug report actionable without
asking): auto-fill the last run's summary, the VRAM total rather than just the card name, the
install mode and the ffmpeg build stamp. Item 2 there is what would settle THIS defect from a
report, without a round trip.

#### As fixed

**Warn, and only warn**, decided on three grounds worth keeping: 0.6.0 already carries a
great deal of change and this is not the place to add risk; a 6 GB card genuinely works for
some jobs (Tag & Rename with the smallest vision model is the clear one), and blanket-refusing
would deny a user a feature that would have run on their machine; and until reports actually
accumulate - and #24 makes them worth reading - gating potential users buys nothing.

- `gui.common.small_gpu_note(memory_gb)` is the whole rule, pure and tested: a short
  "below the 8 GB minimum" or None. `LOCAL_VRAM_MIN_GB` is the one place the number lives,
  and a test pins that the message quotes it, so the two cannot drift.
- **Both local pickers label the card** (`ToolTab._populate_local_gpus` and the Video
  Upscaler's synthetic local choice): `NVIDIA GeForce RTX 2060, 6 GB (below the 8 GB
  minimum)`. Passive, always visible, no friction.
- **One dialog before the first local run**, `ToolTab.confirm_small_gpu`, shown **once per
  session across every tab** rather than per run: the card does not change between runs, and
  a dialog on every Start trains the user to click through it, which is how a warning stops
  being one. It says what to expect (slow rather than failed, offloading to system memory),
  where it is most likely to break (large images, the higher targets), and the three ways out
  (smaller model, lower target, or rent a card).
- **An unknown size is never labelled.** `nvidia-smi` can answer `[N/A]` per field, and a card
  we know nothing about must not be described as if we had measured it.
- The **Video Upscaler gets the label but no dialog**: it already refuses a card whose every
  target exceeds its VRAM, which is a stronger per-target check, and the label is what
  explains a short target list.

The counterfactual is still worth remembering: the report that prompted this said nothing
about VRAM, and an RTX 2060 might have had 12 GB. This closes a real asymmetry, not that
report.


### D1 (fixed): the app would not start after the system Python was uninstalled or reinstalled

**Found:** 2026-08-19, on a VM running 0.5.5. **Fixed the same day, before 0.6.0**, after Python was uninstalled and reinstalled for
an unrelated tool. **Severity: high.** The app does not start, shows nothing at all, writes no
log, and the documented recovery path does not recover it.

#### What the user sees

Double-click, and **nothing happens**. No window, no error, no crash log. The app is simply
gone as far as the desktop is concerned.

#### Root cause: three layers, each of which alone would be survivable

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

#### Shape of the fix

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

#### Manual workaround for an install that is already broken

Delete `.venv` **and** `.setup_complete` from the app folder, then launch normally. The
launcher will re-run the bootstrapper, which rebuilds the environment from scratch. On a
Local or Both install this re-downloads the whole GPU stack, so expect a long first run.
Nothing in `config.json`, `db\cache.db` or the logs is affected.

#### Not yet known

- Whether a **same-path, same-version repair** (uninstall then reinstall 3.12.9 to the
  identical location) leaves the venv working. It probably does, which would mean the trigger
  is narrower than "Python was reinstalled", but it does not change the fix.
- Whether the installer's own upgrade path can land in this state on its own. There is no
  reason to think so: the installer never touches Python or `.venv`.

#### As fixed

Both halves, exactly as sketched above:

- **`Image Toolbox.cmd` runs the interpreter** (`python.exe -c "import sys"`, measured 36 ms)
  before starting `pythonw.exe`, and sends a failure to the bootstrapper instead of launching
  into nothing. This is the layer that turns "nothing happens" into a repair.
- **`bootstrap.ps1` step 3 asks `Test-VenvWorks`** instead of `Test-Path`, and when the venv
  is there but dead it tries `Repair-VenvHome` first: rewriting `home` and `executable` in
  `pyvenv.cfg` is the entire repair when a compatible Python is present again, because the
  installed packages were built against the same ABI. Only if that fails is `.venv` deleted
  and rebuilt, and the user is told what that costs before it happens.

Three details that are load-bearing rather than tidy:

1. **The ABI guard.** The repair is refused across a minor version. Repointing a 3.12 venv at
   a 3.13 would produce an environment that starts and then fails on every import, which is
   worse than the honest failure it replaced.
2. **No BOM.** `pyvenv.cfg` is parsed line by line by the stub, and Windows PowerShell's
   `Set-Content -Encoding utf8` writes a BOM that would ride on the first key, so the rewrite
   goes through `WriteAllLines` with an explicit no-BOM encoding.
3. **The venv stub fails cleanly** (exit 103, a printed message, no modal dialog), which is
   what makes probing it safe from a launcher that must not block. Verified.

Verified against a real broken venv: healthy -> break `home` -> detected as broken -> repaired
-> healthy again, with no BOM written and a cross-minor repair correctly refused.
`tests/test_venv_health.py` pins the invariants, the strongest being that neither half may go
back to deciding from a path existing.

### D2 (fixed): NVENC was chosen because the build lists it, not because the machine has it

**Found:** 2026-08-19, on a Remote-only 0.6.0 install on a VM with a virtual GPU. Video
Stabilization pass 2 died instantly with `ffmpeg failed (exit 4294967295)` and an empty stderr
tail. **Severity: high on any machine without an NVIDIA GPU**, which on a Remote-only install
is the normal case rather than an edge case.

#### Root cause

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

#### The fix

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

**Fixed** by `video_pipeline.nvenc_usable`: a one-frame encode to a null sink, cached per
codec per process, asked before NVENC is returned. One thing the fix had to learn the hard
way, and it is why a behavioural probe needs its own verification: **the probe frame must be
at least 256x256.** NVENC refuses smaller dimensions outright (`InitializeEncoder failed:
Frame dimensions are less than the minimum supported value`), so the first attempt, at 64x64,
reported NO NVENC on a machine with a working 3090. A probe that fails closed on good hardware
is worse than no probe: it would have quietly moved every user to the CPU encoder. Five tests
pin the behaviour, including the minimum frame size and the once-per-process caching, and the
fix was confirmed on the VM that found it: with no NVIDIA GPU present the run picks the CPU
encoder and completes.

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
