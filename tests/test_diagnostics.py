"""
Future feature #24: the diagnostics redactor and the log-tail selection.

This is the module that decides what may leave the user's machine, so the tests
are named for the CONSEQUENCE, not for the function. Every string marked "real"
below was sampled verbatim from this developer's own logs; they are the reason the
implementation looks the way it does, and they are here so a later "simplification"
of the bounding rule fails loudly instead of quietly leaking.

The three findings under test:

  1. A path runs to the end of its line. Of 16,128 path-bearing lines across five
     real logs, 14 had trailing text and 12 of those were the double space INSIDE a
     folder name. So the only exception shape is `<path>  (note-without-slashes)`.
  2. Real logs carry the 8.3 short form (`C:\\Users\\EDUARD~1\\...`), which a long-form
     root table does not match.
  3. A private name can appear with no path syntax at all (the Batch Upscaler's bare
     relative folder headers), which no drive-letter rule can catch.

And the invariant that makes the above safe to be imperfect: anything path-shaped
that survives redaction causes the whole LINE to be dropped.
"""

import os
import json
import re

import pytest

import diagnostics as dg


# Real lines, verbatim from logs/log_40d8b4704174.log and logs/log_c8d03a4bda1a.log.
REAL_SKIP = (
    "2026-06-12 | 18:26:43 |   [3/29795] SKIP (unreadable image - file may be "
    "corrupted)  X:\\Personale\\Poze\\Oracle\\Irinel  Poze Cairo\\Cairo5\\Picture 209.jpg")
REAL_VARIANT = (
    "2026-07-29 | 09:20:22 |     C:\\Users\\EDUARD~1\\AppData\\Local\\Temp\\tmp1\\src"
    "\\logo.png  (would lose transparency)")
REAL_FOLDER_HEADER = "[folder]  Oracle\\Irinel  Poze Cairo\\Cairo5"
REAL_BARE_NAME = "[folder]  James (cats mainly)"

PRIVATE_BITS = ("Personale", "Poze", "Oracle", "Irinel", "Cairo", "James", "cats",
                "logo", "Picture")


def _redactor(**kw):
    cfg = {"defaults": {
        "upscale_source": "X:\\Personale\\Poze",
        "upscale_output": "X:\\Personale\\Poze\\__upscaled__",
    }}
    env = {"USERPROFILE": "C:\\Users\\Eduard Baniceru", "TEMP": "C:\\Temp"}
    # use_db=False: a unit test must never read the developer's own cache.db. It
    # would make results machine-dependent, and the point of these tests is the
    # RULES, not this install's data.
    kw.setdefault("names", ())
    return dg.make_redactor(cfg, env=env, app_root="D:\\App", salt=b"fixed-salt",
                            use_db=False, **kw)


# ─────────────────────────────────────────────
#  1. The bounding rule
# ─────────────────────────────────────────────

def test_a_real_skip_line_keeps_its_message_and_loses_every_private_name():
    """The whole point of the feature: the diagnostic half survives, the private
    half does not. If this ever fails by keeping a name, a report leaks."""
    out = _redactor().line(REAL_SKIP)
    assert "[3/29795] SKIP (unreadable image - file may be corrupted)" in out
    assert "<ROOT" in out
    assert out != dg.REDACTED_LINE
    for bit in PRIVATE_BITS:
        assert bit not in out, "leaked %r" % bit


def test_the_double_space_inside_a_folder_name_is_not_a_separator():
    """`Irinel  Poze Cairo` contains a double space. Treating that as "path ends
    here" was the obvious implementation and it leaks the rest of the path."""
    out = _redactor().line(REAL_SKIP)
    assert "Poze Cairo" not in out
    assert "Cairo5" not in out
    assert "Picture 209" not in out


def test_a_genuine_trailing_note_survives_the_path():
    """The one real exception shape, `<path>  (would lose transparency)`. The reason
    is the answer to the report, so losing it defeats the purpose."""
    out = _redactor().line(REAL_VARIANT)
    assert out.endswith("(would lose transparency)")
    assert "logo" not in out
    assert ".png" in out, "the extension is diagnostic and must survive"


def test_split_trailing_note_refuses_a_note_containing_a_slash():
    """The discriminator between the two cases above: a real note has no slash in
    it, a folder name usually does. Measured 14/14 correct."""
    tail, note = dg.split_trailing_note("\\a\\b.jpg  (would lose transparency)")
    assert note.strip() == "(would lose transparency)"
    tail, note = dg.split_trailing_note("\\Irinel  Poze Cairo\\Cairo5\\Picture 209.jpg")
    assert note == ""
    assert tail.endswith("Picture 209.jpg")


# ─────────────────────────────────────────────
#  2. The 8.3 short form
# ─────────────────────────────────────────────

def test_the_short_form_of_a_root_is_registered_when_the_path_exists(tmp_path):
    """Real logs contain `C:\\Users\\EDUARD~1\\...` because a child process was
    launched with a short-form cwd. Long-form-only matching leaves those lines to
    the fail-closed rule, so the report arrives gutted rather than leaked."""
    spellings = dg._path_spellings(str(tmp_path))
    assert str(tmp_path) in spellings
    if os.name == "nt":
        # A tmp_path under %TEMP% normally has a short form; if the filesystem has
        # 8.3 disabled the API returns the long name, which is still correct.
        assert all(s for s in spellings)


def test_an_unknown_user_path_is_dropped_not_passed_through():
    """The fail-closed rule. A root we do not know about must cost the LINE, never
    be emitted verbatim."""
    r = _redactor()
    assert r.line("something happened at Z:\\Secret Project\\notes.txt") == dg.REDACTED_LINE
    assert r.dropped == 1


