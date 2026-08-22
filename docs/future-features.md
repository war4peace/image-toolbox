# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

One entry is listed first despite being **built**, because it is the only one with **dates**
on it (#25, the RunPod API v2 migration: the code shipped in 0.6.1, but the old transports must
be deleted after **2026-11-15** and in **early 2027**, and nothing else in the project will
remember that). A second is listed as built for a different reason: #24 (making a bug report
actionable without asking the user anything) shipped in 0.6.1, and is kept here rather than
folded into the legend because its redaction rules are the kind that get "simplified" later,
and the record of what each one cost is the argument against that. The rest are unbuilt: one
measurement-gated processing capability (#21 denoising, gated on a measurement that has not
been run), a Video Upscaler feature (#12 mixed local+remote queue) and a remote-side one
blocked on funds rather than design (#15 a second GPU provider). Two lower-priority ones each
introduce a new process model, networking, or packaging (HTTP interface #3, Unraid #4). The
**shipped** milestones are kept below as a numbering legend, after the open work.

---

## Contents

- [25. RunPod API v2 migration](#25-runpod-api-v2-migration-built-two-deletions-still-dated) (built; two dated deletions left)
- [24. Make a bug report actionable without asking](#24-make-a-bug-report-actionable-without-asking-built-061) (built)
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

## 24. Make a bug report actionable without asking: BUILT (0.6.1)

There is already an in-app **Report an issue** path (`gui.common._issue_url`, the link at the
bottom of the main window plus a second entry point in the Benchmark window) that opens a
pre-filled GitHub new-issue page. It fills the app version, the OS, the Python version, the GPU
**name**, and a "please attach" line pointing at the newest crash log. This milestone is about
everything it throws away.

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
| 6 | **The run logs**, redacted, as an attached file rather than "please attach" | Users do not attach files, and a URL cannot carry a log anyway. See the delivery shape below |

### The shape: a redacted zip the user drags in

The URL is capped at roughly 8 KB (`_MAX_ISSUE_URL = 7800` already encodes this for benchmark
contributions), which is enough for items 1 to 5 and nothing else. Rather than shrink the
payload to fit the transport, the transport changes:

1. One collector builds **both** a body (items 1 to 5, under the cap) **and** a diagnostics
   zip written to `./issues` in the app folder. `{localappdata}\Programs\Image Toolbox` is
   writable non-elevated (`PrivilegesRequired=lowest`), same as `logs/` and `db/` already are.
2. A **review dialog** opens first, with nothing else on screen yet.
3. Its one button opens the browser at the pre-filled `?body=` URL, then selects the zip in
   Explorer (`explorer /select,`, already implemented in `gui/filmstrip.py` and worth lifting
   into `gui/common.py` rather than copying a second time).
4. The body's first line names the zip and asks the user to drag it into the box. That
   instruction belongs **in the issue body**, where the user is already looking, not only in a
   popup they will have dismissed.

The same generator with a different sink is the **"Copy diagnostics"** button: the body block
to the clipboard, no cap, for a forum post, a chat or an email that never becomes a GitHub
issue. And `report.md` goes **inside** the zip, byte-identical to the issue body, so a browser
flow that fails (not logged in, body lost through the login redirect) still leaves the whole
report on disk.

### Redaction, and the measurement that decides it

Sampling a real upscale log (`logs/log_40d8b4704174.log`, 7 MB, 29,795 files) settles what the
zip may contain:

```
[1/29795] SKIP (unreadable image)  X:\Personale\Poze\04-01-2004\IMG_0001_upscaled.JPG
[2/29795] SKIP (unreadable image)  X:\Personale\Poze\James (cats mainly)\dsc01308.jpg
[3/29795] SKIP (unreadable image)  X:\Personale\Poze\Oracle\Irinel  Poze Cairo\Cairo5\Picture 209.jpg
```

Three lines, and they carry a person's name, a location and a private folder taxonomy. The
paths are not incidental to the log, they **are** the log, one per file, tens of thousands of
times. And after Tag & Rename has run, the filenames are AI-written descriptions of private
photos, so a tagged tree's log reads as a caption list of someone's family album.

**A regex scrub cannot fix this.** `James (cats mainly)` has spaces and `Irinel  Poze Cairo`
has a double space, so there is no reliable way to find where a Windows path ends inside
free-form text: any pattern either stops early and leaks the tail, or swallows the rest of the
line. This is exactly why redaction must be a rule about what is **collected**, and a zip is
where that rule is most likely to be broken, because a zip makes it cheap to just throw the
files in.

Four rules, all unconditional:

- **Allowlist the sources.** Structurally excluded, not filtered out afterwards:
  `config.local.json` (`config_store.SECRET_FIELDS`: API key, MQTT password, notification
  tokens and webhook URLs, where a webhook id IS the credential), `db/cache.db` and its `.bak`
  siblings (20 MB, and a complete index of the private tree), and the raw output of
  `nvidia-smi -q` (serial number and GPU UUID; allowlist the fields instead of dumping it).
- **Tokenise the known roots, hash everything after them.** The app knows its own roots (every
  `defaults.*` folder, `APP_ROOT`, `%USERPROFILE%`), so match longest-first and substitute,
  then replace each remaining path component with a short stable hash, extension preserved.
  Line 1 above becomes `<SRC1>\a3f1\7c2e.JPG`. Counts, sequence, depth, extensions and
  repeat-offender correlation all survive, which is what a diagnosis actually uses; content
  leaks nothing.
- **Fail closed on the remainder.** After substitution, any line still matching a drive-letter
  or UNC prefix means an unrecognised root, so the **whole line** is replaced with
  `[redacted: unrecognised path]`. Losing a line costs a little diagnostic value; keeping it
  costs the promise.
- **No opt-out.** An "include real folder and file names" checkbox was considered and
  **rejected**: it trades a permanent public leak for a marginal debugging convenience, most
  users will not read it carefully, and knowing the exact filename is almost never what
  settles a report. Dropping it also means the collector has one code path, no mode to test,
  and no ambiguity about what a given report actually contains.

Two invariants follow. **`./issues` holds redacted zips and nothing else, ever**, because it is
the folder the app points Explorer at and anything un-redacted sitting beside the zip is a
drag-and-drop accident waiting to happen. And since the hash is one-way for the **user** too,
generate a hash-to-path **mapping file** so "what is `7c2e.JPG`?" stays answerable; it is
written to `logs/`, never to `./issues` and never into the zip. Prune `./issues` to the newest
N zips, reusing the newest-10 idiom `conc_runs` already uses, or the folder grows forever and
nobody ever looks in it.

### The review dialog

Opening Explorer with the file selected is not inspection: nobody unzips twelve files to audit
them. So the dialog does the work: the entries with their sizes, one line stating what was
redacted, and the buttons **Open folder** / **Open report.md** / **Report without attaching**.

It also carries one disclosure, in one line: attaching to a public issue makes the zip
**publicly downloadable and permanent from the moment it uploads**, even if the issue is never
submitted. That is the fact the whole redaction design exists to make safe.

### The GitHub template question, and the trap in it

A GitHub **issue template** would improve the human side, and the repo has none today
(`.github/` holds only `workflows/`). Three things were verified before deciding:

1. **Issue forms (YAML) and `?body=` do not compose.** A form is pre-filled per field, as
   `?template=bug.yml&<field-id>=value`. A plain `?body=` has no field to land in.
2. **A URL query is a documented way to bypass the template chooser**, so `?body=` keeps
   opening a blank editor once templates exist. Note that GitHub's maintainers treat that as a
   defect to be closed, not as a contract.
3. `blank_issues_enabled: false` only hides the Blank option from the chooser for non-write
   users. It is not enforcement.

**The decision is a markdown template, with the app staying on `?body=` and no `template=`
parameter**, and the reason is an asymmetry: **installs are immutable, the repo is not.** An
install shipped today keeps sending whatever URL it was compiled with, forever. Point that URL
at `?template=bug.yml&diagnostics=...` and it now depends on a filename and a field id living
in a repo that will be edited by someone who has forgotten the coupling; rename either and
GitHub silently ignores the unknown parameter, so every install older than the change starts
sending empty reports with nothing failing loudly. That is the same shape as D1, the ffmpeg pin
and NVENC: **present is not working**, and the failure is invisible from the side that can
still act. A `?body=` URL references nothing in the repo and cannot go stale.

The counter-argument is real and worth recording: a form with `validations: required` on "What
happened?" is the only mechanism that stops a two-word report at the source, and a named
`diagnostics` textarea would make the block greppable rather than prose. It is refused because
this milestone's premise is that the two-word report is normal and the **automated** half is
the lever; a required field mostly converts "not working" into "not working." typed into a box.

So the template is **not a prerequisite**. What the template question gated was the output
shape (one markdown string, or N named parameters), and that is now answered. The file itself
is a short job that can land at any point: `.github/ISSUE_TEMPLATE/bug.md` with headings
mirroring the app's body verbatim, plus `config.yml` with `blank_issues_enabled: true`. The two
halves are then coupled by convention only, so drift costs nothing. Add `labels=bug` to
`_issue_url` at the same time, which buys the triage half of a template's value today with no
file and no coupling, exactly as `_benchmark_issue_url` already does.

### Where the last-run summary comes from: the newest log's tail, unparsed

**Decided: scrape the log files, take the tail verbatim, store nothing new.** The alternative
was persisting a small ring of the `last_run` dicts already published to MQTT and then dropped.
Two samples settled it.

The trigger report ("the output folders seem empty") is answered **by the last eight lines of
an upscale log, with no parsing at all**:

```
Found 0 eligible file(s) (0 already done, 0 too large, 2 left as-is - 2/2 from cache).
  Left as they are - upscaling would discard part of these images (2):
    <SRC1>\7c2e.png  (would lose transparency)
    <SRC1>\a3f1.tif  (would lose 16-bit depth)
Nothing to process.
```

And Tag & Rename already ends its log with a formatted Folder / Processed / Rotated / Skipped /
Failed / Elapsed table. The information is not missing; it is simply never leaving the machine.

**No parser, deliberately.** There are five log prefixes (`log_` upscale, `tag_`, `video_`,
`conc_`, `stab_`), each ending in a different shape, and none of those shapes is a contract:
they are human-readable run output that gets reworded whenever a runner is touched. A parser
over them breaks **silently**, which is the one failure mode this feature cannot afford, since
its whole purpose is to arrive when nobody is watching. A tail cannot break. Three further
properties come free: it is **retroactive** (it reads runs that happened long before the
feature shipped), it needs **no `db.py` change and no new schema**, and it is the only approach
that works for the case that matters most, a run that **crashed** and therefore has no summary
block at all, where the tail is precisely the interesting part.

Mechanics:

- **Select by mtime per prefix.** Logs are per source folder (`<prefix>_<hash>.log`), so the
  newest file matching each prefix is that tool's most recent run.
- **Budget.** The most recently modified log across all five prefixes gets a real tail (on the
  order of 25 lines) in the URL body, since that is almost always the run being reported; the
  other four get one line each (tool, when, size). Full tails go in the zip, where the 8 KB cap
  does not apply.
- **Collapse the tail** through `gui.widgets.COLLAPSE_PROCESSING_RE`, the same pattern the log
  window's "Collapse repeating progress lines" toggle uses. Without it a video run's last 25
  lines are 25 identical per-minute heartbeats. With it, the tail is literally what the user
  saw on screen, which is the point.
- **The redactor runs on the body too, not only on the zip.** Those tails are made of paths.

One redactor requirement was discovered from the samples and is easy to miss: real logs contain
the **8.3 short form** (`C:\Users\EDUARD~1\AppData\Local\Temp\...`), so a root table built from
`%USERPROFILE%` in its long form alone will not match it, and the fail-closed rule would then
drop those lines wholesale. Register both spellings of every root.

### As built (0.6.1): what changed from the plan

Shipped as `scripts/diagnostics.py` (redactor + collector, stdlib-only and torch-free),
`gui.dialogs.DiagnosticsDialog` (the review step), `gui.common.open_in_explorer` +
the `body=` path through `_issue_url`, and `.github/ISSUE_TEMPLATE/{bug.md,config.yml}`.
`tests/test_diagnostics.py` pins the rules. Four things came out of building it that
the plan above did not know.

**1. Loosening the fail-closed rule turned drops into a leak, on real data.** The
first cut asked only "is anything path-shaped left in this line". Hashing ONE
component was enough to satisfy it, so this real line

```
[2/2] Poze (Fototarget)\2005-10-24\098.avi -> 2X: 160x120 327f
```

was emitted with the private folder intact: the date matched the name dictionary and
was hashed, while `Poze (Fototarget)`, separated from `[2/2]` by a single space, was
never isolated as a segment. The rule is now per-separator and asymmetric: whatever
sits immediately either side of a backslash must be a redacted span or whitespace, so
EVERY component of a joined token must resolve or the line goes. The lesson
generalises and is worth keeping: **a loosened fail-closed rule does not fail loudly,
it converts drops into leaks**, and only an adversarial pass over real logs finds it.

**2. The vision model's DESCRIPTIONS were still in the zip, and a path rule can never
reach them.** Found by the author reading an actual generated zip, after everything
above already passed. Tag & Rename logs the model's own sentence about each picture,
and the file name it condenses out of it:

```
           -> 0001_Kitten_Walking_Snowy_Surface.png  (renamed)
           -> "A kitten with striking blue eyes and a fluffy coat is walking on a snowy surface..."
```

This is the **worst** disclosure in the whole feature and the redactor was blind to it
by construction: it is not a path, not a folder name, not in any dictionary, just free
English prose. And it is qualitatively different from a leaked folder name, because a
collection's worth of these lines is a description of somebody's family, which for an
app whose purpose is reviving personal photo collections is the entire point of the
data. So the rule is **removal, not redaction**, and it is a rule about the line's
SHAPE, because prose cannot be pattern-matched: measured across every log this install
holds, 6,541 lines start with an arrow, **all** of them are Tag & Rename output, and
every one carries a description or a generated name. No other runner uses the shape
(the Video Upscaler's `a.avi -> b.mp4` sits mid-line).

**The line goes entirely, placeholder included** (`Redactor.line` returns the `OMIT`
sentinel and `lines()` filters it). Two intermediate cuts kept a
`-> [description withheld]` marker, the second of them also re-attaching the outcome
`(renamed)` through an allowlist of three exact literals. Both were wrong for the same
reason, and it is a reason worth carrying: **a line that cannot say anything is not
worth the line.** The per-image outcomes are already totalled in the run's own summary
table a few lines below, so nothing is actually lost; what the placeholder added was
one line of noise per image, thousands of them, in a file whose entire purpose is to
be read by somebody debugging. The counter (`Redactor.withheld`) is what tells the
user it happened, and it is reported in the dialog and in the body.

Two follow-ons, both found only by auditing the zip against **the descriptions' own
harvested vocabulary** rather than a guessed word list. The Undo section prints the
current name as a **bare line with no path around it**, so removing the arrow lines and
leaving those protects nothing: after a rename the name IS the description. A bare
media file name is now hashed exactly as it would be inside a path (media extensions
only, so `cache.db` and `video_benchmark.log` stay readable). And the counters were
read off the redactor **before** the zip's logs went through it, so a real report
announced "0 lines withheld" while its tag log had 5,716 removed; the excerpt in the
URL body is now a SLICE of the already-redacted zip text rather than a second pass,
which fixes both the count and any chance of the two disagreeing.

A third follow-on came from asking what the surviving line is actually FOR. A tag
log's per-image line reads `[21/100] 1280x960px  <path>`, and the counter and the
dimensions are its entire diagnostic content: the run is a sequence, the outcomes are
in the summary table, and the file's identity adds nothing the counter does not. So in
Tag & Rename logs a path is **removed, not hashed** (`STRICT_PATH_TOOLS`), keeping the
root TOKEN only where the path is exactly a root, because "was it pointed at the source
folder instead of the upscaled one?" is a documented mistake and the legend answers it
without naming anything. This is per-TOOL policy, not per-line parsing, so a change to
the log's wording cannot quietly reintroduce the path. Collecting less is the only
protection that a later bug in a redaction rule cannot undo. With the arrow lines gone,
the Undo listing's bare name lines go too: a column of anonymous hashes answers
nothing. Two post-conditions came out of reading the result. Removing a path must never
leave a dangling `Cache:`, because an empty-looking field reads as "the app recorded
nothing here", the same misreading that made a silent "0 lines withheld" worse than
useless. And **a quoted path ends at its closing quote**: the runners print folders as
`Scanning 'D:\...\Benchmark' ...`, where the end-of-line rule ate the quote and the
ellipsis and left `Scanning '`, which reads as a truncated log. That end is knowable
exactly rather than guessed, so it is the one case that does not need the end-of-line
rule at all. Measurement (1) never saw it, because it counted trailing text after TWO
spaces and here a single space precedes the ellipsis.

The generalisation is the one worth keeping: **an app that generates text about the
user's data has a disclosure channel that no structural rule will find.** Auditing has
to be done against real output, with the vocabulary harvested from that output.

**3. `db/cache.db` is the redaction dictionary.** The one file that must never be
attached is also the best source of redaction knowledge, and the inversion is the
happiest part of the design: **the file we refuse to ship is what makes the logs safe
to ship.** It supplies both halves that `config.defaults` could not. Folders the app
worked on BEFORE: logs are per-folder and long-lived, and one older log was dropping
**78.5%** of its lines purely because its source is no longer a configured default
(with the recorded roots added: 0.0%). And the NAME dictionary: walking the source
tree for folder names took **145 seconds** over the SMB mount holding the photos,
against 0.16 s for the same 34,991 names from a local SQL scan. It is opened
read-only through a URI, so a diagnostics run can never migrate, lock or write the
cache.

**4. Two patterns were too greedy, and both cost diagnostic value rather than
privacy.** `http://localhost:11434` matched the drive-letter rule as drive `p:`, so
every line mentioning the Ollama URL was dropped and the setting itself came out as
`<unrecognised path>`; a drive letter is ONE letter, so the pattern now has a
lookbehind. And "a config value containing a slash or a colon is a path" mangled
`tagging.camera_filename_patterns` (regexes, full of backslash escapes) along with
that URL; the test is now what a path actually STARTS with. Between them the drop
rate fell from 0.29% to 0.21%.

**5. The review dialog needed a second pass after looking at it.** The redaction
summary and the pointer to the private map were inside the scrolling file list, below
the fold, behind a scrollbar nobody drags. They are the two things the user most needs
to read, so they are fixed labels now and the box holds nothing but the file table.

**Measured, on this developer's own logs:** ~201,000 real lines audited twice, first
against 15 private terms drawn from the actual tree and then against a content-word
list harvested from the model's own descriptions. **Zero leaks** of either kind: every
distinctive word (village, hillside, donkey, romanian, castle) is gone, and what the
audit still flags is ordinary log English ("already tagged", "Force rename mode") or a
substring accident (`river` inside `driver`). 0.23% of lines dropped, 4.15% removed as
model output, ~41,000 lines/s, a 0.5 s build. A report comes out as a ~4,200-character
pre-filled URL (cap 7,800) plus a ~40 KB zip.

Two rules to keep. The URL carries **no `template=` parameter** and must not grow one
(`test_the_issue_url_carries_no_template_parameter` says why in full). And **`./issues`
holds redacted zips and nothing else, ever**, which is what makes dragging from it
safe; the hash-to-name map is the private half and goes to `logs/`, where it is pruned
on a longer schedule than the zips because it stays useful for as long as the ISSUE is
open.

### Constraints that shape it

- **Nothing may be sent anywhere.** This is a pre-filled browser form the user reads and can
  edit before submitting, and a file the user drags in themselves. That property must not be
  traded away for convenience, and it is what makes the enrichment safe in the first place.
- **The zip must stay small.** GitHub allows 25 MB for a `.zip`, so cap at roughly 10 MB for
  headroom. Redacted text compresses hard, so that is a great deal of log.
- **Verified live, 2026-08-22**, from a real install with `.github/ISSUE_TEMPLATE/bug.md`
  already in the repo: the app's link opens the **blank** editor with the body intact,
  down to the line naming the zip, rather than landing at `/issues/new/choose`. That is
  the half that could have failed, and it is the empirical half of decision 2 above:
  bypassing the chooser via a URL query is documented behaviour that GitHub's own
  maintainers treat as a defect to be closed, not as a contract. **So it is worth
  re-checking whenever the template set changes**, and the failure would be loud rather
  than silent (the user lands on a chooser page with an empty form), which is the one
  saving grace. The drag itself is GitHub's ordinary `.zip` attach, well inside its
  25 MB ceiling.

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
- **Open milestones: #21, #12, #15, #3, #4 — plus #25 and #24, both BUILT.** #25 is open only
  for its two dated deletions; #24 has nothing outstanding at all (its one live check passed on
  2026-08-22) and is kept only for its redaction record, which is the part worth re-reading
  before anything else in this app starts collecting user data.
- **#25 (RunPod API v2) was the only milestone with a deadline, and it is done** — all five
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
