"""
diagnostics.py
--------------
Future feature #24: make a bug report actionable without asking the user anything.

This module is the half that has to be right: it turns the app's own logs and
state into something that can safely leave the machine. The GUI half (the
pre-filled issue body, the zip, the review dialog) is built on top of it.

WHY A REDACTOR AND NOT A FILTER
-------------------------------
Measured on this developer's own tree: a real upscale log carries one absolute
path per file, tens of thousands of times, and those paths are the private part.
A sampled line reads

    X:\\Personale\\Poze\\Oracle\\Irinel  Poze Cairo\\Cairo5\\Picture 209.jpg

which is a person's name, a place and a private folder taxonomy on one line. After
Tag & Rename has run, the *filenames* are AI-written descriptions of private
photos, so a tagged tree's log reads as a caption list of someone's family album.

A regex that tries to find "the path" in free text cannot bound it: Windows names
contain spaces, and `Irinel  Poze Cairo` even contains a double space, so any
pattern either stops early (leaking the tail) or swallows the rest of the line. So
this module does not look for paths in general. It substitutes the roots the app
ALREADY KNOWS, hashes everything after them, and then refuses (drops) any line that
still looks like it holds a path. Redaction is a rule about what is collected, not
a filter applied afterwards.

THE THREE MEASUREMENTS THAT SHAPED IT
-------------------------------------
1. `A path runs to the end of its line.` Of 16,128 path-bearing lines across five
   real logs, exactly 14 had anything after the path, and 12 of those were the
   double space INSIDE `Irinel  Poze Cairo`, not a separator. Only 2 were genuine
   trailing text, both of the form `<path>  (would lose transparency)`. Hence
   `split_trailing_note`: consume to end of line, but give back a trailing
   parenthesised note that contains no slash. That is 14/14 correct on the sample
   and needs no guess about where a name ends.
2. `Real logs carry the 8.3 short form.` `C:\\Users\\EDUARD~1\\AppData\\Local\\Temp\\...`
   appears verbatim, so a root table built from the long spelling of %USERPROFILE%
   alone would not match it and the fail-closed rule would then drop those lines
   wholesale. Every root is registered in BOTH spellings.
3. `A private name can appear with no path syntax at all.` The Batch Upscaler
   prints folder headers as bare relative names (`[folder]  James (cats mainly)`),
   which no drive-letter rule can catch. That is what `names` is for: a dictionary
   of the user's real folder names, matched as WHOLE SEGMENTS so a folder called
   "2006" cannot rewrite half of a timestamp.

Everything here is stdlib-only, torch-free and fail-safe: a report that cannot be
built must never take the app down with it, and a redactor that hits something it
does not understand drops the line rather than guessing.
"""

import io
import os
import re
import sys
import glob
import json
import zipfile
import secrets
import hashlib
import datetime
import platform
import collections

try:                                                    # optional, never required
    from debug_log import debug_log
except Exception:                                       # pragma: no cover
    def debug_log(*_a, **_k):
        pass

# scripts/ -> app root (config.json, logs/, db/, issues/ all live there)
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The folder the app writes reports into, and the ONE invariant about it: it holds
# redacted zips and nothing else, ever. It is the folder the app opens in Explorer
# for the user to drag from, so anything un-redacted sitting beside the zip is a
# drag-and-drop accident waiting to happen. The hash->path map (which is the
# private half) goes to logs/, never here.
ISSUES_DIRNAME = "issues"
LOGS_DIRNAME = "logs"

# What a dropped line becomes. Distinctive on purpose: a maintainer reading a
# report should be able to tell "the app removed this" from "the run printed
# nothing", and should be able to count them.
REDACTED_LINE = "[redacted: unrecognised path]"

# One tool per log-file prefix. The runners each write `<prefix>_<folderhash>.log`,
# so the newest file matching a prefix is that tool's most recent run. Order is the
# app's tab order.
TOOL_LOG_PREFIXES = [
    ("Batch Upscaler",      "log_"),
    ("Tag & Rename",        "tag_"),
    ("Video Upscaler",      "video_"),
    ("Conciliation",        "conc_"),
    ("Video Stabilization", "stab_"),
]

# The config keys whose values are user folders. Every one of these is a root to
# be tokenised. Kept as an explicit list rather than "everything under defaults"
# so a future non-path default cannot silently become a redaction root.
DEFAULT_FOLDER_KEYS = [
    "upscale_source", "upscale_output", "tag_folder",
    "conciliate_original", "conciliate_processed",
    "video_source", "video_output",
    "stabilize_source", "stabilize_output",
]

# A path start we did not manage to tokenise: a drive letter or a UNC share. If one
# of these survives substitution the line is dropped whole (fail closed).
#
# The lookbehind is load-bearing and was found by reading real output: without it
# `http://localhost:11434` matches as drive `p:` and every line mentioning the Ollama
# URL is dropped. A drive letter is ONE letter, so anything alphanumeric in front of
# it means this is not one. URLs are diagnostic and not private; losing them to a
# pattern meant for `X:\` is pure cost.
_RESIDUAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/])|(?:\\\\[^\\/\s])")

# (A backslash joining anything unredacted is the OTHER half of the fail-closed
# rule; it needs to know which spans are already redacted, so it is not a plain
# pattern. See `_has_unresolved_backslash`.)

# Trailing note after a path: two-or-more spaces, then a parenthesised group with
# no slash in it. See measurement (1) in the module docstring.
_TRAILING_NOTE_RE = re.compile(r"(\s{2,}\([^()\\/]*\))\s*$")

# ── MODEL OUTPUT ABOUT THE CONTENT OF A PRIVATE PHOTO ────────────────────────
# Tag & Rename writes the vision model's own description of each picture into its
# log, and the name it generates from that description:
#
#     -> 0001_Kitten_Walking_Snowy_Surface.png  (renamed)
#     -> "A kitten with striking blue eyes and a fluffy coat is walking on snow..."
#
# No path rule can catch this. It is not a path, not a folder name, and not in any
# dictionary: it is free English prose describing what is IN the photograph, which
# for this app's whole purpose means somebody's family. A collection's worth of
# those lines says far more about a person than the folder names ever could, and
# publishing them is the failure this feature exists to prevent.
#
# So the rule is not redaction but REMOVAL, and it is a shape rule rather than a
# content rule, because prose cannot be pattern-matched. Measured across every log
# this install has: 6,541 lines start with an arrow, ALL of them in Tag & Rename
# logs, and every single one carries either a description or a generated name. No
# other runner uses the shape at all (the Video Upscaler's `a.avi -> b.mp4` sits
# mid-line, never at the start).
_MODEL_OUTPUT_RE = re.compile(
    r"^(?P<stamp>(?:\d{4}-\d\d-\d\d \| \d\d:\d\d:\d\d \| )?)\s*->\s*(?P<rest>\S.*)$")

# The whole line goes, INCLUDING its trailing outcome marker (`(renamed)`,
# `(file was not renamed by this script)`). An earlier cut kept those through an
# allowlist of exact literals: safe, but not worth the line it rides on, since the
# same outcomes are already totalled in the run's own summary table a few lines
# further down. Per-image attribution is the only thing lost, and a reporter who
# needs it can say which image in their own words.

