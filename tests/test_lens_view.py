"""
The comparison window's lens view (future-features #14).

The lens exists to show ONE patch as original and upscaled *at the same moment*,
which the before/after wipe cannot do (it shows either, and you slide the divider
to swap). That only means anything if both panels really are showing the same
patch, and if the "1:1" the upscaled panel claims really is the file's own pixels.
Both of those are arithmetic, so the arithmetic lives in three pure functions and
is pinned here without a display.

The rest of the file drives the real window (skipped where tkinter or Pillow is
missing) for the behaviour a pure test cannot reach: that the lens replaces the
wipe rather than fighting it for the pointer, that a click pins it, and that the
preference is persisted.
"""

import pytest

from gui.comparison import (LENS_FALLBACK_MAG, LENS_PANEL_MAX, LENS_ZOOMS,
                            lens_magnification, lens_panel_size, lens_placement,
                            lens_span, lens_zoom_floor)


# ── magnification: derived from the real ratio, not hard-coded ───────────────

def test_the_magnification_is_the_actual_upscale_ratio():
    """The point of deriving it: at the ratio, the upscaled panel lands on its own
    native pixels, so the panel shows the deliverable rather than a resample of
    it."""
    mag, native = lens_magnification((960, 540), (3840, 2160))
    assert (mag, native) == (4.0, True)


def test_a_non_integer_ratio_is_used_as_is():
    mag, native = lens_magnification((1000, 750), (2500, 1875))
    assert native and mag == pytest.approx(2.5)


def test_the_smaller_axis_ratio_wins():
    """A box-fit can leave the two axes at slightly different ratios. Taking the
    smaller one keeps the patch inside the panel on both axes."""
    mag, _ = lens_magnification((1000, 500), (3000, 2000))
    assert mag == pytest.approx(3.0)


@pytest.mark.parametrize("old,new", [
    ((1920, 1080), (1920, 1080)),      # a re-run at an already-reached target
    ((1920, 1080), (1940, 1090)),      # a rounding artefact, not an upscale
])
def test_same_size_falls_back_to_a_fixed_magnification(old, new):
    mag, native = lens_magnification(old, new)
    assert mag == LENS_FALLBACK_MAG
    assert native is False, "nothing is at 1:1 here and the label must not claim it"


@pytest.mark.parametrize("old,new", [
    (None, (3840, 2160)),              # the original could not be opened
    ((0, 0), (3840, 2160)),            # degenerate, never divide by it
    ((960, 540), None),
])
def test_a_missing_or_degenerate_size_falls_back(old, new):
    assert lens_magnification(old, new) == (LENS_FALLBACK_MAG, False)


# ── the sample window: centred, clamped, optionally pixel-snapped ────────────

def test_the_span_is_centred_on_the_pointer():
    a0, a1 = lens_span(0.5, 1000, 100)
    assert (a0, a1) == pytest.approx((0.45, 0.55))


@pytest.mark.parametrize("centre", [0.0, 0.02, 0.98, 1.0])
def test_the_span_never_leaves_the_image(centre):
    """Clamped rather than allowed to sample off the edge: an out-of-bounds box
    would come back a different size on each side and the two panels would stop
    showing the same patch exactly where the user is checking an edge."""
    a0, a1 = lens_span(centre, 1000, 100)
    assert 0.0 <= a0 < a1 <= 1.0
    assert a1 - a0 == pytest.approx(0.1)


def test_a_span_wider_than_the_image_becomes_the_whole_image():
    assert lens_span(0.5, 80, 180) == (0.0, 1.0)


def test_snapping_lands_on_whole_pixels():
    """What lets the native panel be a plain crop: a whole-pixel box of exactly the
    panel size needs no resampling at all."""
    a0, a1 = lens_span(0.333, 1000, 180, snap=True)
    assert a0 * 1000 == pytest.approx(round(a0 * 1000))
    assert (a1 - a0) * 1000 == pytest.approx(180)


def test_an_empty_image_does_not_divide_by_zero():
    assert lens_span(0.5, 0, 180) == (0.0, 1.0)


# ── placement: flip near an edge, then clamp ─────────────────────────────────

def test_the_box_sits_below_right_of_the_pointer():
    assert lens_placement(100, 100, 380, 200, 900, 600, 20) == (120, 120)


def test_the_box_flips_instead_of_running_off_the_edge():
    x, y = lens_placement(880, 580, 380, 200, 900, 600, 20)
    assert x + 380 <= 900 and y + 200 <= 600
    assert x < 880 and y < 580