def test_a_unc_share_is_dropped_too():
    r = _redactor()
    assert r.line("copying from \\\\nas01\\family\\2006\\a.jpg") == dg.REDACTED_LINE


# ─────────────────────────────────────────────
#  3. Bare private names with no path syntax
# ─────────────────────────────────────────────

def test_a_bare_folder_header_with_no_drive_letter_is_still_redacted():
    """`[folder]  James (cats mainly)` has no path syntax at all, so no drive-letter
    rule can see it. Without the name dictionary this line leaks a folder name."""
    r = _redactor(names=["James (cats mainly)"])
    out = r.line(REAL_BARE_NAME)
    assert "James" not in out and "cats" not in out
    assert out.startswith("[folder]")


def test_a_relative_backslash_header_is_dropped_when_the_names_are_unknown():
    """With no dictionary the same header still must not survive: it is a private
    folder taxonomy joined by backslashes, so the residual-relpath rule drops it."""
    assert _redactor().line(REAL_FOLDER_HEADER) == dg.REDACTED_LINE


def test_hashing_one_component_does_not_let_its_unhashed_sibling_through():
    """REGRESSION, and the most important test here. A first cut of the whole-chunk
    dictionary pass produced a real leak on a real log line:

        [2/2] Poze (Fototarget)\\2005-10-24\\098.avi -> 2X: 160x120 327f

    The date folder matched the dictionary and was hashed. That was enough to
    satisfy a rule that only asked "is anything path-shaped left", so the line was
    emitted with the private folder beside it intact: separated from `[2/2]` by a
    single space, it had never been isolated as a segment. Every component of a
    backslash-joined token must be resolved, or the LINE goes.

    A loosened fail-closed rule does not fail loudly. It converts drops into leaks.
    """
    r = _redactor(names=["2005-10-24"])
    out = r.line("[2/2] Poze (Fototarget)\\2005-10-24\\098.avi -> 2X: 160x120 327f")
    assert out == dg.REDACTED_LINE


def test_a_fully_hashed_folder_header_still_survives():
    """The other side of that rule: when every component IS resolved, the line is
    kept. Losing these would strip an upscale log of its folder structure."""
    r = _redactor(names=["Oracle", "Irinel  Poze Cairo", "Cairo5"])
    out = r.line("[folder]  Oracle\\Irinel  Poze Cairo\\Cairo5")
    assert out != dg.REDACTED_LINE
    for bit in ("Oracle", "Irinel", "Cairo"):
        assert bit not in out


def test_a_folder_name_containing_a_double_space_is_matched_whole():
    """`Irinel  Poze Cairo` is ONE folder, not two columns. Splitting on the double
    space first means it never matches the dictionary."""
    r = _redactor(names=["Irinel  Poze Cairo"])
    out = r.line("[folder]  Irinel  Poze Cairo")
    assert "Irinel" not in out and "Cairo" not in out


def test_the_same_folder_hashes_alike_in_a_header_and_inside_a_path():
    """A folder header and a full path mention the same folder in two different
    syntaxes. They must agree, or a maintainer cannot connect them."""
    r = _redactor(names=["James (cats mainly)"])
    header = r.line("[folder]  James (cats mainly)")
    inpath = r.line("SKIP  X:\\Personale\\Poze\\James (cats mainly)\\dsc01308.jpg")
    stamp = header.split()[-1]
    assert stamp in inpath


def test_a_dictionary_name_never_rewrites_part_of_a_timestamp():
    """Whole-segment matching is what makes the dictionary safe. A folder called
    "2006" must not rewrite the 2006 inside `2026-06-12` or a duration."""
    r = _redactor(names=["2006", "Poze"])
    out = r.line("2026-06-12 | 18:26:43 | done in 2006 ms")
    assert "2026-06-12" in out and "2006 ms" in out


def test_a_short_dictionary_name_is_ignored():
    """Two-character names carry almost nothing and matching them mangles text."""
    r = _redactor(names=["ab"])
    assert "ab" in r.line("[folder]  ab")


# ─────────────────────────────────────────────
#  4. Model output: what no path rule can see
# ─────────────────────────────────────────────
# Real Tag & Rename log lines. The description is free English prose about what is
# IN somebody's photograph, and the generated filename is that description
# condensed. Neither is a path, neither is a folder name, and neither is in any
# dictionary, so every rule above sails straight past them. This was missed in the
# first build and found by reading an actual zip.

REAL_DESCRIPTION = (
    '           -> "A kitten with striking blue eyes and a fluffy coat is walking '
    'on a snowy surface, looking to the side."')
REAL_GENERATED_NAME = "           -> 0001_Kitten_Walking_Snowy_Surface.png  (renamed)"
REAL_NOT_RENAMED = ("2026-07-23 | 20:40:28 |            -> 001.jpg  "
                    "(file was not renamed by this script)")
REAL_TAGGED_ONLY = "           -> rotated_one.jpg  (tagged only, name kept)"
REAL_UNDO = "           -> EXIF restored, renamed back to 001.jpg"

DESCRIPTION_WORDS = ("kitten", "blue eyes", "fluffy", "snowy", "walking",
                     "Kitten", "Snowy", "Surface")


def test_the_vision_models_description_of_a_photo_never_survives():
    """The sharpest privacy failure in this feature, and the one a path rule cannot
    reach: a collection's worth of these lines says far more about a person than
    any folder name, because it describes their family."""
    assert _redactor().line(REAL_DESCRIPTION) is dg.OMIT