# Such a line is DROPPED, not replaced. `Redactor.line` returns this sentinel and
# the caller emits nothing at all.
#
# The first cut left a placeholder behind, on the reasoning that the report should
# still show an image was processed at that moment and how it turned out. That was
# wrong twice over. The per-image outcome is already in the run's summary table, so
# nothing is actually lost; and a placeholder repeated once per image is thousands of
# lines of noise in a file whose whole purpose is to be read by someone debugging.
# The counter (`Redactor.withheld`) is what tells the user this happened, and it is
# reported in the dialog and in the body.
OMIT = object()

# A line whose whole content is ONE media file name, with no path around it. The
# Undo listing prints exactly this, one per file, above its arrow line:
#
#       001_Mountain_Village_with_Mist_and.jpg
#                -> EXIF restored, renamed back to 001.jpg
#
# After Tag & Rename has run, that name IS the description, so it is as sensitive
# as the prose. It is a filename with the path left off, so it gets what every
# other filename in a report gets: hashed, not dropped. That keeps the listing's
# shape and lets it correlate with the same file mentioned elsewhere.
#
# The extension list is what keeps this safe to apply everywhere: `cache.db`,
# `worker_settings.json` and `video_benchmark.log` are diagnostics and must stay
# readable, while a media file name is private by default. The set is the union of
# what the app's own tools accept (batch_upscale / conciliate / raw_decode), kept
# here as a literal rather than imported, so this module stays free of the runners.
_MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    ".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg", ".flv",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".pef", ".srw",
}
_BARE_NAME_RE = re.compile(
    r"^(?P<stamp>(?:\d{4}-\d\d-\d\d \| \d\d:\d\d:\d\d \| )?)"
    r"(?P<indent>\s*)(?P<name>[^\s\\/:*?\"<>|][^\\/:*?\"<>|]*"
    r"(?P<ext>\.[A-Za-z0-9]{1,5}))\s*$")

# Placeholder wrapper for text this module has ALREADY redacted. The residual-path
# check must never inspect our own output: a `<ROOT1>` token ends in a digit and is
# followed by a separator, which is indistinguishable from a private relative path to
# any pattern looking at the finished line. So each redacted span is parked behind a
# placeholder, the check runs on what is left, and the spans are put back afterwards.
# The wrapper character cannot appear in a path, a hash or a root token.
_HOLE = "\x00"
_HOLE_RE = re.compile("\x00" + r"(\d+)" + "\x00")

# Segment separators for whole-segment dictionary matching: a slash of either kind,
# or a run of two or more spaces (which is how the runners column-align).
_SEGMENT_SPLIT_RE = re.compile(r"([\\/]|\s{2,})")

# A component hash is 6 hex characters. Four would collide constantly inside one
# report (30k files into a 65k space), which would destroy the one thing hashing is
# meant to preserve: being able to tell that the SAME file failed three times.
_HASH_LEN = 6

# Dictionary names shorter than this are not replaced. A two-character folder name
# carries almost nothing, and matching it risks mangling ordinary log text.
_MIN_DICT_NAME = 3


# ─────────────────────────────────────────────────────────────
#  ROOTS
# ─────────────────────────────────────────────────────────────

class Root(object):
    """One redaction root: a token, the human labels that explain what it was, and
    every spelling of the path that should map to it (long form and 8.3 short form).

    `labels` is a list because several config keys routinely point at ONE folder
    (upscale_source, conciliate_original, video_source and stabilize_source are all
    the same photo tree on a normal install). Reporting that they coincide is itself
    diagnostic, and it costs nothing: the legend says so without naming the path.
    """

    __slots__ = ("token", "labels", "spellings")

    def __init__(self, token, labels, spellings):
        self.token = token
        self.labels = list(labels)
        self.spellings = list(spellings)

    def __repr__(self):                                 # pragma: no cover
        return "Root(%r, %r, %d spelling(s))" % (
            self.token, self.labels, len(self.spellings))


def _path_spellings(path):
    """Every spelling of `path` worth matching: as given, and the Windows 8.3 short
    form if the path exists and the API answers.

    Measurement (2): real logs contain `C:\\Users\\EDUARD~1\\AppData\\...` because a
    child process was launched with a short-form cwd. Matching only the long form
    would leave those lines to the fail-closed rule, which would drop them entirely,
    so the report would arrive gutted rather than leaked. Fail-safe: any failure
    here just means fewer spellings, never an exception.
    """
    out = []
    for p in (path, os.path.abspath(path)):
        p = (p or "").rstrip("\\/")
        if p and p not in out:
            out.append(p)
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        for p in list(out):
            n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024)
            if n and buf.value and buf.value not in out:
                out.append(buf.value.rstrip("\\/"))
    except Exception:
        pass                                            # not Windows, or no such path
    return out


def build_roots(cfg=None, app_root=None, env=None, extra=()):
    """Build the redaction root table, longest path first.

    Longest-first is what makes nesting work without special cases: on a normal
    install `defaults.upscale_output` sits INSIDE `defaults.upscale_source`
    (`X:\\...\\Poze\\__upscaled__` under `X:\\...\\Poze`), and the more specific of
    the two has to win or every output path would be reported as a source path.

    `extra` is additional (label, path) pairs, normally the folders `roots_from_db`
    recovered from the cache. They come last so the CURRENT settings get the
    clearer labels when both describe one folder.
    """
    cfg = cfg or {}
    env = os.environ if env is None else env
    app_root = app_root or APP_ROOT

    # (label, path) in the order they should be OFFERED a token; the sort below
    # decides the actual matching order.
    candidates = []
    defaults = cfg.get("defaults") or {}
    for key in DEFAULT_FOLDER_KEYS:
        val = (defaults.get(key) or "").strip()
        if val:
            candidates.append(("defaults." + key, val))
    candidates.append(("the app install folder", app_root))
    for var in ("USERPROFILE", "TEMP", "TMP"):
        val = (env.get(var) or "").strip()
        if val:
            candidates.append(("%" + var + "%", val))
    for label, val in extra or ():
        if (val or "").strip():
            candidates.append((label, val.strip()))

    # Group by normalised path so one folder gets one token even when four config
    # keys point at it. Windows compares paths case-insensitively, so key on lower.
    grouped = collections.OrderedDict()
    for label, path in candidates:
        norm = os.path.normpath(path).rstrip("\\/").lower()
        if not norm:
            continue
        if norm not in grouped:
            grouped[norm] = {"labels": [], "path": path}
        if label not in grouped[norm]["labels"]:
            grouped[norm]["labels"].append(label)      # several rows, one folder

    roots = []
    for norm, item in grouped.items():
        roots.append(Root("", item["labels"], _path_spellings(item["path"])))

    # Longest first, measured on the longest spelling each root offers.
    roots.sort(key=lambda r: max((len(s) for s in r.spellings), default=0),
               reverse=True)
    for i, r in enumerate(roots, 1):
        r.token = "<ROOT%d>" % i
    return roots


# ─────────────────────────────────────────────────────────────
#  PURE HELPERS  (display-free, filesystem-free, unit-tested)
# ─────────────────────────────────────────────────────────────

