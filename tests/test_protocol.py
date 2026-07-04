"""
Golden test for the @@TBX@@ stdin/stdout event protocol (item 2).

The runners emit `@@TBX@@KIND|payload\\n`; the GUI's ToolTab._filter_markers is
the single parser that must recover every event AND the human text around it,
including the awkward cases the code comments describe: an event interleaved
MID-LINE by a background telemetry thread, and a marker split across read chunks.
Emitters and parser drifting apart is exactly the class of bug this pins down.

We drive the REAL parser (ToolTab._filter_markers + _on_marker) bound onto a
minimal fake tab, so no tkinter window is created; only the terminal dispatch
(_handle_event) is stubbed to record events.
"""

import pytest

pytest.importorskip("tkinter")   # the GUI module imports tkinter at import time
import toolbox_gui                # noqa: E402

MARKER = toolbox_gui.GUI_MARKER


def emit(kind, payload, newline="\n"):
    """The exact wire format the runners write (batch_upscale._gui_event etc.)."""
    return f"{MARKER}{kind}|{payload}{newline}"


class FakeTab:
    """Carries just the parser's state; reuses the real parsing methods."""
    _filter_markers = toolbox_gui.ToolTab._filter_markers
    _on_marker = toolbox_gui.ToolTab._on_marker

    def __init__(self):
        self._hold = ""
        self._marker_buf = None
        self.events = []

    def _handle_event(self, kind, payload):
        self.events.append((kind, payload))

    def feed(self, text):
        return self._filter_markers(text)


def test_single_event_is_parsed_and_stripped():
    tab = FakeTab()
    human = tab.feed(emit("STATUS", "Loading the AI engine ..."))
    assert tab.events == [("STATUS", "Loading the AI engine ...")]
    assert human == ""      # the whole line was the event


def test_human_text_passes_through_events_are_captured():
    tab = FakeTab()
    stream = "starting run\n" + emit("IMG", "C:/pics/a.jpg") + "done\n"
    human = tab.feed(stream)
    assert ("IMG", "C:/pics/a.jpg") in tab.events
    assert "starting run" in human
    assert "done" in human
    assert MARKER not in human


def test_payload_may_contain_pipes():
    # ETA's payload is pipe-delimited itself; partition('|') must keep the rest.
    tab = FakeTab()
    tab.feed(emit("ETA", "12.500|3|4|10|P"))
    assert tab.events == [("ETA", "12.500|3|4|10|P")]


def test_json_payload_round_trips():
    tab = FakeTab()
    import json
    payload = json.dumps(["C:/pics/a.jpg", "ok", "C:/out/a.jpg"])
    tab.feed(emit("RESULT", payload))
    assert tab.events == [("RESULT", payload)]
    assert json.loads(tab.events[0][1]) == ["C:/pics/a.jpg", "ok", "C:/out/a.jpg"]


def test_marker_interleaved_mid_line_reconstructs_the_human_line():
    # A telemetry sample emitted mid-line by a background thread: the event is
    # consumed WITH its trailing newline, rejoining the split human text.
    tab = FakeTab()
    stream = "processing " + emit("RTELEM", "{}") + "image 5\n"
    human = tab.feed(stream)
    assert ("RTELEM", "{}") in tab.events
    assert "processing image 5" in human


def test_marker_split_across_chunks():
    tab = FakeTab()
    # The marker "@@TBX@@" arrives split: "@@TB" then "X@@KIND|p\n".
    part1, part2 = MARKER[:4], MARKER[4:] + "STATUS|hello\n"
    out1 = tab.feed(part1)
    assert out1 == ""              # partial marker held back, not shown as text
    assert tab.events == []
    tab.feed(part2)
    assert tab.events == [("STATUS", "hello")]


def test_crlf_line_endings_are_trimmed():
    # The Windows pipe delivers \r\n; _on_marker must strip the trailing \r.
    tab = FakeTab()
    tab.feed(emit("STATUS", "hello", newline="\r\n"))
    assert tab.events == [("STATUS", "hello")]


def test_event_with_empty_payload():
    tab = FakeTab()
    tab.feed(emit("POD", ""))
    assert tab.events == [("POD", "")]


def test_multiple_events_in_one_chunk():
    tab = FakeTab()
    stream = emit("QUEUE", "[]") + emit("IMG", "a.jpg") + emit("PROG", "1|10")
    tab.feed(stream)
    assert tab.events == [("QUEUE", "[]"), ("IMG", "a.jpg"), ("PROG", "1|10")]