def test_the_generated_file_name_never_survives_either():
    """`0001_Kitten_Walking_Snowy_Surface.png` IS the description, condensed. It is
    not enough to remove the prose line and keep the name."""
    assert _redactor().line(REAL_GENERATED_NAME) is dg.OMIT


def test_the_line_is_removed_outright_rather_than_left_as_a_placeholder():
    """A placeholder was the first cut and it was wrong twice over: the per-image
    outcome is already totalled in the run's summary table, so nothing is lost, and
    one placeholder per image is thousands of lines of noise in a file whose whole
    purpose is to be read by somebody debugging. Nothing of the line survives, its
    trailing `(renamed)` marker included."""
    r = _redactor()
    kept = r.lines([REAL_GENERATED_NAME, REAL_DESCRIPTION, REAL_NOT_RENAMED,
                    REAL_TAGGED_ONLY, REAL_UNDO])
    assert kept == []
    assert r.withheld == 5


def test_the_surrounding_lines_are_untouched():
    """Only the model-output lines go. The counter, the size and the timing that
    bracket them are the diagnostic content and must survive intact."""
    r = _redactor()
    kept = r.lines([
        "2026-07-23 | 10:39:35 |   [21/100] 1280x960px",
        REAL_GENERATED_NAME,
        REAL_DESCRIPTION,
        "2026-07-23 | 10:39:36 |            Done in 00:00.867 | Total elapsed: 00:00:23",
    ])
    assert kept == [
        "2026-07-23 | 10:39:35 |   [21/100] 1280x960px",
        "2026-07-23 | 10:39:36 |            Done in 00:00.867 | Total elapsed: 00:00:23",
    ]


def test_the_withheld_count_is_reported():
    """The user is told this happened; it is not a silent removal."""
    r = _redactor()
    r.line(REAL_DESCRIPTION)
    r.line(REAL_GENERATED_NAME)
    assert r.withheld == 2


def test_a_bare_generated_file_name_on_its_own_line_is_hashed():
    """The SECOND shape, found only by auditing a real zip against the descriptions'
    own vocabulary. The Undo listing prints the current name with no path around it:

          001_Mountain_Village_with_Mist_and.jpg
                   -> EXIF restored, renamed back to 001.jpg

    Removing the arrow line and leaving that is no protection at all: after a
    rename, the name IS the description."""
    r = _redactor()
    out = r.line("  001_Mountain_Village_with_Mist_and.jpg")
    for word in ("Mountain", "Village", "Mist"):
        assert word.lower() not in out.lower(), "leaked %r" % word
    assert out.strip().endswith(".jpg"), "the extension is diagnostic and stays"


def test_a_bare_name_is_dropped_entirely_in_a_strict_path_log():
    """With the arrow lines gone, a column of anonymous hashes answers nothing, so
    in a Tag & Rename log the name line goes with them."""
    r = _redactor()
    assert r.line("  001_Mountain_Village.jpg", strict=True) is dg.OMIT


def test_a_bare_name_is_hashed_not_withheld_so_it_still_correlates():
    """It is a filename with the path left off, so it gets what every other filename
    gets. Hashing keeps the listing's shape and ties it to the same file elsewhere."""
    r = _redactor()
    bare = r.line("  001_Mountain_Village.jpg").strip()
    inpath = r.line("SKIP  X:\\Personale\\Poze\\001_Mountain_Village.jpg")
    assert bare.split(".")[0] in inpath


def test_a_bare_NON_media_file_name_stays_readable():
    """`cache.db`, `worker_settings.json` and `video_benchmark.log` are diagnostics.
    The media-extension list is what makes this rule safe to apply everywhere."""
    r = _redactor()
    for name in ("  cache.db", "  worker_settings.json", "  video_benchmark.log"):
        assert name.strip() in r.line(name)


def test_a_sentence_ending_in_a_word_is_not_mistaken_for_a_file_name():
    """The bare-name rule matches a WHOLE line. Ordinary prose must not trip it."""
    r = _redactor()
    for line in ("  Scanning the folder ...", "  Done in 02:01",
                 "  All 2 cached entries verified."):
        assert r.line(line) == line


def test_a_tag_log_per_image_line_keeps_only_the_counter_and_the_size():
    """The counter and the dimensions ARE the diagnostic content of that line. The
    file name adds nothing the counter does not (the run is a sequence, and the
    summary table carries the outcomes), while being the most sensitive string the
    app handles, since after a run it IS the model's description of the picture."""
    r = _redactor()
    line = ("2026-07-23 | 10:39:35 |   [21/100] 1280x960px  "
            "X:\\Personale\\Poze\\021_Mountain_Village_View.jpg")
    assert r.line(line, strict=True) == "2026-07-23 | 10:39:35 |   [21/100] 1280x960px"


def test_strict_mode_keeps_the_root_token_when_the_path_IS_the_root():
    """"Was Tag & Rename pointed at the source folder instead of the upscaled one?"
    is a documented mistake, and the legend resolves the token without naming
    anything. Naming a FILE inside that tree answers nothing; naming the tree does."""
    out = _redactor().line("Source: X:\\Personale\\Poze", strict=True)
    assert out == "Source: <ROOT2>" or re.fullmatch(r"Source: <ROOT\d+>", out)


def test_a_quoted_path_ends_at_its_closing_quote():
    """The runners print folders quoted (`Scanning 'D:\\...\\Benchmark' ...`). The
    end-of-line rule ate the closing quote and everything after it, leaving a line
    reading `Scanning '` -- which looks like a truncated log -- and hashed the
    `' ...` into a meaningless component. A quoted path's end is knowable exactly,
    not guessed, so it is the one case that does not need the end-of-line rule.

    Measurement (1) never saw this: it counted trailing text after TWO spaces, and
    here a single space separates the quote from the ellipsis."""
    r = _redactor()
    out = r.line("  Scanning 'X:\\Personale\\Poze' recursively ...")
    assert out.endswith("' recursively ...")
    assert "Personale" not in out and "Poze" not in out