def component_hash(name, salt=b""):
    """Hash one path component, preserving its extension.

    The extension is kept because it is not private and it is load-bearing for
    diagnosis: `.CR2` vs `.jpg` vs `.tif` is often the whole answer, and the case is
    kept too (a case-only difference is exactly the class of bug #22 dealt with).

    The salt is per-report and random: without one, a 6-hex digest of a short folder
    name is trivially recovered by guessing, which would undo the redaction for the
    common names ("Poze", "2006", a first name). With one, correlation still works
    inside a report, which is all it is for.
    """
    if not name:
        return name
    stem, dot, ext = name.rpartition(".")
    keep_ext = bool(dot) and bool(stem) and len(ext) <= 5 and ext.isalnum()
    subject = (stem if keep_ext else name).lower().encode("utf-8", "replace")
    digest = hashlib.blake2b(subject, key=salt[:64], digest_size=8).hexdigest()
    short = digest[:_HASH_LEN]
    return short + "." + ext if keep_ext else short


def split_trailing_note(tail):
    """Split a trailing `  (note)` off the end of a path tail.

    Measurement (1): a path runs to the end of its line, with exactly one observed
    exception shape, `<path>  (would lose transparency)`. Requiring no slash inside
    the parentheses is what stops `Irinel  Poze Cairo\\Cairo5\\Picture 209.jpg` from
    being mistaken for one (its double space is part of a folder name).

    Returns (path_part, note) where note keeps its leading whitespace, or "".
    """
    m = _TRAILING_NOTE_RE.search(tail or "")
    if not m:
        return tail, ""
    return tail[:m.start()], m.group(1)


def hash_tail(tail, salt=b""):
    """Hash every component of a path tail, keeping the separators.

    Depth survives (a maintainer can see the tree is four deep), extensions survive,
    the same file hashes the same way twice, and nothing else does.
    """
    if not tail:
        return tail
    parts = re.split(r"([\\/])", tail)
    return "".join(p if p in ("\\", "/") else component_hash(p, salt) for p in parts)


def segments(text):
    """Split a line into whole segments for dictionary matching: on slashes and on
    runs of two or more spaces (how the runners column-align their output).

    Whole-segment matching is what makes the name dictionary safe. A folder named
    "2006" must not rewrite the "2006" inside a timestamp, and only an anchored,
    fully-delimited match can promise that.
    """
    return _SEGMENT_SPLIT_RE.split(text or "")


def _has_unresolved_backslash(text):
    """True when a backslash in `text` still joins something we did not redact.

    EVERY component of a backslash-joined token has to be resolved, not just one of
    them. This rule exists because loosening the previous one produced a real leak,
    measured on a real log:

        [2/2] Poze (Fototarget)\\2005-10-24\\098.avi -> 2X: 160x120 327f

    The date folder matched the name dictionary and was hashed, which was enough to
    satisfy a rule that only asked "is there anything path-shaped left". The private
    folder beside it, separated from `[2/2]` by a single space, was never isolated
    and sailed through. So the test is per-separator and asymmetric: whatever sits
    immediately either side of a backslash must be a redacted span (a placeholder)
    or whitespace. A folder header like `[folder]  <hash>\\<hash>` passes because
    each edge touching a separator is a placeholder; `Poze (Fototarget)\\<hash>`
    does not, because the left edge is `)`.
    """
    t = text or ""
    for i, ch in enumerate(t):
        if ch != "\\":
            continue
        before = t[i - 1] if i else " "
        after = t[i + 1] if i + 1 < len(t) else " "
        for edge in (before, after):
            if edge != _HOLE and edge != "\\" and not edge.isspace():
                return True
    return False


def has_residual_path(text):
    """True when a line still looks like it holds a path after substitution: a drive
    letter, a UNC share, or a backslash joining anything unredacted. Any of those
    means we did not recognise something, and the answer to that is to drop the line.

    Accepts text that still carries placeholders. Drive/UNC is checked with the
    placeholders blanked out (our own `<ROOT1>\\...` output would otherwise read as a
    leak); the separator rule is checked WITH them, since their presence is exactly
    what tells a resolved component from an unresolved one.
    """
    t = text or ""
    return bool(_RESIDUAL_PATH_RE.search(_HOLE_RE.sub(" ", t))) or \
        _has_unresolved_backslash(t)


# ─────────────────────────────────────────────────────────────
#  THE REDACTOR
# ─────────────────────────────────────────────────────────────

