"""
Tests for the notification SEVERITY contract (docs/notifications.md).

The bug: an alert's severity travels as a Discord embed colour int, and until
0.5.8 every runner wrote its own literal. The Video Upscaler used a different
palette (flat-UI `0xE74C3C` red, `0xF1C40F` amber) than the image runners
(`15548997`, `16776960`), and `notifications` only knew the image ones. So a
FAILED video run - the expensive, most worth-shouting-about case - went out on
ntfy at the DEFAULT priority 3 with no tag, and on Telegram with no status
emoji: silently the quietest alert the app can send, exactly backwards.

Nothing in the app would ever have noticed: an unknown colour degrades quietly by
design (better than raising into a run). Only a test can catch it, so:

  * one `_SEVERITY` table drives all three renderings, and every colour constant
    has a complete, sane row;
  * the runners emit NAMED constants, asserted structurally - a new raw literal
    fails the suite;
  * every colour the runners' pure decision helpers return is a known severity;
  * an unknown colour still degrades safely.
"""

import re
import pathlib

import pytest

import notifications as N


# Every module that sends a notification. Add new ones here; the structural test
# below then covers them for free.
RUNNERS = ["batch_upscale.py", "tag_and_rename.py", "conciliate.py",
           "batch_video_upscale.py", "video_benchmark.py"]

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

CONSTANTS = [N.COLOR_GREEN, N.COLOR_ORANGE, N.COLOR_YELLOW, N.COLOR_RED]


# ── the table is complete and sane ───────────────────────────────────────────

def test_every_colour_constant_has_a_full_severity_row():
    for color in CONSTANTS:
        level, emoji, tag, priority = N._severity(color)
        assert level and level != "info", color        # a real severity, not the fallback
        assert emoji, color                            # Telegram has no colours
        assert tag, color                              # ntfy tag
        assert 1 <= priority <= 5, color               # ntfy's range


def test_the_constants_are_distinct():
    assert len(set(CONSTANTS)) == len(CONSTANTS)
    levels = [N.level_for(c) for c in CONSTANTS]
    assert len(set(levels)) == len(levels)


def test_priority_rises_with_severity():
    """The whole point of the ntfy mapping: an error has to buzz louder than a
    completion, or a phone notification is useless for the case that matters."""
    prio = {c: N._severity(c)[3] for c in CONSTANTS}
    assert prio[N.COLOR_RED] == 5                      # max / urgent
    assert prio[N.COLOR_ORANGE] > prio[N.COLOR_GREEN]
    assert prio[N.COLOR_YELLOW] > prio[N.COLOR_GREEN]
    assert prio[N.COLOR_GREEN] == 3                    # default: don't cry wolf


def test_level_names():
    assert N.level_for(N.COLOR_GREEN) == "success"
    assert N.level_for(N.COLOR_ORANGE) == "warning"
    assert N.level_for(N.COLOR_YELLOW) == "caution"
    assert N.level_for(N.COLOR_RED) == "error"


# ── unknown colours degrade safely, and the legacy palette still resolves ────

def test_an_unknown_colour_is_info_and_never_raises():
    for value in (123456, None, "red", -1, 0):
        level, emoji, tag, priority = N._severity(value)
        assert level == "info" and emoji == "" and tag == ""
        assert priority == 3                           # normal, not silent, not urgent


def test_the_pre_058_video_palette_still_resolves(monkeypatch):
    """The exact bug, pinned: these are the literals the Video Upscaler used to
    emit. They are gone from the tree, but an equivalent colour from anywhere
    must still render as itself instead of degrading to no tag / priority 3."""
    assert N.level_for(0xE74C3C) == "error"            # was: unmapped
    assert N.level_for(0xF1C40F) == "caution"          # was: unmapped
    assert N.level_for(0x2ECC71) == "success"
    assert N.level_for(0xE67E22) == "warning"
    assert N._severity(0xE74C3C)[3] == 5               # the alert that used to be quiet


# ── the runners emit named constants, not literals ───────────────────────────

_COLOUR_ARG = re.compile(
    r"\b(?:color|notif_color|n_color)\b\s*(?:=|,)\s*(0x[0-9A-Fa-f]{4,8}|\d{4,9})")