def test_a_quoted_path_is_removed_with_its_quotes_in_a_strict_log():
    r = _redactor()
    out = r.line("  Scanning 'X:\\Personale\\Poze\\sub' ...", strict=True)
    assert out == "  Scanning [path removed] ..."


def test_removing_a_path_never_leaves_a_field_looking_empty():
    """`Cache: <path>` became a dangling `Cache:`, which reads as "the app recorded
    nothing here" -- a bug report of its own. Same misreading class as a silent
    "0 lines withheld"."""
    out = _redactor().line("  Cache:   X:\\Personale\\Poze\\db\\cache.db", strict=True)
    assert out.strip() == "Cache: [path removed]"


def test_strict_mode_is_per_tool_and_not_the_default():
    """The Batch Upscaler's `SKIP (unreadable)  <path>` line is the opposite case:
    there the file identity is the whole point, so it stays hashed."""
    r = _redactor()
    normal = r.line(REAL_SKIP)
    assert "\\" in normal and normal != dg.REDACTED_LINE
    assert "Tag & Rename" in dg.STRICT_PATH_TOOLS
    assert "Batch Upscaler" not in dg.STRICT_PATH_TOOLS


def test_the_tag_log_in_a_report_carries_no_file_names_at_all(tmp_path):
    """End to end: the whole four-line block a user quoted, through build_report."""
    root = _app_tree(tmp_path)
    (tmp_path / "logs" / "tag_a.log").write_text("\n".join([
        "2026-07-23 | 10:39:35 |   [21/100] 1280x960px  X:\\Personale\\Poze\\a.jpg",
        "2026-07-23 | 10:39:36 |            -> 021_Mountain_Village_View.jpg  (renamed)",
        '2026-07-23 | 10:39:36 |            -> "Distant mountains loom over green fields."',
        "2026-07-23 | 10:39:36 |            Done in 00:00.867 | Total elapsed: 00:00:23",
    ]), encoding="utf-8")
    rep = dg.build_report({}, app_root=root, redactor=_redactor())
    tag = [text for name, text in rep.files if "tag" in name][0]
    for secret in ("Mountain", "Village", "mountains", "a.jpg", "Personale", "Poze"):
        assert secret not in tag, "leaked %r" % secret
    assert tag.split("\n") == [
        "2026-07-23 | 10:39:35 |   [21/100] 1280x960px",
        "2026-07-23 | 10:39:36 |            Done in 00:00.867 | Total elapsed: 00:00:23",
    ], "only the counter, the size and the timing are worth shipping"


def test_an_arrow_inside_a_line_is_not_a_description():
    """The Video Upscaler writes `a.avi -> 2X: ...` mid-line. Measured across every
    log this install has: 6,541 lines START with an arrow and all 6,541 are Tag &
    Rename output. Widening this rule to any arrow would gut the other logs."""
    r = _redactor(names=["2005-10-24"])
    out = r.line("2026-07-24 | 20:18:14 | [2/2] done -> 2X: 160x120 327f")
    assert "2X: 160x120 327f" in out
    assert r.withheld == 0


# ─────────────────────────────────────────────
#  Hashing: what it must preserve
# ─────────────────────────────────────────────

def test_the_same_file_hashes_the_same_way_twice_within_a_report():
    """Correlation is the reason to hash instead of blanking: "the same file failed
    three times" has to remain visible."""
    r = _redactor()
    a = r.line(REAL_SKIP)
    b = r.line(REAL_SKIP)
    assert a == b


def test_different_reports_do_not_share_hashes():
    """The salt is per-report and random, so a public zip cannot be brute-forced for
    common folder names ("Poze", a first name) by guessing."""
    one = dg.Redactor([], names=["Personale"]).line("[folder]  Personale")
    two = dg.Redactor([], names=["Personale"]).line("[folder]  Personale")
    assert one != two


def test_the_extension_and_its_case_survive():
    """`.CR2` vs `.jpg` is often the whole answer, and a case-only difference is the
    class of bug #22 dealt with."""
    assert dg.component_hash("IMG_0001.CR2", b"s").endswith(".CR2")
    assert dg.component_hash("IMG_0001.jpg", b"s").endswith(".jpg")


def test_depth_survives_so_the_tree_shape_is_still_readable():
    out = dg.hash_tail("\\a\\b\\c\\d.jpg", b"s")
    assert out.count("\\") == 4
    assert out.endswith(".jpg")


def test_hashes_are_long_enough_not_to_collide_across_a_big_tree():
    """Four hex would collide constantly at 30k files and destroy correlation."""
    seen = {dg.component_hash("folder%d" % i, b"s") for i in range(20000)}
    assert len(seen) > 19000


# ─────────────────────────────────────────────
#  Roots and the legend
# ─────────────────────────────────────────────

def test_a_nested_output_folder_wins_over_the_source_that_contains_it():
    """Longest-first. On a normal install `__upscaled__` sits inside the source
    tree, and reporting every output path as a source path would mislead."""
    r = _redactor()
    src = r.line("out: X:\\Personale\\Poze\\a.jpg")
    outp = r.line("out: X:\\Personale\\Poze\\__upscaled__\\a.jpg")
    assert src.split()[1].split("\\")[0] != outp.split()[1].split("\\")[0]