class Redactor(object):
    """Rewrites the app's own output so it can be attached to a public issue.

    Order of operations per line, and the order matters:

      1. substitute the longest matching known root, then hash the rest of the line
         as a path tail (giving back a trailing parenthesised note);
      2. replace any whole segment that matches a known private NAME (the bare
         relative folder headers a root rule cannot see);
      3. if anything path-shaped survives, drop the whole line.

    Step 3 is the one that makes the other two safe to be imperfect. Losing a line
    costs a little diagnostic value; keeping one we did not understand costs the
    promise the feature is built on.
    """

    def __init__(self, roots=None, names=(), salt=None):
        self.roots = list(roots or [])
        self.salt = secrets.token_bytes(16) if salt is None else salt
        self.dropped = 0
        self.withheld = 0                               # model-output lines removed
        self.used = set()                               # tokens that actually matched
        self.mapping = collections.OrderedDict()        # redacted -> original

        # One alternation over every spelling of every root, longest literal first
        # so a nested output folder wins over the source folder that contains it.
        spellings = []
        for root in self.roots:
            for s in root.spellings:
                spellings.append((s, root.token))
        spellings.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._token_of = {s.lower(): tok for s, tok in spellings}
        self._roots_re = (
            re.compile("|".join(re.escape(s) for s, _ in spellings), re.IGNORECASE)
            if spellings else None)

        # The name dictionary, longest first, matched whole-segment only.
        self._names = {}
        for n in names or ():
            n = (n or "").strip()
            if len(n) >= _MIN_DICT_NAME:
                self._names[n.lower()] = n

    # -- placeholders ---------------------------------------------------------
    # Redacted spans are parked behind a placeholder while the residual-path check
    # runs, then put back. Without this the check reads our OWN output as a leak: a
    # `<ROOT1>` token ends in a digit and is followed by a separator, which is
    # exactly the shape of the private relative paths the check exists to catch.

    def _park(self, holes, redacted):
        holes.append(redacted)
        return "%s%d%s" % (_HOLE, len(holes) - 1, _HOLE)

    @staticmethod
    def _unpark(text, holes):
        return _HOLE_RE.sub(lambda m: holes[int(m.group(1))], text)

    # -- step 0: remove what no redaction rule could ever catch ---------------

    def _withhold_model_output(self, text, strict=False):
        """OMIT a vision-model description / generated name line, or return None.

        The line goes entirely: it holds the model's sentence about the picture, or
        the file name condensed from it, and neither is of any use in a debugging
        session. Its outcome (`(renamed)`, `(file was not renamed by this script)`)
        is already counted in the run's summary table, so dropping the line costs the
        report nothing and saves it thousands of lines of placeholder.
        """
        bare = self._hash_bare_name(text, strict=strict)
        if bare is not None:
            return bare
        if not _MODEL_OUTPUT_RE.match(text or ""):
            return None
        self.withheld += 1
        return OMIT

    def _hash_bare_name(self, text, strict=False):
        """Handle a line that is nothing but one media file name, or return None.

        The Undo listing prints these with no path around them, and after a rename
        the name is the model's description of the picture. Normally it is hashed,
        exactly as the same name would be inside a path. In a strict-path log it is
        OMITTED, for the same reason the arrow line above it is: with the arrow lines
        gone, a column of anonymous hashes is noise that answers nothing.
        """
        m = _BARE_NAME_RE.match(text or "")
        if not m or m.group("ext").lower() not in _MEDIA_EXTS:
            return None
        if strict:
            self.withheld += 1
            return OMIT
        name = m.group("name")
        hashed = component_hash(name, self.salt)
        self._remember(hashed, name)
        return "%s%s%s" % (m.group("stamp"), m.group("indent"), hashed)

    # -- the three steps ------------------------------------------------------

    def _substitute_root(self, text, holes, strict=False):
        """Step 1. Replace the first known root and hash to end of line.

        `strict` drops the file part entirely instead of hashing it, and is used for
        logs where a file's identity carries no diagnosis at all (see
        STRICT_PATH_TOOLS). The root TOKEN survives when the path is exactly a root,
        because naming the tree answers a real question -- "was Tag & Rename pointed
        at the source folder instead of the upscaled one?" is a documented mistake,
        and the legend resolves `<ROOT3>` to `defaults.upscale_source` without naming
        anything. Naming a FILE inside that tree answers nothing.
        """
        if not self._roots_re:
            return text
        m = self._roots_re.search(text)
        if not m:
            return text
        token = self._token_of.get(m.group(0).lower(), "<ROOT?>")
        self.used.add(token)
        head, tail = text[:m.start()], text[m.end():]

        # A QUOTED path ends at its closing quote, and that is knowable exactly
        # rather than guessed. The runners print folders this way ("Scanning
        # 'D:\\...\\Benchmark' ..."), and the end-of-line rule ate the closing quote
        # and everything after it, leaving `Scanning '`. It read as a truncated log
        # and it also hashed the `' ...` into a meaningless component. Measurement
        # (1) counted lines with trailing text after TWO spaces, so a single space
        # before an ellipsis was never in that sample.
        quote = head[-1] if head else ""
        rest = ""
        if quote in ("'", '"'):
            close = tail.find(quote)
            if close != -1:
                tail, rest = tail[:close], tail[close:]
        tail, note = split_trailing_note(tail)

        if strict:
            # A bare root keeps its token; a path INTO the tree goes completely,
            # taking the whitespace that separated it from the text before it.
            if not tail.strip():
                return head + token + note + rest
            if rest:                       # quoted: drop both quotes with the path
                return "%s[path removed]%s%s" % (head[:-1], rest[1:], note)
            kept = head.rstrip()
            # Post-condition, not a parse: if removing the path left a dangling
            # label (`Cache:`), say so. An empty-looking field reads as "the app
            # recorded nothing here", which is a bug report of its own -- the same
            # misreading that made a silent "0 lines withheld" worse than useless.
            if kept.endswith((":", "=")):
                kept += " [path removed]"
            return kept + note
        redacted = token + hash_tail(tail, self.salt)
        self._remember(redacted, m.group(0) + tail)
        # A second path later on the same line is inside `tail` and was hashed with
        # it. That loses the "A -> B" wording on the rare move/copy line, which is a
        # price worth paying for never having to guess where the first path ended.
        return head + self._park(holes, redacted) + note + rest

    def _substitute_names(self, text, holes):
        """Step 2. Replace whole segments that match a known private name.

        Two levels, and the order matters. Slashes are split first and each chunk is
        offered to the dictionary WHOLE, because a real folder name can itself
        contain the double space that the runners use as a column separator
        (`Irinel  Poze Cairo` is one folder, not two columns). Only if the whole
        chunk is unknown is it split on runs of spaces and the pieces tried. Without
        the whole-chunk pass, that folder never matches and the line is dropped by
        step 3: safe, but it needlessly loses the folder headers that give an
        upscale log its structure.
        """
        if not self._names:
            return text
        out = []
        for chunk in re.split(r"([\\/])", text):
            out.append(chunk if chunk in ("\\", "/")
                       else self._match_names(chunk, holes))
        return "".join(out)

    def _match_names(self, chunk, holes):
        """Greedy LONGEST dictionary match over one slash-delimited chunk.

        Splitting on runs of spaces and testing each piece is not enough, because a
        real folder name can contain the same double space the runners use to
        column-align (`Irinel  Poze Cairo` is one folder). So candidates are built by
        joining consecutive segments, longest first: the whole chunk is tried before
        its halves, and the halves before the individual pieces. That way a name
        containing a separator still matches, while a match can still only ever start
        and end on a segment boundary, which is what keeps a folder called "2006"
        from rewriting half of a timestamp.
        """
        segs = segments(chunk)
        out, i, n = [], 0, len(segs)
        while i < n:
            hit, end = None, i
            for j in range(n, i, -1):
                cand = "".join(segs[i:j]).strip()
                if len(cand) < _MIN_DICT_NAME:
                    continue
                real = self._names.get(cand.lower())
                if real is not None:
                    hit, end = real, j
                    break
            if hit is None:
                out.append(segs[i])
                i += 1
            else:
                out.append(self._hash_segment("".join(segs[i:end]), hit, holes))
                i = end
        return "".join(out)

    def _hash_segment(self, seg, real, holes):
        """Replace one whole segment with its hash, keeping the surrounding space."""
        lead = seg[:len(seg) - len(seg.lstrip())]
        trail = seg[len(seg.rstrip()):]
        hashed = component_hash(real, self.salt)
        self._remember(hashed, real)
        return lead + self._park(holes, hashed) + trail

    def line(self, text, strict=False):
        """Redact one line.

        Returns the redacted text, REDACTED_LINE if anything path-shaped survives,
        or the OMIT sentinel for a line that must not appear at all. Callers that
        build a document from many lines should use `lines()`, which filters OMIT
        for them; a caller handling one line at a time has to check for it.
        """
        # FIRST, before any path handling: a description is prose, so none of the
        # rules below can see it, and it is the most sensitive thing in these logs.
        withheld = self._withhold_model_output(text, strict=strict)
        if withheld is not None:
            return withheld
        holes = []
        try:
            out = self._substitute_root(text or "", holes, strict=strict)
            out = self._substitute_names(out, holes)
        except Exception as exc:                        # never raise into a report
            debug_log("diagnostics: redaction failed, dropping line", exc)
            self.dropped += 1
            return REDACTED_LINE
        # Check what is LEFT, with every already-redacted span blanked out. Two
        # adjacent hashed folder names still have a separator between them, and to a
        # pattern hunting for `name\name` that is indistinguishable from the private
        # relative path it exists to catch.
        if has_residual_path(_HOLE_RE.sub(" ", out)):
            self.dropped += 1
            return REDACTED_LINE
        return self._unpark(out, holes)

    # -- convenience ----------------------------------------------------------

    def lines(self, seq, strict=False):
        """Redact many lines, dropping the ones that must not appear at all."""
        out = []
        for ln in seq:
            got = self.line(ln, strict=strict)
            if got is not OMIT:
                out.append(got)
        return out

    def text(self, s, strict=False):
        """Redact a multi-line string, preserving line structure."""
        return "\n".join(self.lines((s or "").split("\n"), strict=strict))

    def path(self, p):
        """Redact a single path VALUE (a settings echo, not a log line).

        Unlike `line`, an unrecognised path here is replaced with a placeholder
        rather than dropped: the field's presence is itself information ("the output
        folder is set to something outside every known root") and losing the whole
        row would hide that.
        """
        if not p:
            return p
        holes = []
        out = self._substitute_root(p, holes)
        if has_residual_path(out):
            self.dropped += 1
            return "<unrecognised path, %d char(s)>" % len(p)
        return self._unpark(out, holes)

    def _remember(self, redacted, original):
        """Record redacted -> original for the map file written to logs/.

        The hash is one-way for the USER too, so without this nobody can answer
        "what is 7c2e.jpg?" when a maintainer asks. The map never enters the zip and
        never enters ./issues; see ISSUES_DIRNAME.
        """
        if redacted and redacted not in self.mapping:
            self.mapping[redacted] = original

    def legend(self):
        """The lines that explain the tokens without naming a single path.

        That several keys share one token is deliberately visible: "the source and
        the conciliation original are the same folder" explains a whole class of
        report, and says nothing private.
        """
        out = []
        # Only the roots that actually matched something. The recorded-roots table
        # can hold hundreds of folders this install once touched, and listing every
        # one of them would bury the handful the report is actually about. Before
        # anything has been redacted there is nothing to filter by, so show all.
        shown = [r for r in self.roots if r.token in self.used] or self.roots
        for root in shown:
            out.append("  %-9s = %s" % (root.token, ", ".join(root.labels)))
        return out