# The four canonical ints and the flat-UI palette, in any notation. None of these
# may appear in a runner: they are what the constants exist to replace.
_LEGACY = [str(c) for c in CONSTANTS] + [
    "0x2ECC71", "0xE67E22", "0xF1C40F", "0xE74C3C", "0xFFFF00", "0xED4245"]


@pytest.mark.parametrize("name", RUNNERS)
def test_runner_uses_named_colour_constants(name):
    src = (SCRIPTS / name).read_text(encoding="utf-8")
    found = _COLOUR_ARG.findall(src)
    assert not found, f"{name}: raw colour literal(s) {found}; use notifications.COLOR_*"
    for literal in _LEGACY:
        assert literal not in src, f"{name}: raw colour literal {literal}"


def test_the_constants_live_in_exactly_one_place():
    """notifications.py is allowed the literals (it defines them); nowhere else."""
    src = (SCRIPTS / "notifications.py").read_text(encoding="utf-8")
    for color in CONSTANTS:
        assert str(color) in src


# ── every colour the runners actually decide on is known ─────────────────────

def _video_stop_colours():
    import batch_video_upscale as bv
    reasons = ["per-run cap of 30 min reached",
               "per-run cost cap of $5 reached (~$4.80)",
               "the staging work folder is inside the source folder being scanned",
               "gpu thrash detected",
               "stopped by user",
               "something nobody foresaw"]
    return [bv._stop_notice(r)[1] for r in reasons]


def _conciliate_colours():
    import conciliate
    return [conciliate._completion_notice(d, c, e, s)[1]
            for d, c, e, s in ((10, 0, 0, False), (5, 2, 0, False),
                               (5, 0, 1, False), (3, 0, 0, True))]


def test_every_decided_colour_is_a_known_severity():
    """The runners' pure outcome->colour helpers must only ever produce colours the
    severity table knows. This is the assertion that would have failed before the
    fix, on every video branch."""
    for color in _video_stop_colours() + _conciliate_colours():
        assert N.level_for(color) != "info", hex(color)
        assert color in CONSTANTS, hex(color)


# ── the rendering actually changes with severity ─────────────────────────────

class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


# conftest's autouse `_block_real_notifications` stubs the three senders to no-ops
# for every test, so the suite can never message a real endpoint. These tests are
# about what send_ntfy PUTS ON THE WIRE, so they need the real one back. Captured
# at import (before any fixture runs) and restored per test, with urlopen stubbed
# instead: the guard stays intact for every other test, and nothing leaves here.
_REAL_SEND_NTFY = N.send_ntfy


def _capture_ntfy(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None, context=None):
        sent["headers"] = {k.lower(): v for k, v in req.header_items()}
        sent["url"] = req.full_url
        return _FakeResponse()

    monkeypatch.setattr(N, "send_ntfy", _REAL_SEND_NTFY)
    monkeypatch.setattr(N.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_ntfy_error_is_urgent_and_tagged(monkeypatch):
    sent = _capture_ntfy(monkeypatch)
    N.send_ntfy("https://ntfy.sh", "my-topic", "Failed", "It broke", N.COLOR_RED)
    assert sent["headers"]["priority"] == "5"
    assert sent["headers"]["tags"] == "red_circle"


def test_ntfy_success_is_normal_priority(monkeypatch):
    sent = _capture_ntfy(monkeypatch)
    N.send_ntfy("https://ntfy.sh", "my-topic", "Done", "All good", N.COLOR_GREEN)
    assert sent["headers"]["priority"] == "3"
    assert sent["headers"]["tags"] == "green_circle"


def test_ntfy_unknown_colour_sends_without_a_tag(monkeypatch):
    sent = _capture_ntfy(monkeypatch)
    N.send_ntfy("https://ntfy.sh", "my-topic", "Hm", "Body", 424242)
    assert sent["headers"]["priority"] == "3"
    assert "tags" not in sent["headers"]               # absent, not empty


def test_telegram_text_leads_with_the_status_emoji():
    red = N._telegram_text("Failed", "It broke", N.COLOR_RED)
    green = N._telegram_text("Done", "All good", N.COLOR_GREEN)
    assert red.startswith("\U0001F534")
    assert green.startswith("\U0001F7E2")
    assert N._telegram_text("Hm", "Body", 424242).startswith("<b>")   # no emoji