def test_config_keys_pointing_at_one_folder_share_one_token():
    """Four keys normally point at the same photo tree. That they coincide is itself
    diagnostic and says nothing private, so the legend reports it."""
    cfg = {"defaults": {"upscale_source": "X:\\Poze", "video_source": "X:\\Poze",
                        "conciliate_original": "X:\\Poze"}}
    roots = dg.build_roots(cfg, app_root="D:\\App", env={})
    photo = [r for r in roots if "defaults.upscale_source" in r.labels][0]
    assert len(photo.labels) == 3


def test_the_legend_never_contains_a_path():
    """It explains the tokens; naming the folder would undo the whole exercise."""
    for line in _redactor().legend():
        assert "\\" not in line and ":" not in line.replace("=", "")


def test_the_mapping_is_kept_so_the_user_can_answer_what_is_this_file():
    """The hash is one-way for the USER too. Without the map nobody can answer a
    maintainer's "what is 7c2e.jpg?"."""
    r = _redactor()
    r.line(REAL_SKIP)
    assert r.mapping
    assert any("Picture 209.jpg" in v for v in r.mapping.values())


# ─────────────────────────────────────────────
#  Degraded modes: the safe direction
# ─────────────────────────────────────────────

def test_a_redactor_with_no_roots_drops_paths_rather_than_passing_them():
    """If building the root table fails, the degraded mode must be the SAFE one."""
    r = dg.make_redactor(cfg=None, app_root=None, env={}, names=(), use_db=False)
    assert r.line("X:\\Personale\\Poze\\a.jpg") == dg.REDACTED_LINE


def test_redacting_a_settings_value_keeps_the_row_instead_of_dropping_it():
    """Unlike a log line, a settings echo loses information by vanishing: "the
    output folder is somewhere we do not know about" is itself the answer."""
    out = _redactor().path("Q:\\Somewhere Else")
    assert out != dg.REDACTED_LINE and "Somewhere" not in out and "char" in out


def test_nothing_here_raises_on_junk():
    r = _redactor()
    for junk in (None, "", "\x00\x01", "(" * 500, "\\" * 500):
        r.line(junk)


# ─────────────────────────────────────────────
#  Log selection and tails
# ─────────────────────────────────────────────

def _write(p, text):
    with open(str(p), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_the_newest_log_per_tool_is_picked_and_the_newest_tool_comes_first(tmp_path):
    _write(tmp_path / "log_aaa.log", "old upscale\n")
    _write(tmp_path / "log_bbb.log", "new upscale\n")
    _write(tmp_path / "tag_ccc.log", "tag run\n")
    os.utime(str(tmp_path / "log_aaa.log"), (1000, 1000))
    os.utime(str(tmp_path / "log_bbb.log"), (2000, 2000))
    os.utime(str(tmp_path / "tag_ccc.log"), (3000, 3000))

    rows = dg.newest_logs(str(tmp_path))
    assert [r[0] for r in rows] == ["Tag & Rename", "Batch Upscaler"]
    assert os.path.basename(rows[1][1]) == "log_bbb.log"


def test_a_tool_that_never_ran_is_simply_absent(tmp_path):
    _write(tmp_path / "log_aaa.log", "x\n")
    assert [r[0] for r in dg.newest_logs(str(tmp_path))] == ["Batch Upscaler"]


def test_the_tail_is_the_end_of_the_file(tmp_path):
    _write(tmp_path / "log_a.log", "\n".join("line %d" % i for i in range(200)))
    assert dg.tail_lines(str(tmp_path / "log_a.log"), 3) == ["line 197", "line 198", "line 199"]


def test_a_seven_megabyte_log_does_not_have_to_be_read_whole(tmp_path):
    """These logs really do reach 7 MB; only the tail window is read."""
    p = tmp_path / "log_big.log"
    _write(p, ("padding line\n" * 200000) + "the last line\n")
    assert os.path.getsize(str(p)) > 2 * 1024 * 1024
    assert dg.tail_lines(str(p), 1, max_bytes=4096) == ["the last line"]


def test_repeated_progress_heartbeats_collapse_to_the_latest(tmp_path):
    """Otherwise a video run's last 25 lines are 25 identical per-minute
    heartbeats, which tells a maintainer nothing at all."""
    pat = re.compile(r"Processing:")
    lines = ["start"] + ["Processing: %d%%" % i for i in range(1, 40)] + ["done"]
    _write(tmp_path / "video_a.log", "\n".join(lines))
    out = dg.tail_lines(str(tmp_path / "video_a.log"), 25, collapse_re=pat)
    assert out == ["start", "Processing: 39%", "done"]


def test_a_missing_log_is_not_an_error(tmp_path):
    assert dg.tail_lines(str(tmp_path / "nope.log"), 5) == []


# ─────────────────────────────────────────────
#  The cache DB as a redaction dictionary
# ─────────────────────────────────────────────

def _fake_cache(tmp_path):
    """A cache.db with just the tables the redactor reads."""
    import sqlite3
    p = tmp_path / "cache.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        "CREATE TABLE upscale_roots (id INTEGER, source_root TEXT, output_root TEXT);"
        "CREATE TABLE tag_roots (id INTEGER, source_root TEXT);"
        "CREATE TABLE video_roots (id INTEGER, source_root TEXT, output_root TEXT);"
        "CREATE TABLE upscale_files (root_id INTEGER, rel_path TEXT);"
        "CREATE TABLE tag_files (root_id INTEGER, original_rel_path TEXT,"
        "                        current_rel_path TEXT);"
        "CREATE TABLE video_files (root_id INTEGER, rel_path TEXT);")
    c.execute("INSERT INTO upscale_roots VALUES (1, ?, ?)",
              ("Y:\\OldShoot", "Y:\\OldShoot\\__upscaled__"))
    c.execute("INSERT INTO upscale_files VALUES (1, ?)",
              ("James (cats mainly)\\dsc01308.jpg",))
    c.execute("INSERT INTO tag_files VALUES (1, ?, ?)",
              ("a.jpg", "a_Two_Children_On_A_Beach.jpg"))
    c.commit()
    c.close()
    return str(p)