# ─────────────────────────────────────────────────────────────
#  THE CACHE DB AS A REDACTION DICTIONARY
# ─────────────────────────────────────────────────────────────
# `db/cache.db` is the one file that must NEVER be attached to a report: it is a
# complete index of the user's private tree. It is also, for exactly that reason,
# the best possible source of redaction knowledge. The file we refuse to ship is
# what makes the logs safe to ship.
#
# Two things come out of it, and each fixes a measured failure of the config-only
# root table:
#
#   * ROOTS the app has worked on BEFORE. `config.defaults` describes only the
#     CURRENT settings, while logs are per-folder and long-lived. Measured on real
#     logs: one older run's log had 78.5% of its lines dropped because its source
#     folder is no longer a configured default. With the recorded roots added, that
#     log is readable again without any loosening of the rules.
#   * NAMES, for the bare relative folder headers that carry no path syntax. The
#     obvious source, walking the source tree, took **145 seconds** over the SMB
#     mount that holds this developer's photos, which is far too slow for a button
#     press. The same names come out of a local SQL scan in well under a second.
#
# Opened read-only through a URI so a diagnostics run can never migrate, lock or
# write the cache, and imported nowhere: `db.py` is not needed for a plain read.

_DB_ROOT_QUERIES = [
    ("upscale source (recorded)", "SELECT source_root FROM upscale_roots"),
    ("upscale output (recorded)", "SELECT output_root FROM upscale_roots"),
    ("tag folder (recorded)",     "SELECT source_root FROM tag_roots"),
    ("video source (recorded)",   "SELECT source_root FROM video_roots"),
    ("video output (recorded)",   "SELECT output_root FROM video_roots"),
]

_DB_NAME_QUERIES = [
    "SELECT rel_path FROM upscale_files",
    "SELECT original_rel_path FROM tag_files",
    "SELECT current_rel_path FROM tag_files",
    "SELECT rel_path FROM video_files",
]


def _open_cache_readonly(db_path=None):
    """Read-only connection to db/cache.db, or None. Never creates or migrates."""
    path = db_path or os.path.join(APP_ROOT, "db", "cache.db")
    if not os.path.isfile(path):
        return None
    try:
        import sqlite3
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        return sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception as exc:
        debug_log("diagnostics: cache.db unreadable for redaction", exc)
        return None


def roots_from_db(db_path=None, limit=400):
    """Every folder this install has recorded working on, as (label, path) pairs."""
    conn = _open_cache_readonly(db_path)
    if conn is None:
        return []
    out = []
    try:
        for label, sql in _DB_ROOT_QUERIES:
            try:
                for (value,) in conn.execute(sql):
                    if value and len(out) < limit:
                        out.append((label, value))
            except Exception:
                continue                                # a table an older DB lacks
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def names_from_db(db_path=None, limit=200000):
    """Every folder and file NAME this install has indexed.

    Both halves are wanted. Folder names are the leak a root rule cannot catch (the
    bare `[folder]  James (cats mainly)` header); file names matter because Tag &
    Rename turns them into descriptions of the picture, so a bare filename in a log
    line is a caption whether or not a path is attached to it.
    """
    conn = _open_cache_readonly(db_path)
    if conn is None:
        return set()
    names = set()
    try:
        for sql in _DB_NAME_QUERIES:
            try:
                for (rel,) in conn.execute(sql):
                    if not rel:
                        continue
                    for part in re.split(r"[\\/]", rel):
                        part = part.strip()
                        if len(part) >= _MIN_DICT_NAME:
                            names.add(part)
                    if len(names) >= limit:
                        return names
            except Exception:
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return names


def make_redactor(cfg=None, names=None, app_root=None, env=None, salt=None,
                  db_path=None, use_db=True, extra=()):
    """Build a Redactor for this install.

    Fail-safe in the SAFE direction: on any error the result is a redactor with no
    roots, which recognises nothing and therefore DROPS every path-bearing line. A
    degraded report is the acceptable failure; a leaked one is not.

    `extra` adds (label, path) roots on top of whatever the DB offers. Tests use it
    to supply a spelling that `_path_spellings` could only DERIVE on a machine where
    that path exists, which is not a property a unit test may depend on.
    """
    extra, dict_names = list(extra or ()), set(names or ())
    if use_db:
        try:
            extra = list(roots_from_db(db_path)) + extra
        except Exception as exc:
            debug_log("diagnostics: could not read recorded roots", exc)
        if names is None:
            try:
                dict_names = names_from_db(db_path)
            except Exception as exc:
                debug_log("diagnostics: could not read name dictionary", exc)
    try:
        roots = build_roots(cfg, app_root=app_root, env=env, extra=extra)
    except Exception as exc:
        debug_log("diagnostics: could not build redaction roots", exc)
        roots = []
    return Redactor(roots, names=dict_names, salt=salt)


# ─────────────────────────────────────────────────────────────
#  LOG SELECTION  (feature #24: the last-run summary IS the log tail)
# ─────────────────────────────────────────────────────────────

def newest_logs(log_dir=None, prefixes=None):
    """The newest log file per tool, newest tool first.

    Deliberately NOT a parser. The five runners each end their log in a different
    shape and none of those shapes is a contract: they are human-readable output
    that gets reworded whenever a runner is touched, so a parser over them would
    break silently, which is the one failure this feature cannot afford. A tail
    cannot break, it is retroactive (it reads runs from before this feature
    existed), and it is the only thing that works for a run that CRASHED and
    therefore has no summary block at all.

    Returns [(tool_name, path, mtime), ...].
    """
    log_dir = log_dir or os.path.join(APP_ROOT, LOGS_DIRNAME)
    out = []
    for tool, prefix in (prefixes or TOOL_LOG_PREFIXES):
        newest, newest_mtime = None, -1.0
        try:
            for path in glob.glob(os.path.join(log_dir, prefix + "*.log")):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest, newest_mtime = path, mtime
        except Exception as exc:
            debug_log("diagnostics: could not scan logs for %s" % tool, exc)
        if newest:
            out.append((tool, newest, newest_mtime))
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def tail_lines(path, count=25, collapse_re=None, max_bytes=512 * 1024):
    """The last `count` lines of a log, optionally collapsing repeated progress.

    `collapse_re` is injected rather than imported so this module stays free of the
    gui package; the caller passes `gui.widgets.COLLAPSE_PROCESSING_RE`, the same
    pattern the log window's "Collapse repeating progress lines" toggle uses. Without
    it a video run's last 25 lines are 25 identical per-minute heartbeats. With it,
    the tail is what the user actually saw on screen, which is the point.

    Only the last `max_bytes` are read: these logs reach 7 MB.
    """
    try:
        size = os.path.getsize(path)
        with io.open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                           # discard a partial line
            raw = fh.read()
        lines = raw.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    except Exception as exc:
        debug_log("diagnostics: could not read log tail %s" % os.path.basename(path or ""), exc)
        return []

    if collapse_re is not None:
        lines = collapse_runs(lines, collapse_re)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-count:] if count else lines


