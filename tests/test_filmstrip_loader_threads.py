"""
A FilmStrip's decode threads do not outlive the FilmStrip.

Every loader used to be fire-and-forget: `threading.Thread(...).start()` with nothing
holding the reference. `_gen` asks a running loader to give up at its next checkpoint,
which is the right mechanism while the widget is alive and not a guarantee about anything
once it is not -- nobody could wait for one, and nobody could tell whether one was still
going.

That showed up twice, in opposite registers. In the app, closing the upscaled-image browser
left up to a page of decodes running against a window nobody was looking at. In CI it killed
the process: a leaked loader retrying `from PIL import Image` (uncached, because the runner
had no Pillow) met the main thread's own `importorskip("PIL.Image")` inside the import lock
with a garbage collection in between, and the run died with `Windows fatal exception: code
0x80000003` on a commit that had passed on another branch twenty minutes earlier.

So these tests assert the EFFECT -- that no thread survives the widget -- rather than that
the widget holds a set of threads or binds an event. Both of those can be true while a
loader runs on, which is precisely what happened before.
"""

import threading

import pytest

from conftest import make_tk_root

pytest.importorskip("PIL")

from gui.filmstrip import FilmStrip                                   # noqa: E402


def _images(tmp_path, n=40, size=(900, 900)):
    """Real files, big enough that decoding is not instantaneous: a strip whose threads
    have already finished proves nothing about stopping them."""
    from PIL import Image

    out = []
    for i in range(n):
        p = tmp_path / f"img{i:03d}.png"
        Image.new("RGB", size, (i * 6 % 256, 90, 200)).save(p)
        out.append(str(p))
    return out


def _new_threads(before):
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


def test_destroying_the_widget_stops_its_decode_threads(tmp_path):
    """The failure this prevents, stated plainly: a window is closed and its decode
    threads keep running, holding files and CPU, into whatever happens next."""
    paths = _images(tmp_path)
    root = make_tk_root()
    before = set(threading.enumerate())
    try:
        strip = FilmStrip(root, on_zoom=None)
        strip.set_queue(paths)
        strip.show_page(0)
        assert _new_threads(before), \
            "no loader started; the test is not exercising what it claims"
    finally:
        root.destroy()

    assert not _new_threads(before), \
        "a decode thread outlived the widget that started it"


def test_destroying_only_the_strip_is_enough(tmp_path):
    """A FilmStrip can be destroyed on its own (the browser builds a second one), so the
    guard has to hang off the widget, not off the root window going away."""
    paths = _images(tmp_path)
    root = make_tk_root()
    before = set(threading.enumerate())
    try:
        strip = FilmStrip(root, on_zoom=None)
        strip.set_queue(paths)
        strip.show_page(0)
        strip.destroy()
        assert not _new_threads(before), "destroying the strip left its loaders running"
    finally:
        root.destroy()


def test_the_handler_ignores_a_destroy_that_is_not_its_own(tmp_path):
    """The <Destroy> guard, tested at the level it actually works at.

    The first version of this test destroyed a CHILD widget and asserted the strip's
    loaders survived -- and it passed with the guard removed, because a plain `bind`
    attaches to the widget's own bindtag and never sees a child's destroy at all. It was
    a no-op wearing a pass. The guard is still worth having (the toplevel and `all` tags
    do see every child, and are one `bind_all` away), so it is exercised directly, with a
    foreign widget, which is the only way to reach it."""
    paths = _images(tmp_path, n=8, size=(200, 200))
    root = make_tk_root()
    try:
        strip = FilmStrip(root, on_zoom=None)
        strip.set_queue(paths)
        strip.show_page(0)
        gen = strip._gen

        other = type("Evt", (), {"widget": strip.winfo_children()[0]})()
        strip._on_destroy(other)
        assert strip._gen == gen, "a foreign <Destroy> invalidated the strip's own loaders"

        mine = type("Evt", (), {"widget": strip})()
        strip._on_destroy(mine)
        assert strip._gen != gen, "the strip's own <Destroy> did not stop its loaders"
    finally:
        root.destroy()


def test_stop_loaders_says_what_it_could_not_stop(tmp_path):
    """It returns the stragglers instead of sleeping and hoping. A caller that wants a
    fact gets one; the app itself ignores the answer, because an overrun costs a little
    wasted decoding and never correctness."""
    paths = _images(tmp_path)
    root = make_tk_root()
    try:
        strip = FilmStrip(root, on_zoom=None)
        strip.set_queue(paths)
        strip.show_page(0)
        assert strip.stop_loaders(timeout=5.0) == []
    finally:
        root.destroy()


def test_stopping_twice_is_harmless(tmp_path):
    """Called from <Destroy> and, in the browser's case, potentially by hand first."""
    root = make_tk_root()
    try:
        strip = FilmStrip(root, on_zoom=None)
        strip.set_queue(_images(tmp_path, n=4, size=(120, 120)))
        strip.show_page(0)
        assert strip.stop_loaders(timeout=5.0) == []
        assert strip.stop_loaders(timeout=5.0) == []
    finally:
        root.destroy()


def test_an_invalidated_loader_gives_up_before_importing_pillow(tmp_path, monkeypatch):
    """The checkpoint that was missing. `_load_batch` opened with the PIL import and only
    then looked at `_gen`, so a loader that was already stale still paid for the import --
    and with the module absent that import is not cached, so it walked the finders every
    time. That is the exact code the CI crash dump was sitting in."""
    root = make_tk_root()
    try:
        strip = FilmStrip(root, on_zoom=None)
        seen = []
        real_import = __import__

        def _watched(name, *a, **k):
            if name.startswith("PIL"):
                seen.append(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", _watched)
        strip._load_batch([str(tmp_path / "nope.png")], strip._gen - 1)   # stale by one
        assert not seen, f"a stale loader still imported {seen}"
    finally:
        root.destroy()