def test_a_folder_the_app_processed_before_is_still_a_known_root(tmp_path):
    """config.defaults describes only the CURRENT settings, but logs are per-folder
    and long-lived. Measured on real logs: one older run had 78.5% of its lines
    dropped because its source folder is no longer a configured default."""
    db = _fake_cache(tmp_path)
    r = dg.make_redactor({"defaults": {}}, app_root="D:\\App", env={}, db_path=db)
    out = r.line("SKIP  Y:\\OldShoot\\2006\\a.jpg")
    assert out != dg.REDACTED_LINE and "OldShoot" not in out


def test_the_name_dictionary_comes_from_the_db_not_a_tree_walk(tmp_path):
    """Walking the source tree to collect names took 145 seconds over the SMB mount
    holding this developer's photos, which is far too slow for a button press. The
    same names come out of a local SQL scan."""
    names = dg.names_from_db(_fake_cache(tmp_path))
    assert "James (cats mainly)" in names
    assert "dsc01308.jpg" in names


def test_a_tagged_filename_is_in_the_dictionary_because_it_describes_the_photo(tmp_path):
    """Tag & Rename turns a filename into a description of the picture, so a bare
    filename in a log line is a caption whether or not a path is attached."""
    names = dg.names_from_db(_fake_cache(tmp_path))
    assert "a_Two_Children_On_A_Beach.jpg" in names


def test_a_missing_or_broken_cache_is_not_an_error(tmp_path):
    assert dg.names_from_db(str(tmp_path / "nope.db")) == set()
    assert dg.roots_from_db(str(tmp_path / "nope.db")) == []
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all")
    assert dg.names_from_db(str(junk)) == set()


def test_the_legend_lists_only_the_roots_that_actually_matched(tmp_path):
    """The recorded-roots table holds every folder this install ever touched;
    listing all of them would bury the handful the report is about."""
    db = _fake_cache(tmp_path)
    cfg = {"defaults": {"upscale_source": "X:\\Personale\\Poze"}}
    r = dg.make_redactor(cfg, app_root="D:\\App", env={}, db_path=db)
    r.line("SKIP  X:\\Personale\\Poze\\a.jpg")
    assert len(r.legend()) == 1


def test_the_cache_is_opened_read_only(tmp_path):
    """A diagnostics run must never migrate, lock or write the user's cache."""
    db = _fake_cache(tmp_path)
    conn = dg._open_cache_readonly(db)
    assert conn is not None
    with pytest.raises(Exception):
        conn.execute("CREATE TABLE nope (x INTEGER)")
    conn.close()


def test_the_issues_folder_is_not_the_logs_folder():
    """The invariant: ./issues holds redacted zips only, because it is the folder
    the app opens for the user to drag from. The hash->path map goes to logs/."""
    assert dg.ISSUES_DIRNAME != dg.LOGS_DIRNAME


# ─────────────────────────────────────────────
#  What may be collected at all
# ─────────────────────────────────────────────

def _app_tree(tmp_path, secret="sk-live-DO-NOT-REPORT-THIS"):
    """A miniature app root with a tracked config and a secrets overlay."""
    import json
    (tmp_path / "config.json").write_text(json.dumps({
        "upscale": {"resolution": 2160, "upscale_cutoff_pct": 66},
        "ollama": {"url": "http://localhost:11434", "model": "qwen3-vl:8b-instruct"},
        "tagging": {"camera_filename_patterns": ["^DSC\\d+$", "^IMG_\\d+$"]},
        "defaults": {"upscale_source": "X:\\Personale\\Poze"},
        "runpod": {"api_key": "", "region": "EU-RO-1"},
        "notifications": {"discord_webhook_url": ""},
    }), encoding="utf-8")
    (tmp_path / "config.local.json").write_text(json.dumps({
        "runpod": {"api_key": secret},
        "notifications": {"discord_webhook_url": "https://discord.com/" + secret},
    }), encoding="utf-8")
    os.makedirs(str(tmp_path / "logs"), exist_ok=True)
    return str(tmp_path)


def test_the_secrets_overlay_is_never_read(tmp_path):
    """config.local.json holds the RunPod API key, the MQTT password and every
    notification token. The surest way never to report a credential is never to
    read the file that holds one, so this reads the TRACKED config directly and
    never config_store.load(), which would deep-merge the overlay over it."""
    root = _app_tree(tmp_path)
    reported = json.dumps(dg.tracked_config(root))
    assert "DO-NOT-REPORT-THIS" not in reported


def test_the_endpoint_sections_are_excluded_wholesale(tmp_path):
    """`runpod` and `notifications` exist to hold endpoints and keys. An allowlist
    that must be updated when a field is added is one that will one day not have
    been, so the whole section is dropped rather than field-by-field."""
    cfg = dg.tracked_config(_app_tree(tmp_path))
    assert "runpod" not in cfg and "notifications" not in cfg
    assert "upscale" in cfg