def collapse_runs(lines, pattern):
    """Collapse each run of consecutive lines matching `pattern` down to its LAST
    one, matching the log window's display behaviour (the on-disk log keeps every
    line; only what we quote is collapsed)."""
    out = []
    for line in lines:
        if pattern.search(line or ""):
            if out and pattern.search(out[-1] or ""):
                out[-1] = line
                continue
        out.append(line)
    return out

# ─────────────────────────────────────────────────────────────
#  WHAT GOES IN A REPORT
# ─────────────────────────────────────────────────────────────
# Ordered by what a real report would have settled. The trigger for this whole
# feature was a report reading, in full, title "not working" / body "the output
# folders seem empty" / `GPU: NVIDIA GeForce RTX 2060`. The app knew the answer at
# the time and threw it away.

# Lines of log quoted in the pre-filled URL body, for the single most recent run.
# The URL is capped near 8 KB, so this is the one tail that gets real space.
URL_TAIL_LINES = 25

# Lines of each tool's log carried in the zip, where the URL cap does not apply.
ZIP_TAIL_LINES = 3000

# The zip is dragged into a GitHub comment box, which accepts 25 MB for a .zip.
# Redacted text compresses hard, so this is a great deal of log.
MAX_ZIP_BYTES = 10 * 1024 * 1024

# Reports kept in ./issues before the oldest are pruned, matching the newest-10
# rule the conciliation journal already uses.
KEEP_REPORTS = 10

# Config sections worth reporting. Everything else in config.json is either noise
# or a credential slot. `notifications` and `runpod` are excluded WHOLESALE rather
# than field-by-field: they are the two sections whose entire purpose is endpoints
# and keys, and an allowlist that has to be updated when a field is added is an
# allowlist that will one day not have been.
REPORTED_CONFIG_SECTIONS = ["upscale", "tagging", "ollama", "video", "seedvr2",
                            "defaults", "updates"]

# Which sections explain which tool. Used when the full settings block will not fit
# in the URL: rather than dropping all of it, keep the settings for the tool that
# actually ran, since "the most common 'not working' is a correct run the settings
# explain". Conciliation and Stabilization are driven by their folder choices, so
# `defaults` is what explains them.
# Tools whose logs get NO file identity at all, only the root token.
#
# Tag & Rename is the one that qualifies, and it qualifies twice over. Its per-image
# line reads `[21/100] 1280x960px  <path>`, where the counter and the dimensions are
# the entire diagnostic content: the file's name adds nothing that the counter does
# not, because the run is a sequence and the summary table already carries the
# outcome counts. And its file names are the most sensitive strings the app ever
# handles, since after a run they ARE the model's description of the picture. So the
# path is removed rather than hashed. Collecting less is the only protection that
# cannot be undone by a later bug in a redaction rule.
STRICT_PATH_TOOLS = {"Tag & Rename"}

TOOL_CONFIG_SECTIONS = {
    "Batch Upscaler":      ["upscale", "defaults"],
    "Tag & Rename":        ["tagging", "ollama", "defaults"],
    "Video Upscaler":      ["video", "defaults"],
    "Conciliation":        ["defaults"],
    "Video Stabilization": ["video", "defaults"],
}


def _app_version():
    """The app version, read from the GUI package that owns it. Guarded: this module
    is imported by tests and headless tooling where gui may not be importable."""
    try:
        from gui.common import APP_VERSION
        return APP_VERSION
    except Exception:
        return "unknown"


def _install_mode(app_root=None):
    """Local / Remote / Both, as the installer wrote it. Missing means an upgrade
    from before the file existed, which bootstrap.ps1 treats as "both"."""
    try:
        path = os.path.join(app_root or APP_ROOT, "install_mode.txt")
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or "unknown"
    except Exception:
        return "not recorded (treated as Both)"


def _ffmpeg_build(app_root=None):
    """The bundled ffmpeg build stamp. 0.6.0 pinned a master autobuild because every
    8.1.x corrupts memory in vidstabtransform, and the stamp is the only way to tell
    from outside which build an install actually has."""
    try:
        path = os.path.join(app_root or APP_ROOT, "ffmpeg", "build.txt")
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return " ".join(fh.read().split())[:200] or "empty"
    except Exception:
        return "absent (no bundled ffmpeg, or bootstrapped before build stamps)"


