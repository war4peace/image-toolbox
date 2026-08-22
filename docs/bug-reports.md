# Bug reports

How **Report an issue** turns a two-word bug report into an actionable one, and what it
refuses to collect on the way. Design + as-built notes for roadmap **#24**, shipped in
**0.6.1**.

The premise is the whole design: **a user who writes two words is the normal case, not a
failure of the user.** The lever is the automated half. Everything the report carries
already existed somewhere on the machine; the app was simply throwing it away.

Related:

| For | Read |
|---|---|
| The module itself | [`scripts/diagnostics.py`](../scripts/diagnostics.py), [`tests/test_diagnostics.py`](../tests/test_diagnostics.py) |
| The two GUI defects this feature's own testing found | [`known-defects.md`](known-defects.md) D5 and D6 |
| Live telemetry and usage graphs (a different thing entirely) | [`telemetry-design.md`](telemetry-design.md) |

## Contents

- [The trigger](#the-trigger)
- [What the report carries](#what-the-report-carries)
- [The shape: a redacted zip the user drags in](#the-shape-a-redacted-zip-the-user-drags-in)
- [Redaction](#redaction)
- [What building it changed](#what-building-it-changed)
- [Where the last-run summary comes from](#where-the-last-run-summary-comes-from)
- [The GitHub template question](#the-github-template-question)
- [Measured](#measured)
- [Rules to keep](#rules-to-keep)

---

## The trigger

A real report, in full: title **"not working"**, body **"the output folders seem empty"**,
plus the auto-filled `GPU: NVIDIA GeForce RTX 2060`.

Nothing there is enough to answer, and yet **the app knew the answer at the time and threw
it away.** An empty output folder is almost always a completed run that skipped everything
as already near the target, or a tree that has been conciliated. Both are correct behaviour,
and the run summary said so on screen. The user is not going to write that down. The app can.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## What the report carries

In descending order of what it settles:

| # | Field | Settles |
|---|---|---|
| 1 | **The last run's summary per tool**: which tool, when, and how it ended | "The output folder is empty" in one line, without a round trip. The highest-value item here |
| 2 | **VRAM total, not just the GPU name** | Whether the card is under the 8 GB minimum. A card name does not imply its memory: the RTX 2060 shipped in 6 GB *and* 12 GB |
| 3 | **Install mode** (Local / Remote / Both) | Which half of the app is even in play. A Remote-only install has no local GPU stack at all |
| 4 | **The settings for the tool last used**: Resolution Target, skip-cutoff, model, "Run on" | The most common "not working" is a correct run that the settings explain |
| 5 | **The ffmpeg build stamp** and whether `.venv` looks healthy | [`known-defects.md`](known-defects.md) D1 and the vidstab pin: both invisible from outside, both already hit |
| 6 | **The run logs**, redacted, as an attached file | Users do not attach files, and a URL cannot carry a log anyway |

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## The shape: a redacted zip the user drags in

The URL is capped at roughly 8 KB, which fits items 1 to 5 and nothing else. Rather than
shrink the payload to fit the transport, the transport changes:

1. One collector builds **both** the body and a diagnostics zip in `./issues`.
2. A **review dialog** opens first, with nothing else on screen.
3. Its button opens the browser at the pre-filled `?body=` URL, then selects the zip in
   Explorer behind it.
4. The body's first line names the zip and asks for the drag. **That instruction belongs in
   the issue body**, where the user is already looking, not only in a popup they dismissed.

**Nothing is sent anywhere.** It is a form the user reads, edits and submits, and a file they
attach themselves. That property is what makes the enrichment safe and must not be traded for
convenience.

**Copy diagnostics** is the same generator with a different sink: the body to the clipboard,
no cap, for a forum post or an email that never becomes a GitHub issue. `report.md` also goes
**inside** the zip byte-identical to the body, so a browser flow that fails (not logged in,
body lost through the login redirect) still leaves the whole report on disk.

Opening Explorer with the file selected is **not** inspection: nobody unzips twelve files to
audit them. So the dialog does that work: the entries with sizes, what was removed and why,
and one line stating that attaching to a public issue makes the zip **publicly downloadable
and permanent from the moment it uploads**, even if the issue is never submitted. That fact
is what the whole redaction design exists to make safe.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## Redaction

Sampling one real upscale log (7 MB, 29,795 files) settled what a zip may contain:

```
[1/29795] SKIP (unreadable image)  X:\Personale\Poze\04-01-2004\IMG_0001_upscaled.JPG
[2/29795] SKIP (unreadable image)  X:\Personale\Poze\James (cats mainly)\dsc01308.jpg
[3/29795] SKIP (unreadable image)  X:\Personale\Poze\Oracle\Irinel  Poze Cairo\Cairo5\Picture 209.jpg
```

Three lines carrying a person's name, a location and a private folder taxonomy. The paths are
not incidental to the log, they **are** the log, one per file, tens of thousands of times.

**A regex scrub cannot fix this.** `James (cats mainly)` contains spaces and
`Irinel  Poze Cairo` contains a *double* space, so there is no reliable way to find where a
Windows path ends inside free text: any pattern either stops early and leaks the tail or
swallows the rest of the line. Redaction is therefore a rule about what is **collected**, and
a zip is where that rule is most likely to be broken, because a zip makes it cheap to throw
the files in.

Four rules, all unconditional:

- **Allowlist the sources.** Structurally excluded, never filtered afterwards:
  `config.local.json` (the API key, MQTT password, notification tokens and webhook URLs,
  where a webhook id *is* the credential), `db/cache.db` and its `.bak` siblings, and the raw
  output of `nvidia-smi -q` (serial number and GPU UUID: allowlist the fields instead).
- **Tokenise the known roots, hash everything after them.** The app knows its own roots, so
  match longest-first and substitute, then replace each remaining component with a short
  salted hash, extension and its case preserved (`.CR2` vs `.jpg` is often the answer). Counts,
  sequence, depth, extensions and repeat-offender correlation all survive, which is what a
  diagnosis actually uses.
- **Fail closed on the remainder.** Any line still looking like it holds a path is dropped
  **whole**. Losing a line costs a little diagnostic value; keeping it costs the promise.
- **No opt-out.** An "include real folder and file names" checkbox was considered and
  **rejected**: it trades a permanent public leak for a marginal debugging convenience, most
  users will not read it carefully, and the exact filename is almost never what settles a
  report. Dropping it also leaves the collector with one code path, no mode to test, and no
  ambiguity about what a given report contains.

Two invariants follow. **`./issues` holds redacted zips and nothing else, ever**, because it
is the folder the app points Explorer at, and anything un-redacted beside the zip is a
drag-and-drop accident waiting to happen. And because the hash is one-way for the **user**
too, a hash-to-name **mapping file** keeps "what is `7c2e.JPG`?" answerable; it goes to
`logs/`, never to `./issues` and never into the zip.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## What building it changed

Five findings, none of which the plan anticipated. They are the argument against later
"simplifications", which is why this section exists at all.

### 1. A loosened fail-closed rule turns drops into leaks

The first cut asked only "is anything path-shaped left in this line". Hashing **one**
component satisfied it, so this real line was emitted with the private folder intact:

```
[2/2] Poze (Fototarget)\2005-10-24\098.avi -> 2X: 160x120 327f
```

The date matched the name dictionary and was hashed; `Poze (Fototarget)`, separated from
`[2/2]` by a single space, was never isolated as a segment. The rule is now per-separator and
asymmetric: **every** component of a backslash-joined token must resolve or the line goes.

The lesson generalises: **a loosened fail-closed rule does not fail loudly, it converts drops
into leaks**, and only an adversarial pass over real logs finds it.

### 2. The vision model's descriptions, which no path rule can reach

Found by reading an actual generated zip, after everything above already passed. Tag & Rename
logs the model's own sentence about each picture, and the name condensed out of it:

```
           -> 0001_Kitten_Walking_Snowy_Surface.png  (renamed)
           -> "A kitten with striking blue eyes and a fluffy coat is walking on a snowy surface..."
```

This is the **worst** disclosure in the feature and the redactor was blind to it by
construction: not a path, not a folder name, not in any dictionary, just free English prose.
It is also qualitatively different from a leaked folder name, because a collection's worth of
these lines is a description of somebody's family, which for an app whose purpose is reviving
personal photo collections is the entire point of the data.

So the rule is **removal, not redaction**, and it is a rule about the line's SHAPE, because
prose cannot be pattern-matched: measured across every log this install holds, 6,541 lines
start with an arrow, **all** of them are Tag & Rename output, and every one carries a
description or a generated name. No other runner uses the shape.

**The line goes entirely, placeholder included.** Two intermediate cuts kept a
`-> [description withheld]` marker, the second re-attaching the outcome `(renamed)` through an
allowlist. Both were wrong for the same reason: **a line that cannot say anything is not worth
the line.** The per-image outcomes are already totalled in the run's own summary table, so
nothing is lost; what the placeholder added was one line of noise per image, thousands of
them, in a file whose entire purpose is to be read. A counter reports how many went.

Three follow-ons, all found by auditing against the descriptions' **own harvested
vocabulary** rather than a guessed word list:

- The Undo section prints the current name as a **bare line with no path around it**, and
  after a rename the name *is* the description. A bare media file name is now hashed exactly
  as it would be inside a path (media extensions only, so `cache.db` stays readable). With the
  arrow lines gone those bare-name lines go too: a column of anonymous hashes answers nothing.
- The counters were read off the redactor **before** the zip's logs went through it, so a real
  report announced "0 lines withheld" while its tag log had 5,716 removed. The URL body is now
  a **slice of the already-redacted zip text** rather than a second pass.
- Asking what the surviving line is FOR: a tag log's per-image line is
  `[21/100] 1280x960px  <path>`, where the counter and dimensions are its entire diagnostic
  content. So in Tag & Rename logs a path is **removed, not hashed** (`STRICT_PATH_TOOLS`),
  keeping the root token only where the path is exactly a root, because "was it pointed at the
  source folder instead of the upscaled one?" is a documented mistake the legend answers
  without naming anything. This is per-**tool** policy, not per-line parsing, so a reworded log
  cannot quietly reintroduce the path. **Collecting less is the only protection a later bug in
  a redaction rule cannot undo.**

Two post-conditions came from reading the result. Removing a path must never leave a dangling
`Cache:`, because an empty-looking field reads as "the app recorded nothing here". And **a
quoted path ends at its closing quote**: the runners print `Scanning 'D:\...\Benchmark' ...`,
where the end-of-line rule ate the quote and the ellipsis and left `Scanning '`, which reads as
a truncated log. That end is knowable exactly rather than guessed, so it is the one case that
does not need the end-of-line rule at all.

**The generalisation worth keeping: an app that generates text about the user's data has a
disclosure channel that no structural rule will find.** Auditing has to run against real
output, with the vocabulary harvested from that output.

### 3. `db/cache.db` is the redaction dictionary

The one file that must never be attached is also the best source of redaction knowledge, and
the inversion is the happiest part of the design: **the file we refuse to ship is what makes
the logs safe to ship.** It supplies both halves `config.defaults` could not:

- **Folders the app worked on before.** Logs are per-folder and long-lived, and one older log
  was dropping **78.5%** of its lines purely because its source is no longer a configured
  default. With the recorded roots added: 0.0%.
- **The name dictionary.** Walking the source tree for folder names took **145 seconds** over
  the SMB mount holding the photos, against **0.16 s** for the same 34,991 names from a local
  SQL scan.

It is opened read-only through a URI, so a diagnostics run can never migrate, lock or write
the cache.

### 4. Two patterns were too greedy, and both cost diagnostic value rather than privacy

`http://localhost:11434` matched the drive-letter rule as drive `p:`, so every line mentioning
the Ollama URL was dropped and the setting itself came out as `<unrecognised path>`; a drive
letter is ONE letter, so the pattern now has a lookbehind. And "a config value containing a
slash or a colon is a path" mangled the camera-filename regexes along with that URL; the test
is now what a path actually **starts** with. Between them the drop rate fell from 0.29% to
0.21%.

### 5. The review dialog needed a second pass after looking at it

The redaction summary and the pointer to the private map were inside the scrolling file list,
below the fold, behind a scrollbar nobody drags. They are the two things the user most needs
to read, so they are fixed labels now and the box holds nothing but the file table.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## Where the last-run summary comes from

**The newest log per tool, tail taken verbatim, nothing new stored.** The alternative was
persisting a ring of the `last_run` dicts already published to MQTT and then dropped. Two
samples settled it: the trigger report is answered by the last eight lines of an upscale log
with no parsing at all, and Tag & Rename already ends its log with a formatted summary table.
The information was never missing; it simply never left the machine.

**No parser, deliberately.** Five log prefixes, each ending in a different shape, and none of
those shapes is a contract: they are human-readable output that gets reworded whenever a
runner is touched. A parser over them breaks **silently**, which is the one failure this
feature cannot afford, since its whole purpose is to arrive when nobody is watching. A tail
cannot break. Three properties come free: it is **retroactive** (it reads runs from before the
feature existed), it needs **no schema change**, and it is the only approach that works for
the case that matters most, a run that **crashed** and therefore has no summary block at all.

Mechanics: select by mtime per prefix; the most recently modified log gets a real tail in the
URL body and the others one line each, with full tails in the zip; collapse the tail through
the log window's own `COLLAPSE_PROCESSING_RE`, so it is literally what the user saw on screen;
and **the redactor runs on the body too, not only on the zip**, because those tails are made
of paths.

One requirement is easy to miss: real logs contain the **8.3 short form**
(`C:\Users\EDUARD~1\AppData\Local\Temp\...`), because a child process was launched with a
short-form cwd. A root table built from `%USERPROFILE%` in its long form alone will not match
it and the fail-closed rule would drop those lines wholesale, so **both spellings of every
root are registered**. Note that deriving the short form needs the path to **exist**, which is
also why a unit test must supply it rather than rely on the machine having one.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## The GitHub template question

Three things were verified before deciding:

1. **Issue forms (YAML) and `?body=` do not compose.** A form is pre-filled per field, as
   `?template=bug.yml&<field-id>=value`. A plain `?body=` has no field to land in.
2. **A URL query is a documented way to bypass the template chooser**, so `?body=` keeps
   opening a blank editor once templates exist. GitHub's maintainers treat that as a defect to
   be closed, not as a contract.
3. `blank_issues_enabled: false` only hides the Blank option from the chooser for non-write
   users. It is not enforcement.

**The decision is a markdown template, with the app staying on `?body=` and no `template=`
parameter**, and the reason is an asymmetry: **installs are immutable, the repo is not.** An
install shipped today keeps sending whatever URL it was compiled with, forever. Point that URL
at `?template=bug.yml&diagnostics=...` and it now depends on a filename and a field id living
in a repo that will be edited by somebody who has forgotten the coupling; rename either and
GitHub silently ignores the unknown parameter, so **every install older than the change starts
sending empty reports with nothing failing loudly.** A `?body=` URL references nothing in the
repo and cannot go stale. `config.yml` keeps `blank_issues_enabled: true` for the same reason.

The counter-argument is real and worth recording: a form with `validations: required` on "What
happened?" is the only mechanism that stops a two-word report at the source, and a named
`diagnostics` textarea would make the block greppable rather than prose. It is refused because
this feature's premise is that the two-word report is normal and the **automated** half is the
lever; a required field mostly converts "not working" into "not working." typed into a box.

**Verified live, 2026-08-22**, from a real install with `.github/ISSUE_TEMPLATE/bug.md`
already in the repo: the app's link opens the **blank** editor with the body intact, down to
the line naming the zip, rather than landing at `/issues/new/choose`. That is the half that
could have failed, and it is the empirical half of point 2. **Worth re-checking whenever the
template set changes** - though the failure would be loud rather than silent (the user lands
on a chooser page with an empty form), which is the one saving grace. The drag itself is
GitHub's ordinary `.zip` attach, well inside its 25 MB ceiling; the zip is capped at roughly
10 MB for headroom, and redacted text compresses hard, so that is a great deal of log.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## Measured

On this developer's own logs: **~201,000 real lines audited twice**, first against 15 private
terms drawn from the actual tree and then against a content-word list harvested from the
model's own descriptions.

| | |
|---|---|
| Leaks of either kind | **zero** |
| What the audit still flags | ordinary log English ("already tagged", "Force rename mode") and one substring accident (`river` inside `driver`) |
| Lines dropped | 0.23% |
| Lines removed as model output | 4.15% |
| Throughput | ~41,000 lines/s, a 0.5 s build |
| A report | a ~4,200-character pre-filled URL (cap 7,800) plus a ~40 KB zip |

Every distinctive word (village, hillside, donkey, romanian, castle) is gone.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>

---

## Rules to keep

- **The URL carries no `template=` parameter** and must not grow one.
  `test_the_issue_url_carries_no_template_parameter` says why in full.
- **`./issues` holds redacted zips and nothing else, ever.** That is what makes dragging from
  it safe. The hash-to-name map is the private half and goes to `logs/`, pruned on a longer
  schedule than the zips because it stays useful for as long as the ISSUE is open.
- **There is no "include real names" opt-out**, and adding one would undo the design rather
  than extend it.
- **Fail-safe throughout.** A report that cannot be gathered falls through to the plain
  pre-filled issue, because the one thing this must never do is stop somebody reporting a bug.

<div align="right"><a href="#bug-reports">↑ Back to top</a></div>