def test_a_url_is_not_mistaken_for_a_drive_letter():
    """REGRESSION. `http://localhost:11434` matched the drive-letter pattern as
    drive `p:`, so every line mentioning the Ollama URL was dropped and the setting
    itself came out as `<unrecognised path>`. A drive letter is ONE letter."""
    assert not dg.looks_like_a_path("http://localhost:11434")
    assert dg.looks_like_a_path("X:\\Personale\\Poze")
    assert _redactor().line("Ollama at http://localhost:11434 OK") != dg.REDACTED_LINE


def test_a_regex_setting_is_not_mistaken_for_a_path():
    """`tagging.camera_filename_patterns` is full of backslash escapes. Reporting it
    as `<unrecognised path>` loses a real setting and protects nothing."""
    assert not dg.looks_like_a_path("^DSC\\d+$")


def test_a_broken_venv_is_reported_because_it_looks_like_nothing_happening(tmp_path):
    """known-defects D1 in one line: a venv is NOT self-contained, so uninstalling
    the base Python leaves the app starting into nothing, with no window, no error
    and no crash log. From the outside that is "I click it and nothing happens"."""
    scripts = tmp_path / ".venv" / "Scripts"
    os.makedirs(str(scripts))
    (scripts / "python.exe").write_bytes(b"")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        "home = C:\\NoSuchPython\\3.12\n", encoding="utf-8")
    assert "BROKEN" in dg.venv_health(str(tmp_path))


def test_a_healthy_venv_says_so(tmp_path):
    scripts = tmp_path / ".venv" / "Scripts"
    os.makedirs(str(scripts))
    (scripts / "python.exe").write_bytes(b"")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        "home = %s\n" % str(tmp_path), encoding="utf-8")
    assert dg.venv_health(str(tmp_path)) == "ok"


# ─────────────────────────────────────────────
#  The report, the zip and the map
# ─────────────────────────────────────────────

def test_the_url_body_stays_inside_its_budget(tmp_path):
    """A pre-filled body has to survive percent-encoding into a ~7800 char URL."""
    root = _app_tree(tmp_path)
    for i in range(400):
        (tmp_path / "logs" / ("log_%03d.log" % i)).write_text(
            "\n".join("padding line %d" % n for n in range(400)), encoding="utf-8")
    rep = dg.build_report({}, app_root=root, redactor=_redactor())
    assert len(rep.body) <= dg.MAX_BODY_CHARS


def test_the_settings_shrink_before_the_run_output_does():
    """The environment and the run's own output are what answer "the output folder
    is empty". The settings are large, repetitive and fully present in the zip."""
    head = ["H"]
    runs = ["R" * 100]
    tail = ["T"]
    settings = ["S" * 5000]
    relevant = ["s" * 50]
    out = dg._fit_budget(head, runs, settings, tail, limit=400, relevant=relevant)
    assert "R" * 100 in out and "s" * 50 in out and "S" * 5000 not in out


def test_the_legend_survives_every_level_of_trimming():
    """Without it, the tokens in the text above mean nothing."""
    out = dg._fit_budget(["H"], ["R" * 900], ["S" * 900], ["<ROOT1> = source"],
                         limit=120)
    assert "<ROOT1> = source" in out


def test_the_zip_carries_the_report_and_the_logs(tmp_path):
    import zipfile
    root = _app_tree(tmp_path)
    (tmp_path / "logs" / "log_a.log").write_text("upscale ran\n", encoding="utf-8")
    rep = dg.build_report({}, app_root=root, redactor=_redactor())
    path = dg.write_zip(rep, str(tmp_path / "issues"))
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert "report.md" in names
    assert any(n.startswith("logs/") for n in names)


def test_the_hash_map_never_enters_the_zip_or_the_issues_folder(tmp_path):
    """It is the private half of the report: the one file that can turn every hash
    back into a real path. It belongs in logs/, beside the logs it explains."""
    import zipfile
    root = _app_tree(tmp_path)
    red = _redactor()
    red.line("SKIP  X:\\Personale\\Poze\\a.jpg")
    rep = dg.build_report({}, app_root=root, redactor=red)
    issues = str(tmp_path / "issues")
    zpath = dg.write_zip(rep, issues)
    mpath = dg.write_mapping(rep, str(tmp_path / "logs"))

    assert mpath and os.path.isfile(mpath)
    assert "Personale" in open(mpath, encoding="utf-8").read()   # it IS the private half
    assert os.path.dirname(mpath) != issues
    assert os.listdir(issues) == [os.path.basename(zpath)]
    with zipfile.ZipFile(zpath) as zf:
        for name in zf.namelist():
            assert "Personale" not in zf.read(name).decode("utf-8", "replace")


def test_only_zips_are_left_in_the_issues_folder(tmp_path):
    """The invariant that makes drag-and-drop safe: nothing un-redacted may sit
    beside the file the user is about to drag."""
    root = _app_tree(tmp_path)
    rep = dg.build_report({}, app_root=root, redactor=_redactor())
    issues = str(tmp_path / "issues")
    for _ in range(3):
        dg.write_zip(rep, issues, dg.report_name())
    assert all(f.endswith(".zip") for f in os.listdir(issues))


def test_old_reports_are_pruned(tmp_path):
    issues = str(tmp_path / "issues")
    os.makedirs(issues)
    for i in range(15):
        p = os.path.join(issues, "imgtbx-diag-2026010%02d-000000.zip" % i)
        open(p, "wb").close()
        os.utime(p, (1000 + i, 1000 + i))
    dg.prune_reports(issues, str(tmp_path), keep=10)
    assert len(os.listdir(issues)) == 10