def venv_health(app_root=None):
    """Whether the .venv still points at a Python that exists.

    This is known-defects D1 in one line: a venv is NOT self-contained, so
    uninstalling the base Python leaves the app starting into nothing, with no
    window, no error and no crash log. From the outside that is indistinguishable
    from "the app does nothing when I click it", which is exactly the report this
    feature exists to answer.
    """
    root = app_root or APP_ROOT
    exe = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(exe):
        return "no .venv (Scripts/python.exe missing)"
    cfg = os.path.join(root, ".venv", "pyvenv.cfg")
    try:
        home = None
        with io.open(cfg, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.lower().startswith("home"):
                    home = line.split("=", 1)[1].strip()
                    break
        if not home:
            return "present, pyvenv.cfg has no home= line"
        if not os.path.isdir(home):
            return "BROKEN: pyvenv.cfg home= points at a folder that no longer exists (see D1)"
        return "ok"
    except Exception:
        return "present, pyvenv.cfg unreadable"


def gpu_summary():
    """Card name AND total VRAM. The name alone does not imply the memory: the RTX
    2060 shipped in 6 GB and 12 GB, and the 8 GB minimum is what decides whether a
    report is a bug at all (known-defects D4)."""
    name = vram = None
    try:
        import system_telemetry
        name = system_telemetry.gpu_name()
        sample = system_telemetry.sample_gpu() or {}
        vram = sample.get("gpu_total_mb")
    except Exception as exc:
        debug_log("diagnostics: GPU probe failed", exc)
    if not name:
        return "not detected (no NVIDIA driver, or a Remote-only install)"
    return "%s%s" % (name, (", %.1f GB VRAM" % (vram / 1024.0)) if vram else
                     ", VRAM total unknown")


def tracked_config(app_root=None):
    """config.json as it is on disk, with the secret slots blanked.

    Reads the TRACKED file directly and never `config_store.load()`, which would
    deep-merge `config.local.json` over it. That overlay is where the RunPod API key,
    the MQTT password and every notification token live, and the surest way never to
    report a credential is never to read the file that holds one.
    """
    try:
        path = os.path.join(app_root or APP_ROOT, "config.json")
        with io.open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        debug_log("diagnostics: config.json unreadable", exc)
        return {}
    out = {}
    for section in REPORTED_CONFIG_SECTIONS:
        value = cfg.get(section)
        if isinstance(value, dict):
            out[section] = dict(value)
    # Belt and braces: blank anything the secret list names that still landed here
    # (a pre-0.4.3 install that has not been migrated yet).
    try:
        from config_store import SECRET_FIELDS
        for dotted in SECRET_FIELDS:
            section, _, key = dotted.partition(".")
            if section in out and key in out[section]:
                out[section][key] = "<removed>"
    except Exception:
        pass
    return out


def looks_like_a_path(value, red=None):
    """True when a config VALUE is actually a path, rather than merely containing a
    slash or a colon.

    "Contains a separator" was the first rule and it was far too broad, which showed
    up immediately on real settings: `ollama.url` (`http://localhost:11434`) and
    `tagging.camera_filename_patterns` (regexes, full of backslash escapes) both came
    out as `<unrecognised path>`. Neither is private and both are diagnostic, so the
    test is narrowed to what a path actually starts with: a drive letter, a UNC
    prefix, or one of the roots this install already knows.
    """
    if not isinstance(value, str) or not value:
        return False
    if _RESIDUAL_PATH_RE.search(value):
        return True
    return bool(red is not None and red._roots_re and red._roots_re.search(value))


def _redact_config(cfg, red):
    """Redact a config tree: a value that really is a path goes through the redactor,
    everything else is a setting and is reported as-is."""
    if isinstance(cfg, dict):
        return {k: _redact_config(v, red) for k, v in cfg.items()}
    if isinstance(cfg, list):
        return [_redact_config(v, red) for v in cfg]
    if looks_like_a_path(cfg, red):
        return red.path(cfg)
    return cfg

# ─────────────────────────────────────────────────────────────
#  THE REPORT
# ─────────────────────────────────────────────────────────────

class Report(object):
    """One built report: the body that goes in the URL, the fuller text that goes in
    the zip, the files the zip carries, and the hash map that does NOT."""

    __slots__ = ("body", "full", "files", "mapping", "legend", "dropped",
                 "withheld", "salt")

    def __init__(self):
        self.body = ""
        self.full = ""
        self.files = []                                 # [(arcname, text), ...]
        self.mapping = {}
        self.legend = []
        self.dropped = 0
        self.withheld = 0
        self.salt = b""


def _environment_lines(app_root=None):
    """The fields that need no redaction at all: versions, mode, hardware, health.

    None of these can carry a path or a name, which is why they are gathered
    separately from anything that touches the user's tree.
    """
    return [
        "- Image Toolbox: %s" % _app_version(),
        "- Install mode: %s" % _install_mode(app_root),
        "- OS: %s" % platform.platform(),
        "- Python: %s" % sys.version.split()[0],
        "- GPU: %s" % gpu_summary(),
        "- Bundled ffmpeg: %s" % _ffmpeg_build(app_root),
        "- Virtual environment: %s" % venv_health(app_root),
    ]


def _settings_lines(red, app_root=None, sections=None, heading="*Settings:*"):
    """The settings that explain a run. The most common "not working" is a correct
    run that the settings explain."""
    cfg = _redact_config(tracked_config(app_root), red)
    out = []
    for section in (sections or REPORTED_CONFIG_SECTIONS):
        values = cfg.get(section)
        if not values:
            continue
        out.append("  [%s]" % section)
        for key in sorted(values):
            out.append("    %s = %s" % (key, values[key]))
    return ["", heading, ""] + out if out else []


def _run_lines(logs, redacted):
    """The last-run summaries: the newest run quoted at length, the rest named.

    The newest log across all five tools is almost always the run being reported, so
    it gets the space. Naming the others still answers "when did this tool last run
    at all", which is most of what the extra tails would have said.

    `redacted` maps a tool to the lines already redacted for the zip, and the quote
    is a SLICE of those rather than a second pass over the file. Redacting the same
    lines twice inflated the "N lines withheld" counter by the size of this excerpt
    (75 for 50 real lines), and a number the user is being asked to trust must not be
    an artifact of how many times the app looked at something. It also guarantees the
    excerpt and the attached log cannot disagree.
    """
    out = []
    for index, (tool, path, mtime) in enumerate(logs):
        when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if index == 0:
            out.append("### Last run: %s (%s, %.0f KB of log)"
                       % (tool, when, size / 1024.0))
            out.append("")
            out.append("```")
            out.extend(redacted.get(tool, [])[-URL_TAIL_LINES:])
            out.append("```")
            out.append("")
        else:
            out.append("- %s: last ran %s (%.0f KB of log)" % (tool, when, size / 1024.0))
    return out


# The pre-filled URL has to survive percent-encoding, so the BODY budget is well
# under `gui.common._MAX_ISSUE_URL` (7800). Escaping a body of mostly ASCII inflates
# it by roughly a third, and the rest of the URL (host, path, title) is fixed
# overhead, so 4500 characters of body is the safe working figure.
MAX_BODY_CHARS = 4500


def _fit_budget(head, runs, settings, tail, limit=MAX_BODY_CHARS, relevant=None):
    """Assemble the body in priority order, dropping the cheapest parts first.

    The order is the order of what a report would have SETTLED: the environment and
    the last run's own output are what answer "the output folder is empty", so they
    are never sacrificed. The settings block is large, repetitive and fully present
    in the attached zip, so it is what shrinks. It shrinks in two steps rather than
    one, because "the settings for the tool that ran" is most of the value of the
    whole block at a fraction of its size. The legend must survive to the end:
    without it the tokens in the text above mean nothing.
    """
    def size(parts):
        return sum(len(p) + 1 for p in parts)

    fixed = size(head) + size(runs) + size(tail)
    for block in (settings, relevant or []):
        if block and fixed + size(block) <= limit:
            return "\n".join(head + runs + block + tail) + "\n"

    note = ["", "*Settings omitted here to fit the URL: they are in the attached"
            " zip, in full.*"]
    if fixed + size(note) <= limit:
        return "\n".join(head + runs + note + tail) + "\n"

    # Still too big: the run tail is the only remaining large block, so trim it from
    # the TOP, keeping the end of the log, which is where a run says how it finished.
    trimmed = list(runs)
    while trimmed and size(head) + size(trimmed) + size(note) + size(tail) > limit:
        trimmed.pop(1) if len(trimmed) > 1 else trimmed.pop()
    return "\n".join(head + trimmed + note + tail) + "\n"


def build_report(cfg=None, app_root=None, collapse_re=None, zip_name=None,
                 redactor=None, extra_logs=()):
    """Build the whole report. Fail-safe: any section that cannot be gathered is
    reported as unavailable rather than taking the report down with it."""
    app_root = app_root or APP_ROOT
    red = redactor if redactor is not None else make_redactor(cfg)
    report = Report()

    logs = newest_logs(os.path.join(app_root, LOGS_DIRNAME))

    # THE ZIP'S LOGS ARE REDACTED FIRST, before the body is assembled, and the
    # ordering is load-bearing rather than stylistic. The counts below ("N lines
    # withheld") and the token legend are read off the redactor, so computing them
    # before the bulk of the work has gone through it reports ZERO withheld lines on
    # a report whose zip holds thousands. Measured: a real report said 0 while its
    # tag log had 5,716 descriptions removed, which is precisely the number the user
    # needs to be able to trust.
    zip_logs, redacted_by_tool = [], {}
    for tool, path, _mtime in logs:
        name = "logs/%s.log" % re.sub(r"[^a-z0-9]+", "-", tool.lower()).strip("-")
        strict = tool in STRICT_PATH_TOOLS
        lines = red.lines(tail_lines(path, ZIP_TAIL_LINES, collapse_re),
                          strict=strict)
        redacted_by_tool[tool] = lines
        zip_logs.append((name, "\n".join(lines)))

    head = ["**What happened?**", "", "", "**Steps to reproduce:**", "", ""]
    if zip_name:
        head += [
            "> Please drag **`%s`** into this box to attach it." % zip_name,
            "> The folder is already open on your screen, and the file contains the"
            " redacted logs for this report.",
            "",
        ]
    head += ["---", "", "*Diagnostics (auto-filled, please keep):*", ""]

    env = _environment_lines(app_root)
    runs = _run_lines(logs, redacted_by_tool)
    settings = _settings_lines(red, app_root)
    last_tool = logs[0][0] if logs else None
    relevant = _settings_lines(
        red, app_root, TOOL_CONFIG_SECTIONS.get(last_tool),
        heading="*Settings for %s (the rest are in the attached zip):*" % last_tool
    ) if last_tool in TOOL_CONFIG_SECTIONS else []

    # The legend is read AFTER everything above, because it reports only the roots
    # that actually matched.
    tail = ["", "*Folder names and file names are replaced with hashes. Tokens:*", ""]
    tail += red.legend()
    if red.dropped:
        tail += ["", "*%d log line(s) were removed entirely: they held a path this"
                 " install could not recognise, and the rule is to drop rather than"
                 " guess.*" % red.dropped]
    if red.withheld:
        tail += ["", "*%d line(s) carrying the vision model's own description of a"
                 " photo, and the file name generated from it, were removed from the"
                 " attached logs entirely. They describe the CONTENT of private"
                 " pictures and are never reported.*" % red.withheld]

    # The zip always carries everything; the URL body is what has to fit.
    report.full = "\n".join(head + env + [""] + runs + settings + tail) + "\n"
    report.body = _fit_budget(head + env + [""], runs, settings, tail,
                              relevant=relevant)
    report.legend = red.legend()
    report.dropped = red.dropped
    report.withheld = red.withheld
    report.mapping = dict(red.mapping)
    report.salt = red.salt

    report.files.append(("report.md", report.body))
    report.files.extend(zip_logs)
    # The debug trail, the newest crash log, and whatever the caller knows is more
    # relevant than either (the Benchmark window points at its own log). These are
    # ATTACHED rather than pointed at: the old flow asked the user to attach a file
    # and users do not attach files, which is half of what this feature is for.
    extras = [(os.path.join(app_root, LOGS_DIRNAME, "debug.log"), "debug"),
              (_newest_crash_log(app_root), "crash")]
    extras += [(p, l) for l, p in (extra_logs or ())]
    for extra, label in extras:
        if extra and os.path.isfile(extra):
            body = "\n".join(
                red.lines(tail_lines(extra, ZIP_TAIL_LINES, collapse_re)))
            report.files.append(("logs/%s.log" % label, body))
    return report


def _newest_crash_log(app_root=None):
    """The newest crash log, which is the one thing a crashed run leaves behind."""
    try:
        log_dir = os.path.join(app_root or APP_ROOT, LOGS_DIRNAME)
        crashes = sorted(f for f in os.listdir(log_dir)
                         if f.startswith("crash_") and f.endswith(".log"))
        return os.path.join(log_dir, crashes[-1]) if crashes else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  WRITING IT OUT
# ─────────────────────────────────────────────────────────────

def report_name(now=None):
    """`imgtbx-diag-<stamp>.zip`. Stamped, not numbered, so two reports from the same
    session sort correctly and neither overwrites the other."""
    now = now or datetime.datetime.now()
    return "imgtbx-diag-%s.zip" % now.strftime("%Y%m%d-%H%M%S")


def write_zip(report, issues_dir=None, name=None):
    """Write the report's files to ./issues/<name>. Returns the path.

    ONE invariant guards this folder: it holds redacted zips and nothing else, ever.
    It is what the app opens in Explorer for the user to drag from, so an
    un-redacted file sitting beside the zip is a drag-and-drop accident waiting to
    happen. The hash map goes to logs/ (see `write_mapping`).
    """
    issues_dir = issues_dir or os.path.join(APP_ROOT, ISSUES_DIRNAME)
    os.makedirs(issues_dir, exist_ok=True)
    path = os.path.join(issues_dir, name or report_name())
    total = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, text in report.files:
            data = (text or "").encode("utf-8", "replace")
            if total + len(data) > MAX_ZIP_BYTES:
                zf.writestr(arcname + ".truncated.txt",
                            "Omitted: the report reached its %d MB budget."
                            % (MAX_ZIP_BYTES // (1024 * 1024)))
                continue
            total += len(data)
            zf.writestr(arcname, data)
    return path


def write_mapping(report, logs_dir=None, stamp=None):
    """Write the hash -> real name map, for the user's eyes only.

    The hash is one-way for the USER too, so without this nobody can answer a
    maintainer's "what is 7c2e.jpg?". It is the private half of the report, so it
    goes to logs/, NEVER to ./issues and never into the zip.
    """
    if not report.mapping:
        return None
    logs_dir = logs_dir or os.path.join(APP_ROOT, LOGS_DIRNAME)
    try:
        os.makedirs(logs_dir, exist_ok=True)
        stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(logs_dir, "diagmap_%s.txt" % stamp)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Hash -> real name, for the diagnostics report of %s.\n" % stamp)
            fh.write("This file is PRIVATE. It is not in the zip and must not be "
                     "attached to an issue.\n\n")
            for redacted, original in sorted(report.mapping.items()):
                fh.write("%-40s %s\n" % (redacted, original))
        return path
    except Exception as exc:
        debug_log("diagnostics: could not write the hash map", exc)
        return None


def _prune(folder, prefix, suffix, keep):
    """Keep the newest `keep` files matching prefix/suffix. Fail-safe."""
    try:
        found = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.startswith(prefix) and f.endswith(suffix)]
        found.sort(key=os.path.getmtime, reverse=True)
        for old in found[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return len(found[keep:])
    except Exception:
        return 0


def prune_reports(issues_dir=None, logs_dir=None, keep=KEEP_REPORTS):
    """Keep the newest `keep` zips, and rather more of the maps.

    Without this both folders grow forever and nobody ever looks in either. The maps
    are kept longer than the zips they belong to, deliberately: a map is a couple of
    kilobytes, and it stays useful for as long as the ISSUE is open, which is long
    after the local zip has stopped mattering. Throwing it away early would leave a
    user unable to answer a question about a report they already sent.
    """
    issues_dir = issues_dir or os.path.join(APP_ROOT, ISSUES_DIRNAME)
    logs_dir = logs_dir or os.path.join(APP_ROOT, LOGS_DIRNAME)
    removed = _prune(issues_dir, "imgtbx-diag-", ".zip", keep)
    _prune(logs_dir, "diagmap_", ".txt", keep * 3)
    return removed