def test_the_box_is_clamped_when_it_cannot_fit_either_way():
    """A corner on a small window: neither side fits, so clamping into the canvas
    is what keeps the panels visible at all."""
    x, y = lens_placement(5, 5, 380, 200, 420, 240, 20)
    assert (x, y) == (20, 20) or (0 <= x <= 40 and 0 <= y <= 40)
    assert 0 <= x <= 420 - 380 and 0 <= y <= 240 - 200


# ── panel size: follows the window, grows with the zoom, never overflows ────

def test_the_panel_follows_the_window_size():
    """Half of why the lens read as "so small I had to squint": a maximised window
    on a big monitor got the same fixed stamp as a small one."""
    small = lens_panel_size(1100, 640, 1)
    big = lens_panel_size(2560, 1400, 1)
    assert big > small


def test_a_zoom_step_grows_the_panel():
    assert lens_panel_size(2560, 1400, 2) > lens_panel_size(2560, 1400, 1)


@pytest.mark.parametrize("cw,ch", [(1100, 640), (900, 500), (600, 380), (420, 240)])
@pytest.mark.parametrize("zoom", LENS_ZOOMS)
def test_the_whole_box_always_fits_the_canvas(cw, ch, zoom):
    """The zoom must never push half the comparison off-screen: that would trade
    one unreadable lens for another."""
    from gui.comparison import LENS_GAP, LENS_LABEL_H, LENS_PAD

    panel = lens_panel_size(cw, ch, zoom)
    assert 2 * panel + LENS_GAP + 2 * LENS_PAD <= cw
    assert panel + LENS_LABEL_H + 2 * LENS_PAD <= ch


@pytest.mark.parametrize("zoom", LENS_ZOOMS)
def test_the_panel_is_capped_on_a_huge_canvas(zoom):
    assert lens_panel_size(7680, 4320, zoom) <= LENS_PANEL_MAX


def test_the_panel_size_never_decreases_with_zoom():
    sizes = [lens_panel_size(2560, 1400, z) for z in LENS_ZOOMS]
    assert sizes == sorted(sizes)


# ── the zoom floor: a lens weaker than the view is not a lens ────────────────

def test_a_view_already_magnifying_forces_a_stronger_lens():
    """The reported defect: a 320x240 -> 640x480 pair in a maximised window is
    drawn at ~2.7x, so a 1:1 lens showed the patch SMALLER than it already looked
    behind the panels."""
    assert lens_zoom_floor(2.7, 1) == 4


def test_the_floor_never_lowers_the_users_choice():
    assert lens_zoom_floor(0.3, 8) == 8
    assert lens_zoom_floor(1.0, 4) == 4


def test_a_shrunk_view_leaves_the_lens_at_one():
    """A 4K pair fitted into a window is displayed at well under 1:1, so native
    1:1 is already a magnification and nothing needs forcing."""
    assert lens_zoom_floor(0.28, 1) == 1


def test_the_floor_is_capped_at_the_largest_step():
    assert lens_zoom_floor(50.0, 1) == LENS_ZOOMS[-1]


# ── the real window ──────────────────────────────────────────────────────────

pytest.importorskip("tkinter")
pytest.importorskip("PIL")

import tkinter as tk                                   # noqa: E402
from types import SimpleNamespace                       # noqa: E402


def _fake_event(x, y, delta=0, state=0):
    return SimpleNamespace(x=x, y=y, delta=delta, state=state, widget=None)


@pytest.fixture(scope="module")
def root():
    """ONE hidden root for the module (creating and tearing one down per test is
    flaky on Windows)."""
    try:
        r = tk.Tk()
    except tk.TclError:                                 # no display
        pytest.skip("no Tk display")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def win(root, tmp_path, monkeypatch):
    """A real ComparisonWindow over a real 2x pair, with a fixed canvas size (the
    window is never mapped, so Tk would report 1x1) and settings kept in memory."""
    from PIL import Image

    from gui import comparison

    monkeypatch.setattr(comparison, "save_settings", lambda *a, **k: None)
    src = tmp_path / "a.png"
    out = tmp_path / "a_up.png"
    Image.new("RGB", (400, 300), (30, 90, 160)).save(src)
    Image.new("RGB", (800, 600), (30, 90, 160)).save(out)
    app = SimpleNamespace(settings={})
    w = comparison.ComparisonWindow(root, str(src), str(out), app=app)
    w.withdraw()
    w._size = lambda: (900, 600)
    w._render()
    yield w
    w.destroy()


