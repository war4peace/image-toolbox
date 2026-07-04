"""
The GUI funds readout's colour bands + formatting (recommendations item 3 UI).
funds_color / fmt_funds live in toolbox_gui (which imports tkinter), so this
skips where tkinter is unavailable. The bands mirror the telemetry colours and
are measured as a margin ABOVE the configured funds floor.
"""

import pytest

pytest.importorskip("tkinter")
import toolbox_gui as gui   # noqa: E402

BLUE = "#3a86ff"
GREEN = "#1a9e4b"
DARK_YELLOW = "#b58900"
RED = "#d11a2a"
GREY = "#7f8a99"


@pytest.mark.parametrize("delta,color", [
    (10.0, BLUE),          # exactly +$10 → blue
    (25.0, BLUE),
    (9.99, GREEN),         # just under +$10 → green
    (5.0, GREEN),          # exactly +$5 → green
    (4.99, DARK_YELLOW),   # just under +$5 → dark yellow
    (1.01, DARK_YELLOW),   # just over +$1 → dark yellow
    (1.0, RED),            # exactly +$1 → red (at/near floor)
    (0.0, RED),            # at the floor → red
    (-5.0, RED),           # below the floor → red
])
def test_bands_at_zero_floor(delta, color):
    assert gui.funds_color(delta, 0.0) == color


@pytest.mark.parametrize("delta,color", [
    (10.0, BLUE), (5.0, GREEN), (3.0, DARK_YELLOW), (0.5, RED),
])
def test_bands_are_relative_to_the_floor(delta, color):
    # A $20 floor shifts every band up by $20; the margin is what matters.
    assert gui.funds_color(20.0 + delta, 20.0) == color


def test_unknown_balance_is_grey():
    assert gui.funds_color(None, 0.0) == GREY
    assert gui.funds_color(None, 100.0) == GREY


def test_none_floor_treated_as_zero():
    assert gui.funds_color(12.0, None) == BLUE


# ── fmt_funds ───────────────────────────────────────────────────────────────

def test_fmt_funds_formats_dollars():
    assert gui.fmt_funds({"balance": 12.3}) == ("$12.30", 12.3)


def test_fmt_funds_unknown_cases():
    assert gui.fmt_funds(None) == ("Unknown", None)
    assert gui.fmt_funds({}) == ("Unknown", None)
    assert gui.fmt_funds({"balance": None}) == ("Unknown", None)
    assert gui.fmt_funds("nonsense") == ("Unknown", None)
