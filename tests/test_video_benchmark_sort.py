"""
Benchmark window results filtering + column sorting (parity with the Video Upscaler lists).

The wiring (detach/reattach filter, header-click view-sort) mirrors the already-covered
tab_video mechanics; what is fragile and worth pinning is how the sort keys parse the DISPLAY
cells back into sortable numbers, since the table shows strings ('0.39', '23.1/23.1 GB',
'1:02:03', '—'). These are pure staticmethods, so no Tk window is needed (importing the module
only needs tkinter present, not a display).
"""

import pytest

pytest.importorskip("tkinter")

from gui.video_benchmark import BenchmarkWindow as BW


# ── _num: leading number of a display cell ───────────────────────────────────

def test_num_parses_plain_integer():
    assert BW._num("5") == 5.0


def test_num_parses_decimal_spf():
    assert BW._num("0.39") == 0.39


def test_num_parses_leading_number_of_peak_vram():
    assert BW._num("23.1/23.1 GB") == 23.1


def test_num_blank_and_dash_sort_to_bottom():
    assert BW._num("—") == -1.0
    assert BW._num("") == -1.0
    assert BW._num(None) == -1.0


def test_num_orders_absent_below_present():
    # An un-benchmarked cell ('—') must sort below any real value in an ascending sort.
    assert BW._num("—") < BW._num("0.01")


# ── _hms_to_s: runtime cell back to seconds ──────────────────────────────────

def test_hms_minutes_seconds():
    assert BW._hms_to_s("1:30") == 90.0


def test_hms_hours_minutes_seconds():
    assert BW._hms_to_s("1:02:03") == 3723.0


def test_hms_blank_and_dash():
    assert BW._hms_to_s("—") == -1.0
    assert BW._hms_to_s("") == -1.0
    assert BW._hms_to_s(None) == -1.0


def test_hms_ordering_is_numeric_not_lexical():
    # '10:00' (600 s) must sort ABOVE '9:00' (540 s); a string sort would invert them.
    assert BW._hms_to_s("10:00") > BW._hms_to_s("9:00")


def test_hms_bad_value_is_safe():
    assert BW._hms_to_s("n/a") == -1.0
