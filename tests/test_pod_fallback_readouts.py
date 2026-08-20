"""
The two readouts #25 P4 added, tested where they are actually rendered.

Both exist for the same reason: when a remote run goes wrong, the app's own
channels to the pod (the ssh tunnel, the worker) are exactly what is missing,
while RunPod's control plane can still answer. So the telemetry row has to be
able to show a THINNER sample without pretending it is the usual one, and the
RunPod tab has to be able to show what an account has been charged, since spend
is the only money figure left once the balance retires (#25 P3).
"""

import pytest

pytest.importorskip("tkinter")
import tkinter as tk                                        # noqa: E402

from gui.widgets import TelemetryRow                        # noqa: E402
from gui.tab_runpod import _fmt_spend                        # noqa: E402


@pytest.fixture(scope="module")
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for tkinter")
    r.withdraw()
    try:
        yield r
    finally:
        r.destroy()


def _texts(row):
    return [w.cget("text") for w in row._labels]


def test_a_percentage_only_sample_is_shown_as_a_percentage(root):
    """The control plane publishes utilisation with no capacities. Showing
    "VRAM 87%" is the honest reading; inventing a total to keep the familiar
    "12.3/24.0 GB" shape would put a fabricated number beside real ones."""
    row = TelemetryRow(root)
    row.show({"cpu": 41, "ram_pct": 63, "gpu_util_pct": 98, "gpu_mem_pct": 87,
              "via": "api"})
    text = " ".join(_texts(row))
    assert "CPU 41%" in text
    assert "RAM 63%" in text
    assert "VRAM 87%" in text
    assert "GPU 98%" in text
    assert "GB" not in text          # nothing was invented


def test_the_row_says_the_sample_came_the_other_way(root):
    """Otherwise the drop in detail reads as the pod having gone quiet, when what
    actually happened is that the tunnel went down."""
    row = TelemetryRow(root)
    row.show({"cpu": 41, "gpu_util_pct": 98, "via": "api"})
    assert any("tunnel down" in t for t in _texts(row))


def test_the_ordinary_sample_is_untouched(root):
    """The fallback is additive. A worker sample still renders exactly as it did,
    with its absolute figures and no provenance note."""
    row = TelemetryRow(root)
    row.show({"cpu": 12, "ram_used_mb": 8192, "ram_total_mb": 32768,
              "gpu_used_mb": 12288, "gpu_total_mb": 24576, "gpu_util_pct": 77,
              "gpu_temp_c": 64})
    text = " ".join(_texts(row))
    assert "RAM 8.0/32.0 GB (25%)" in text
    assert "VRAM 12.0/24.0 GB (50%)" in text
    assert "tunnel down" not in text


def test_an_absolute_reading_wins_over_a_percentage(root):
    """A sample carrying both is the worker's, which knows the capacities. The
    richer form must not be displaced by the fallback field."""
    row = TelemetryRow(root)
    row.show({"ram_used_mb": 8192, "ram_total_mb": 32768, "ram_pct": 99})
    assert any("RAM 8.0/32.0 GB (25%)" in t for t in _texts(row))


def test_a_pod_with_no_gpu_reading_at_all_still_says_so(root):
    """The existing "GPU: n/a" must survive the new percentage branch, or an
    unreadable GPU turns into a row that simply omits it."""
    row = TelemetryRow(root)
    row.show({"cpu": 5})
    assert any("GPU: n/a" in t for t in _texts(row))


def test_spend_says_where_the_money_went():
    """The split is the point. A network volume bills around the clock whether or
    not anything is running, which no other readout in the app reveals."""
    assert _fmt_spend({"days": 30, "total": 4.26, "gpu": 0.79, "storage": 3.48}) == (
        "spent in the last 30 days: $4.26 ($0.79 pods, $3.48 storage)")


def test_no_spend_figure_means_no_label():
    """None on the v1 escape hatch (no billing route), on an unreachable API and
    with no key. An empty label, never a "$0.00" that would read as "nothing was
    charged"."""
    assert _fmt_spend(None) == ""
    assert _fmt_spend({}) == ""
    assert _fmt_spend({"total": 4.26}) == ""