def _lens_items(w):
    return w.canvas.find_withtag("lens")


def test_the_lens_is_off_until_asked_for(win):
    assert win._lens_on is False
    assert not _lens_items(win)


def test_hovering_with_the_lens_on_draws_the_panels(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_motion(_fake_event(450, 300))
    assert _lens_items(win), "the lens drew nothing over the middle of the image"


def test_the_lens_replaces_the_wipe_rather_than_sharing_the_canvas(win):
    """Wipe mode blits both images (two halves of one rect); lens mode blits the
    upscaled one full-frame as the context, and the comparison moves into the
    panels. Two pointer gestures on one canvas would fight."""
    assert len(win._photos) == 2
    win.lens_var.set(True)
    win._on_lens_toggle()
    assert len(win._photos) == 1


def test_the_pointer_off_the_image_draws_no_lens(win):
    """The letterbox margin is not part of either image; magnifying it would show
    the window's background."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_motion(_fake_event(5, 5))
    assert not _lens_items(win)


def test_leaving_the_canvas_takes_the_lens_with_it(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_motion(_fake_event(450, 300))
    win._on_leave(None)
    assert not _lens_items(win)


def test_a_click_pins_the_lens_and_a_second_click_releases_it(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_press(_fake_event(450, 300))
    win._on_release(_fake_event(451, 301))              # within CLICK_TOL: a click
    assert win._lens_pin == (451, 301)
    win._on_motion(_fake_event(300, 200))               # a pinned lens ignores the pointer
    assert win._lens_pin == (451, 301)
    win._on_press(_fake_event(451, 301))
    win._on_release(_fake_event(451, 301))
    assert win._lens_pin is None


def test_a_drag_pans_and_does_not_pin(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_press(_fake_event(450, 300))
    win._on_drag(_fake_event(400, 300))
    win._on_release(_fake_event(400, 300))
    assert win._lens_pin is None


def test_escape_releases_the_pin_first_and_leaves_the_window_open(win):
    """Esc backs out one level at a time, and the pin is the inner one: the lens
    hint says "click or Esc to release", so it has to win while a pin exists."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._toggle_pin(450, 300)
    assert win._lens_pin is not None
    win._on_escape()
    assert win._lens_pin is None
    assert win.winfo_exists()


def test_escape_leaves_lens_mode_before_it_leaves_the_window(win):
    """Second level of the same rule: a lens the user is looking through must not
    take the window with it."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    assert win._lens_on and win._lens_pin is None
    win._on_escape()
    assert win._lens_on is False
    assert win.lens_var.get() is False          # the tick clears with it
    assert win.winfo_exists()


def test_escape_from_a_pinned_lens_takes_three_presses_to_leave(win):
    """The whole ladder in one test: pin -> lens -> window, one level per press,
    innermost first, so each press undoes the most recent thing turned on."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._toggle_pin(450, 300)
    assert win._lens_pin is not None

    win._on_escape()
    assert win._lens_pin is None and win._lens_on and win.winfo_exists()
    win._on_escape()
    assert not win._lens_on and win.winfo_exists()
    win._on_escape()
    assert not win.winfo_exists()


def test_escape_closes_the_window_when_nothing_is_pinned(win):
    """0.6.0: with no lens and no pin to back out of, Esc does what every other
    viewer does. The window is reached by a double-click from the browser the
    user wants to get back to, and re-decoding the pair costs under a second."""
    assert win._lens_pin is None and not win._lens_on
    win._on_escape()
    assert not win.winfo_exists()


def test_escape_off_and_l_off_are_the_same_action(win):
    """Esc leaves lens mode through lens_var + _on_lens_toggle, exactly as the L
    shortcut and the checkbox do, so the two cannot drift apart (the saved
    preference in particular)."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_escape()
    esc_state = (win._lens_on, win.lens_var.get(),
                 dict(win._app.settings).get(win.LENS_SETTING))

    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_lens_key()                          # the L shortcut
    l_state = (win._lens_on, win.lens_var.get(),
               dict(win._app.settings).get(win.LENS_SETTING))
    assert esc_state == l_state == (False, False, False)


def test_escape_bound_to_the_window_reaches_that_handler(win):
    """The behaviour is only real if the KEY is wired to it."""
    assert "_on_escape" in win.bind("<Escape>")


def test_the_toggle_is_remembered_across_sessions(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    assert win._app.settings[win.LENS_SETTING] is True
    win.lens_var.set(False)
    win._on_lens_toggle()
    assert win._app.settings[win.LENS_SETTING] is False


# ── the wheel drives the lens (the fix for "I had to squint") ───────────────

def test_the_wheel_steps_the_lens_zoom_in_lens_mode(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    view_zoom = win._zoom
    win._on_wheel(_fake_event(450, 300, delta=120))
    assert win._lens_zoom == 2
    win._on_wheel(_fake_event(450, 300, delta=120))
    assert win._lens_zoom == 4
    win._on_wheel(_fake_event(450, 300, delta=-120))
    assert win._lens_zoom == 2
    assert win._zoom == view_zoom, "the wheel moved the picture behind as well"


def test_the_zoom_stops_at_the_ends_instead_of_wrapping(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    for _ in range(6):
        win._on_wheel(_fake_event(450, 300, delta=120))
    assert win._lens_zoom == LENS_ZOOMS[-1]
    for _ in range(6):
        win._on_wheel(_fake_event(450, 300, delta=-120))
    assert win._lens_zoom == LENS_ZOOMS[0]


def test_ctrl_wheel_still_zooms_the_picture_behind(win):
    """The escape hatch: lens mode takes the plain wheel, so the view zoom needs a
    way back or it would be unreachable without leaving the mode."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    lens_zoom, view_zoom = win._lens_zoom, win._zoom
    win._on_wheel(_fake_event(450, 300, delta=120, state=win.CTRL_MASK))
    assert win._lens_zoom == lens_zoom
    assert win._zoom > view_zoom


def test_the_wheel_zooms_the_view_when_the_lens_is_off(win):
    view_zoom = win._zoom
    win._on_wheel(_fake_event(450, 300, delta=120))
    assert win._zoom > view_zoom


@pytest.mark.parametrize("zoom", LENS_ZOOMS)
def test_the_zoom_step_is_the_magnification_however_it_is_reached(win, zoom):
    """A step doubles the screen pixels per upscaled pixel. WHICH way it gets there
    is a layout detail: while the panel can still grow it shows the same patch
    bigger, and once the canvas caps the panel it shows a smaller patch instead.
    Both are the same zoom, and this is the property that must hold either way."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._lens_zoom = zoom
    rect, panel, _mag, _native = win._lens_sample(450, 300)
    span_new = (rect[2] - rect[0]) * win._new.width
    assert panel / span_new == pytest.approx(zoom, rel=0.02)


def test_a_capped_panel_zooms_by_narrowing_the_patch_instead(win):
    """The canvas eventually stops the panel growing; the zoom must keep working."""
    win._size = lambda: (700, 480)          # small enough to cap quickly
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._lens_zoom = 1
    _, panel1, _, _ = win._lens_sample(350, 240)
    win._lens_zoom = 8
    rect8, panel8, _, _ = win._lens_sample(350, 240)
    assert panel8 <= panel1 * 8                     # capped
    assert (rect8[2] - rect8[0]) * win._new.width < panel1  # patch narrowed


def test_the_zoom_is_remembered_across_sessions(win):
    win.lens_var.set(True)
    win._on_lens_toggle()
    win._on_wheel(_fake_event(450, 300, delta=120))
    assert win._app.settings[win.LENS_ZOOM_SETTING] == win._lens_zoom == 2


def test_a_lens_weaker_than_the_view_is_raised_on_enable(root, tmp_path, monkeypatch):
    """The reported case end to end: a 320x240 video upscaled to 640x480, opened
    maximised. The canvas draws it at ~2.5x, so a 1:1 lens would show the patch
    smaller than it already is behind the panels."""
    from PIL import Image

    from gui import comparison

    monkeypatch.setattr(comparison, "save_settings", lambda *a, **k: None)
    src, out = tmp_path / "s.png", tmp_path / "s_up.png"
    Image.new("RGB", (320, 240), (20, 40, 80)).save(src)
    Image.new("RGB", (640, 480), (20, 40, 80)).save(out)
    w = comparison.ComparisonWindow(root, str(src), str(out),
                                    app=SimpleNamespace(settings={}))
    w.withdraw()
    w._size = lambda: (1590, 1300)          # maximised
    try:
        w._render()
        assert w._lens_zoom == 1
        w.lens_var.set(True)
        w._on_lens_toggle()
        assert w._lens_zoom == 4, "the lens was left weaker than the view behind it"
        # …and the user still gets the final say.
        w._on_wheel(_fake_event(800, 650, delta=-120))
        assert w._lens_zoom == 2
        w._render()
        assert w._lens_zoom == 2, "the floor overrode the user's own wheel"
    finally:
        w.destroy()


def test_a_new_pair_clears_where_the_lens_was_pointing(win, tmp_path):
    """The on/off preference is about how the user likes to compare; the pinned
    spot is about one image and means nothing on the next one."""
    from PIL import Image

    win.lens_var.set(True)
    win._on_lens_toggle()
    win._toggle_pin(450, 300)
    other = tmp_path / "b.png"
    other_up = tmp_path / "b_up.png"
    Image.new("RGB", (200, 200), (10, 10, 10)).save(other)
    Image.new("RGB", (800, 800), (10, 10, 10)).save(other_up)
    win.show(str(other), str(other_up))
    assert win._lens_pin is None and win._lens_pos is None
    assert win._lens_on is True


# ── the one invariant the whole feature rests on ─────────────────────────────

@pytest.fixture
def gradient_win(root, tmp_path, monkeypatch):
    """A pair whose pixel VALUES encode their position (R rises left→right, G
    rises top→bottom), so the mean colour of a tile says where it was sampled
    from. The upscaled half is an exact nearest-neighbour 2x of the original, so
    the same patch of the picture must read the same on both sides."""
    from PIL import Image

    from gui import comparison

    monkeypatch.setattr(comparison, "save_settings", lambda *a, **k: None)
    w0, h0 = 400, 300
    old = Image.new("RGB", (w0, h0))
    old.putdata([(x * 255 // w0, y * 255 // h0, 0)
                 for y in range(h0) for x in range(w0)])
    src, out = tmp_path / "g.png", tmp_path / "g_up.png"
    old.save(src)
    old.resize((w0 * 2, h0 * 2), Image.NEAREST).save(out)
    w = comparison.ComparisonWindow(root, str(src), str(out), app=SimpleNamespace(settings={}))
    w.withdraw()
    w._size = lambda: (900, 600)
    w.lens_var.set(True)
    w._on_lens_toggle()
    yield w
    w.destroy()


# The image is 800x600 in a 900x600 canvas, so it sits at x = 50..850: the last
# two points are hard against the right/bottom edges, where the clamp bites.
@pytest.mark.parametrize("pos", [(450, 300), (120, 90), (845, 595), (452, 121)])
def test_both_panels_magnify_the_same_patch(gradient_win, pos):
    """ONE normalized rect is mapped onto each image. Deriving a box per image
    would let the two drift apart at the edges (where the clamp bites) and on any
    aspect mismatch, and the lens would then be quietly comparing two different
    places, the one failure a lens must not have, because it looks like a
    difference between the images.

    Position is readable from the pixels here, so equal tile means == the two
    panels are looking at the same spot."""
    from PIL import ImageStat

    sample = gradient_win._lens_sample(*pos)
    assert sample is not None, "the point should be on the image"
    rect, panel, _mag, native = sample
    assert native
    left = ImageStat.Stat(gradient_win._lens_tile(gradient_win._old, rect, panel)).mean
    right = ImageStat.Stat(gradient_win._lens_tile(gradient_win._new, rect, panel)).mean
    for a, b in zip(left, right):
        assert a == pytest.approx(b, abs=2.0)


def test_the_native_panel_is_a_crop_not_a_resample(win):
    """"1:1" has to be true. At zoom 1 the whole-pixel box is exactly the panel's
    size, so the right-hand panel is cropped straight out of the upscaled file and
    shows the pixels that were actually written."""
    win.lens_var.set(True)
    win._on_lens_toggle()
    rect, panel, mag, native = win._lens_sample(450, 300)
    assert native and mag == pytest.approx(2.0) and win._lens_zoom == 1
    tile = win._lens_tile(win._new, rect, panel)
    assert tile.size == (panel, panel)
    bx, by = round(rect[0] * win._new.width), round(rect[1] * win._new.height)
    crop = win._new.crop((bx, by, bx + panel, by + panel))
    assert tile.tobytes() == crop.tobytes()


# ── structural: one bar builder, so the toggle cannot exist on one window only ──

def test_both_comparison_windows_build_the_same_top_bar():
    import inspect

    from gui import comparison

    src = inspect.getsource(comparison)
    assert src.count("self._build_top_bar()") == 2