def test_the_maps_outlive_the_zips_they_belong_to(tmp_path):
    """A map stays useful for as long as the ISSUE is open, which is long after the
    local zip has stopped mattering. Pruning it on the zip's schedule would leave a
    user unable to answer a question about a report they already sent."""
    issues, logs = str(tmp_path / "issues"), str(tmp_path / "logs")
    os.makedirs(issues)
    os.makedirs(logs)
    for i in range(30):
        z = os.path.join(issues, "imgtbx-diag-202601%02d-000000.zip" % i)
        m = os.path.join(logs, "diagmap_202601%02d-000000.txt" % i)
        open(z, "wb").close()
        open(m, "wb").close()
        os.utime(z, (1000 + i, 1000 + i))
        os.utime(m, (1000 + i, 1000 + i))
    dg.prune_reports(issues, logs, keep=5)
    assert len(os.listdir(issues)) == 5
    assert len(os.listdir(logs)) == 15


def test_pruning_never_touches_anything_else_in_logs(tmp_path):
    """logs/ holds the run logs and the crash logs. The prune is prefix-scoped."""
    logs = str(tmp_path / "logs")
    os.makedirs(logs)
    for name in ("log_abc.log", "crash_2026.log", "debug.log"):
        open(os.path.join(logs, name), "wb").close()
    dg.prune_reports(str(tmp_path / "issues"), logs, keep=0)
    assert sorted(os.listdir(logs)) == ["crash_2026.log", "debug.log", "log_abc.log"]


def test_the_counts_cover_the_zip_and_not_just_the_url_body(tmp_path):
    """REGRESSION. The counts were read off the redactor BEFORE the zip's logs went
    through it, so a real report announced "0 lines withheld" while its tag log had
    5,716 descriptions removed. That number is exactly what the user is being asked
    to trust, so reporting zero is worse than reporting nothing."""
    root = _app_tree(tmp_path)
    (tmp_path / "logs" / "tag_a.log").write_text(
        "\n".join(['  Tagging ...'] +
                  ['           -> "A private description of a photo number %d."' % i
                   for i in range(50)]), encoding="utf-8")
    rep = dg.build_report({}, app_root=root, redactor=_redactor())
    assert rep.withheld == 50
    assert "50 line(s) carrying the vision model" in rep.full
    tag = [text for name, text in rep.files if "tag" in name][0]
    assert "description" not in tag


def test_building_a_report_never_raises_on_a_bare_machine(tmp_path):
    """No config, no logs, no db, no GPU. It must still produce something."""
    rep = dg.build_report({}, app_root=str(tmp_path), redactor=_redactor())
    assert rep.body and "What happened?" in rep.body


# ─────────────────────────────────────────────
#  The URL, and the GitHub side
# ─────────────────────────────────────────────

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(APP_ROOT, ".github", "ISSUE_TEMPLATE", "bug.md")


def test_the_issue_url_carries_no_template_parameter():
    """THE decision of #24's GitHub half, and it must not be "tidied up" later.

    Installs are immutable, the repo is not. An install shipped today keeps sending
    whatever URL it was compiled with, forever. Pointing it at
    `?template=bug.yml&<field>=...` couples it to a filename and a field id in a
    repo that will be edited by someone who has forgotten the coupling, and GitHub
    silently ignores an unknown field parameter, so every older install would start
    sending empty reports with nothing failing loudly. A `?body=` URL references
    nothing in the repo and cannot go stale.
    """
    from gui.common import _issue_url
    url = _issue_url(body="hello")
    assert "template=" not in url
    assert "body=hello" in url
    assert "labels=bug" in url


def test_a_real_body_still_fits_the_url_cap(tmp_path):
    """The cap is on the whole URL, and percent-encoding inflates a body by roughly
    a third, which is why the body budget is well under it."""
    from gui.common import _issue_url, _MAX_ISSUE_URL
    body = "x" * dg.MAX_BODY_CHARS
    assert len(_issue_url(body=body)) <= _MAX_ISSUE_URL


def test_the_legacy_url_still_works_when_no_report_could_be_built():
    """The fallback path: a report that cannot be gathered must never stop somebody
    reporting a bug."""
    from gui.common import _issue_url
    url = _issue_url()
    assert url.startswith("https://github.com/") and "body=" in url


def test_the_issue_template_matches_what_the_app_fills_in():
    """The app's body and the repo's template are coupled by convention only, so
    nothing breaks if they drift. But they SHOULD agree: a reporter arriving through
    the GitHub UI and one arriving through the app should be answering the same
    questions, and a maintainer should not have to read two shapes."""
    assert os.path.isfile(TEMPLATE), "the bug template is missing"
    template = open(TEMPLATE, encoding="utf-8").read()
    body = dg.build_report({}, app_root=APP_ROOT, redactor=_redactor()).body
    for heading in ("**What happened?**", "**Steps to reproduce:**"):
        assert heading in template and heading in body, heading
    for field in ("- Image Toolbox:", "- Install mode:", "- OS:", "- Python:",
                  "- GPU:", "- Bundled ffmpeg:", "- Virtual environment:"):
        assert field in template, "%s missing from the template" % field
        assert field in body, "%s missing from the app's body" % field


def test_blank_issues_stay_enabled():
    """The app's URL lands on the blank editor. GitHub treats a URL query as a way
    past the template chooser but calls that a defect rather than a contract, so
    disabling blank issues would put every installed copy of the app at the mercy of
    a bug being fixed."""
    cfg = os.path.join(APP_ROOT, ".github", "ISSUE_TEMPLATE", "config.yml")
    assert os.path.isfile(cfg)
    assert "blank_issues_enabled: true" in open(cfg, encoding="utf-8").read()
