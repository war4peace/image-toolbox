"""
The upscaled-image browser (future-features #22).

Two things in this feature fail invisibly, so both are pinned here without a
display. **Pairing**: showing two unrelated files side by side is the one failure
it must not have, and a wrong pair is only obvious if you happen to recognise the
photo. **Paging**: an off-by-one page is invisible in a screenshot, and the strip
silently shows the wrong hundred images.

The rest of the window (modality, the tree, the scan thread) is behaviour a pure
test cannot reach; the parts of it that ARE pure -- the folder-row derivation and
the entry ordering -- are covered here too.
"""

import os
import time

import pytest

from gui.browse_upscaled import (Pair, clamp_page, folder_rows, invert_tag_index,
                                 page_count, page_label, page_slice, pair_source,
                                 resolve_file, scan_pairs, sort_entries)


ROOT = os.path.normpath("D:/Photos")


def fake_fs(*paths):
    """A `resolve` over a fixed set of paths: it matches case-insensitively (as
    Windows does) but answers with the name as it was really spelled."""
    have = {os.path.normcase(os.path.normpath(p)): os.path.normpath(p)
            for p in paths}
    return lambda p: have.get(os.path.normcase(os.path.normpath(p)))


# ── pairing: the mirror, inverted ────────────────────────────────────────────

def test_the_mirrored_name_resolves_the_source():
    """The whole reason pairing needs no database: relpath + a lowercased
    extension inverts with a stat."""
    isfile = fake_fs("D:/Photos/2004/trip/img_1.jpg")
    got = pair_source(os.path.join("2004", "trip", "img_1.jpg"), ROOT,
                      resolve=isfile)
    assert got == os.path.normpath("D:/Photos/2004/trip/img_1.jpg")


def test_the_source_extension_may_differ_in_case():
    """The upscaler lowercases the extension it writes, so DSC_0001.JPG comes
    back as DSC_0001.jpg and pairing has to look past the case."""
    isfile = fake_fs("D:/Photos/DSC_0001.JPG")
    assert pair_source("DSC_0001.jpg", ROOT, resolve=isfile) == \
        os.path.normpath("D:/Photos/DSC_0001.JPG")


def test_the_source_is_returned_spelled_the_way_it_is_on_disk(tmp_path):
    """Windows stats case-insensitively, so a plain isfile probe would find the
    file and then hand back a name that disagrees with Explorer -- which is what
    the browser would go on to show, sort and copy."""
    _touch(str(tmp_path / "DSC_0001.JPG"))
    got = resolve_file(str(tmp_path / "dsc_0001.jpg"))
    assert got is not None and os.path.basename(got) == "DSC_0001.JPG"


def test_resolve_file_answers_none_for_a_file_that_is_not_there(tmp_path):
    assert resolve_file(str(tmp_path / "nope.jpg")) is None


def test_a_missing_source_is_no_source_not_a_guess():
    assert pair_source("gone.jpg", ROOT, resolve=fake_fs()) is None


def test_no_source_root_pairs_nothing():
    """The browser still lists an upscaled tree with no photo folder set; every
    entry is simply unpaired."""
    assert pair_source("a.jpg", "", resolve=fake_fs("a.jpg")) is None


# ── pairing: a tagged & renamed output ───────────────────────────────────────

def test_a_renamed_output_resolves_through_the_inverted_tag_index():
    """Tag & Rename is step 2 of the documented workflow, so its renames are the
    normal case, not an edge one. The cache maps original->current; browsing
    starts from the file on disk, so the useful direction is the reverse."""
    tr = {os.path.normcase(os.path.join("2004", "img_1.jpg")):
          os.path.join("2004", "img_1_Boy_on_a_beach.jpg")}
    inv = invert_tag_index(tr)
    isfile = fake_fs("D:/Photos/2004/img_1.jpg")
    got = pair_source(os.path.join("2004", "img_1_Boy_on_a_beach.jpg"), ROOT,
                      inv_tag=inv, resolve=isfile)
    assert got == os.path.normpath("D:/Photos/2004/img_1.jpg")


def test_the_tag_index_is_tried_before_the_mirrored_name():
    """A renamed output whose new name happens to exist in the source tree must
    still resolve to the file it actually came from."""
    tr = {os.path.normcase("real.jpg"): "decoy.jpg"}
    isfile = fake_fs("D:/Photos/real.jpg", "D:/Photos/decoy.jpg")
    assert pair_source("decoy.jpg", ROOT, inv_tag=invert_tag_index(tr),
                       resolve=isfile) == os.path.normpath("D:/Photos/real.jpg")


def test_an_untagged_output_falls_back_to_the_mirrored_name():
    tr = {os.path.normcase("other.jpg"): "other_Renamed.jpg"}
    isfile = fake_fs("D:/Photos/plain.jpg")
    assert pair_source("plain.jpg", ROOT, inv_tag=invert_tag_index(tr),
                       resolve=isfile) == os.path.normpath("D:/Photos/plain.jpg")


def test_an_empty_tag_index_is_harmless():
    assert invert_tag_index(None) == {}
    assert invert_tag_index({}) == {}


# ── the scan walks the output tree ───────────────────────────────────────────

def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


def test_the_scan_pairs_what_it_finds_and_leaves_the_rest_unpaired(tmp_path):
    src = tmp_path / "src"
    out = tmp_path / "src" / "__upscaled__"
    _touch(str(src / "a.JPG"))
    _touch(str(src / "sub" / "b.png"))
    _touch(str(out / "a.jpg"))
    _touch(str(out / "sub" / "b.png"))
    _touch(str(out / "orphan.jpg"))         # its original was deleted
    _touch(str(out / "notes.txt"))          # not an image

    pairs, _ = scan_pairs(str(out), str(src))
    by_name = {os.path.basename(p.out): p for p in pairs}
    assert set(by_name) == {"a.jpg", "b.png", "orphan.jpg"}
    assert by_name["a.jpg"].src == os.path.normpath(str(src / "a.JPG"))
    assert by_name["b.png"].rel_dir == "sub"
    assert by_name["orphan.jpg"].src is None
    # A pair-less output is keyed by the file that exists, which is what makes
    # it cost nothing: no status, no compare entry, open-the-file on double click.
    assert by_name["orphan.jpg"].key == by_name["orphan.jpg"].out


def test_the_scan_prunes_the_apps_own_derived_folders(tmp_path):
    """An __Archive__ nested under the output root is not 'upscaled images' (#16).
    The pruner prunes SUBdirectories only, never the chosen root -- which matters
    here, because the root normally IS __upscaled__."""
    out = tmp_path / "__upscaled__"
    _touch(str(out / "kept.jpg"))
    _touch(str(out / "__Archive__" / "hidden.jpg"))
    _touch(str(out / ".imgtbx_video" / "seg.jpg"))

    pairs, pruned = scan_pairs(str(out), "")
    assert [os.path.basename(p.out) for p in pairs] == ["kept.jpg"]
    assert pruned and "__Archive__" in pruned


def test_an_output_never_pairs_with_itself(tmp_path):
    """Pointing both roots at the same folder would otherwise 'pair' every file
    with itself and claim an upscale that never happened."""
    root = tmp_path / "same"
    _touch(str(root / "a.jpg"))
    pairs, _ = scan_pairs(str(root), str(root))
    assert pairs[0].src is None


# ── paging ───────────────────────────────────────────────────────────────────

def test_no_images_is_no_pages():
    """Not one empty page: 'Page 1 of 1' would claim a page with nothing on it."""
    assert page_count(0) == 0
    assert page_slice(0, 0) == (0, 0)


def test_a_browser_page_holds_200_not_the_tabs_100():
    """A maximised browser at 4K fits a little over 200 default-size cells, so a
    100-image page left half the wall empty. Measured before changing it: decode
    is linear and off-thread (time to the first thumbnail is unchanged), and the
    price is memory, which this window can afford. The tool tabs keep 100."""
    from gui.browse_upscaled import BROWSE_PAGE_SIZE
    from gui.filmstrip import BATCH_SIZE

    assert BROWSE_PAGE_SIZE == 200
    assert BATCH_SIZE == 100                    # the tabs are deliberately not
    assert page_count(200) == 1                 # changed with it
    assert page_slice(0, 250) == (0, 200)
    assert page_count(250) == 2


@pytest.mark.parametrize("total,expected", [(1, 1), (99, 1), (100, 1),
                                            (101, 2), (250, 3)])
def test_pages_are_whole_batches(total, expected):
    assert page_count(total, 100) == expected


def test_a_page_number_is_clamped_not_wrapped():
    """The page bar steps by 5, so it routinely asks for a page past the end;
    landing on the last page is right, wrapping to the first is not."""
    assert clamp_page(7, 250, 100) == 2
    assert clamp_page(-3, 250, 100) == 0
    assert clamp_page(1, 250, 100) == 1


def test_the_last_page_is_short_not_padded():
    assert page_slice(2, 250, 100) == (200, 250)


def test_the_first_page_starts_at_zero():
    assert page_slice(0, 250, 100) == (0, 100)


def test_the_label_counts_from_one_for_the_human():
    label = page_label(1, 250, 100)
    assert "Page 2 of 3" in label and "101-200" in label


def test_a_single_page_says_how_many_not_which_page():
    assert page_label(0, 12, 100) == "12 images"


def test_an_empty_folder_says_so():
    assert page_label(0, 0, 100) == "No images in this folder"


# ── the folder tree ──────────────────────────────────────────────────────────

def test_only_folders_whose_subtree_holds_images_are_listed():
    """A tree of empty folders is noise -- but an ancestor is needed to reach one
    that is not empty, and it honestly shows 0 (a folder lists its own images)."""
    rows = folder_rows({"": 0,
                        os.path.join("2004", "trip"): 3,
                        "empty": 0})
    rels = [r for r, _, _ in rows]
    assert rels == ["", "2004", os.path.join("2004", "trip")]
    assert dict((r, n) for r, _, n in rows)["2004"] == 0
    assert "empty" not in rels


def test_a_parent_always_precedes_its_children():
    rows = folder_rows({os.path.join("a", "b", "c"): 1, "a": 2, "z": 1})
    order = [r for r, _, _ in rows]
    assert order.index("a") < order.index(os.path.join("a", "b"))
    assert order.index(os.path.join("a", "b")) < order.index(os.path.join("a", "b", "c"))


def test_folders_sort_case_insensitively():
    rows = folder_rows({"Zebra": 1, "apple": 1})
    assert [r for r, _, _ in rows] == ["", "apple", "Zebra"]


def test_each_row_names_its_parent():
    rows = dict((r, p) for r, p, _ in folder_rows({os.path.join("a", "b"): 1}))
    assert rows[""] is None
    assert rows["a"] == ""
    assert rows[os.path.join("a", "b")] == "a"


# ── ordering ─────────────────────────────────────────────────────────────────

def test_thumbnails_sort_by_name_case_insensitively():
    """Not by mtime: every file in an upscaled tree shares 'when the batch ran',
    so a time sort is an arbitrary order that merely looks meaningful."""
    entries = [Pair(os.path.normpath("D:/o/Zoo.jpg")),
               Pair(os.path.normpath("D:/o/apple.jpg")),
               Pair(os.path.normpath("D:/o/Boat.jpg"))]
    assert [os.path.basename(e.key) for e in sort_entries(entries)] == \
        ["apple.jpg", "Boat.jpg", "Zoo.jpg"]


def test_a_paired_entry_is_keyed_by_its_source():
    """So the thumbnail, the green frame and the context menu's 'Open original'
    all line up with the run strip, which is keyed the same way."""
    p = Pair(os.path.normpath("D:/out/a.jpg"), os.path.normpath("D:/src/a.jpg"))
    assert p.key == os.path.normpath("D:/src/a.jpg")


# ── opt-in content matching ──────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A throwaway cache.db, so these tests never touch the developer's own."""
    import db

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield db, conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def test_content_matching_finds_an_original_that_was_moved(isolated_db, tmp_path):
    """The case the mirror cannot cover: the original was moved or renamed after
    it was upscaled, so only its content still identifies it."""
    from gui.browse_upscaled import match_by_content

    db, conn = isolated_db
    src = tmp_path / "src"
    out = tmp_path / "out"
    os.makedirs(str(src / "moved_here"), exist_ok=True)
    os.makedirs(str(out), exist_ok=True)
    (src / "moved_here" / "renamed.jpg").write_bytes(b"original bytes")
    (out / "a.jpg").write_bytes(b"upscaled bytes")

    h_src = db.hash_file_cached(conn, str(src / "moved_here" / "renamed.jpg"))
    h_out = db.hash_file_cached(conn, str(out / "a.jpg"))
    db.record_upscale_lineage(conn, h_src, h_out)

    pairs, _ = scan_pairs(str(out), str(src))
    assert pairs[0].src is None                 # the mirror cannot find it
    assert match_by_content(pairs, str(src)) == 1
    assert pairs[0].src == os.path.normpath(str(src / "moved_here" / "renamed.jpg"))
    assert pairs[0].by_content is True


def test_content_matching_never_walks_the_source_tree_without_a_lineage_row(
        isolated_db, tmp_path, monkeypatch):
    """The cheap end first: if not one unpaired output has a recorded source
    hash, the expensive half must not happen at all."""
    from gui import browse_upscaled as bu

    src, out = tmp_path / "src", tmp_path / "out"
    os.makedirs(str(src), exist_ok=True)
    _touch(str(out / "a.jpg"))
    (src / "unrelated.jpg").write_bytes(b"nothing to do with it")

    walked = []
    real_walk = os.walk
    monkeypatch.setattr(bu.os, "walk",
                        lambda p, *a, **k: (walked.append(p), real_walk(p, *a, **k))[1])
    pairs, _ = scan_pairs(str(out), str(src))
    walked.clear()
    assert bu.match_by_content(pairs, str(src)) == 0
    assert walked == []


def test_content_matching_leaves_an_already_paired_original_alone(isolated_db,
                                                                 tmp_path):
    """A source that is already somebody's original is not a candidate: two
    outputs claiming one original is exactly the wrong-pair failure."""
    from gui.browse_upscaled import match_by_content

    db, conn = isolated_db
    src, out = tmp_path / "src", tmp_path / "out"
    os.makedirs(str(src), exist_ok=True)
    os.makedirs(str(out), exist_ok=True)
    (src / "a.jpg").write_bytes(b"the one original")
    (out / "a.jpg").write_bytes(b"upscale of a")
    (out / "b.jpg").write_bytes(b"upscale of something else")

    h_src = db.hash_file_cached(conn, str(src / "a.jpg"))
    db.record_upscale_lineage(conn, h_src,
                              db.hash_file_cached(conn, str(out / "b.jpg")))

    pairs, _ = scan_pairs(str(out), str(src))
    assert match_by_content(pairs, str(src)) == 0
    by_name = {os.path.basename(p.out): p for p in pairs}
    assert by_name["a.jpg"].src is not None     # the mirror got this one
    assert by_name["b.jpg"].src is None         # and it is not up for grabs


# ── the real widgets ─────────────────────────────────────────────────────────

pytest.importorskip("tkinter")

import tkinter as tk                                      # noqa: E402
from tkinter import ttk                                    # noqa: E402
from types import SimpleNamespace                          # noqa: E402

from gui.filmstrip import BATCH_SIZE, FilmStrip            # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts")


@pytest.fixture(scope="module")
def root():
    """ONE hidden root for the module (creating and tearing one down per test is
    flaky on Windows)."""
    try:
        r = tk.Tk()
    except tk.TclError:                                    # no display
        pytest.skip("no Tk display")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def strip(root):
    s = FilmStrip(root, on_zoom=None)
    yield s
    s.destroy()


def test_pages_can_be_turned_without_a_current_image(strip):
    """The whole reason show_page exists. The visible batch used to be a side
    effect of set_current, so a browser with no 'currently processing' image had
    to fake one to turn the page: working the widget through its side effects."""
    paths = [f"D:/o/{i:04d}.jpg" for i in range(250)]
    strip.set_queue(paths)
    assert strip.page_count() == 3

    strip.show_page(0)
    assert strip._order[0] == paths[0] and len(strip._order) == BATCH_SIZE
    strip.show_page(2)
    assert strip._order == paths[200:]
    assert strip.current_page() == 2


def test_turning_a_page_never_claims_an_image_is_being_processed(strip):
    """The blue current-frame means 'the runner is working on this one'. A page
    is a viewport, and saying otherwise would be a lie in a browser where nothing
    is running at all."""
    strip.set_queue([f"D:/o/{i}.jpg" for i in range(150)])
    strip.show_page(1)
    assert strip._current is None
    assert strip._hl_id is None


def test_a_page_past_the_end_is_clamped(strip):
    strip.set_queue([f"D:/o/{i}.jpg" for i in range(150)])
    strip.show_page(99)
    assert strip.current_page() == 1


def test_an_empty_queue_has_no_pages(strip):
    strip.set_queue([])
    assert strip.page_count() == 0
    strip.show_page(0)                                     # must not raise
    assert strip._order == []


def test_set_current_still_drives_the_page_it_used_to(strip):
    """The run strip's behaviour is unchanged: FilmStrip is on all four tool
    tabs, so this refactor had to be purely additive."""
    paths = [f"D:/o/{i}.jpg" for i in range(250)]
    strip.set_queue(paths)
    strip.set_current(paths[130])
    assert strip.current_page() == 1
    assert strip._current == paths[130]


# ── modality: the one failure that is not visible ────────────────────────────

def test_closing_the_browser_brings_the_main_window_back(root, tmp_path,
                                                         monkeypatch):
    """The riskiest line in the feature. Everything else fails visibly; this one
    fails by leaving the app running with no visible window at all, and
    single_instance.py then blocks a relaunch."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    root.deiconify()
    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    assert root.state() == "withdrawn"      # the modal guarantee, without a grab
    win._close()
    assert root.state() == "normal"
    root.withdraw()


def test_a_teardown_that_is_not_a_close_still_restores_the_main_window(
        root, tmp_path, monkeypatch):
    """WM_DELETE_WINDOW is not the only way a Toplevel goes away, so <Destroy>
    is bound as well (guarded to this widget: it bubbles up from every child)."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    root.deiconify()
    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    win.destroy()                            # no _close, no protocol handler
    root.update()
    assert root.state() == "normal"
    root.withdraw()


def test_the_scan_really_runs_on_its_thread_and_finds_the_pairs(root, tmp_path,
                                                                monkeypatch):
    """End to end through the real worker thread, because the interesting failure
    only happens there: the worker used to read the 'Match by content' CHECKBOX,
    and a Tk variable belongs to the main thread, so every scan died on
    "main thread is not in main loop" and reported itself as a failed scan."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    src, out = tmp_path / "src", tmp_path / "src" / "__upscaled__"
    _touch(str(src / "a.JPG"))
    _touch(str(out / "a.jpg"))
    _touch(str(out / "sub" / "lonely.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, str(src), str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        assert not win._busy, "the scan never finished"
        assert "failed" not in win.status_var.get().lower(), win.status_var.get()
        assert len(win._pairs) == 2
        assert sum(1 for p in win._pairs if p.src) == 1
        # Two folders hold images, so both are on the tree and the root's own
        # image is the only one the root folder lists.
        assert win.tree.get_children("")            # the root row exists
        assert len(win._entries) == 1
    finally:
        win._close()
        root.withdraw()


def test_a_filmstrip_page_size_is_per_widget(strip, root):
    """The browser needs 200 per page while the tool tabs need 100, so the size
    is a constructor argument rather than the module constant it used to be."""
    from gui.filmstrip import FilmStrip

    paths = [f"D:/o/{i:04d}.jpg" for i in range(250)]
    strip.set_queue(paths)
    assert strip.page_count() == 3               # default 100

    big = FilmStrip(root, on_zoom=None, page_size=200)
    try:
        big.set_queue(paths)
        assert big.page_count() == 2
        big.show_page(1)
        assert big._order == paths[200:]
    finally:
        big.destroy()


# ── layout: the toolbar belongs to the strip, not to the window ──────────────

def test_the_zoom_and_paging_controls_sit_over_the_thumbnails_only(
        root, tmp_path, monkeypatch):
    """They act on the thumbnails, so they span the thumbnail pane and nothing
    else. Spanning the window would put them under the folder tree too — and the
    split is draggable, so they would drift away from the wall they belong to."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        right = str(win.strip.winfo_parent())
        assert str(win._bar.winfo_parent()) == right
        for b in win._page_btns.values():
            assert str(b).startswith(right)
        # …and the toolbar is above the wall, not below it.
        assert win._bar.grid_info()["row"] < win.strip.grid_info()["row"]
    finally:
        win._close()
        root.withdraw()


def test_the_paging_controls_are_centred_over_the_wall(root, tmp_path,
                                                       monkeypatch):
    """Centred on the WALL, not merely on the space the zoom buttons left over:
    the two outer columns share the slack equally through one `uniform` group."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    for i in range(3):
        _touch(str(out / f"a{i}.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        win.geometry("1200x700")
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        for _ in range(20):
            root.update()
            time.sleep(0.02)
        nav = win._page_btns["prev"].master
        bar_w = win._bar.winfo_width()
        nav_centre = nav.winfo_x() + nav.winfo_width() / 2
        # Within a pixel of the bar's midpoint (grid rounds odd leftovers).
        assert abs(nav_centre - bar_w / 2) <= 1, (nav_centre, bar_w / 2)
        # The zoom buttons still hug the left edge rather than being centred too.
        assert win._bar.grid_slaves(row=0, column=0)[0].winfo_x() <= 2
    finally:
        win._close()
        root.withdraw()


def test_the_folder_tree_opens_collapsed_below_the_root(root, tmp_path,
                                                        monkeypatch):
    """A deep photo tree expanded in full is a wall of folders to scroll past.
    The root is open so its children are visible at once; the rest is one click."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "2004" / "trip" / "a.jpg"))
    _touch(str(out / "2005" / "b.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        roots = win.tree.get_children("")
        assert len(roots) == 1
        # Tk answers 1/0 here, not True/False.
        assert bool(win.tree.item(roots[0], "open")) is True
        kids = win.tree.get_children(roots[0])
        assert len(kids) == 2                   # 2004 and 2005 are both visible
        for iid in kids:
            assert bool(win.tree.item(iid, "open")) is False, iid
    finally:
        win._close()
        root.withdraw()


def test_the_status_line_lives_at_the_bottom_and_the_close_button_with_it(
        root, tmp_path, monkeypatch):
    """Status belongs where a status bar belongs. Close comes down with it rather
    than being left alone in a header row of its own."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        assert win.status_lbl.master is win._foot
        assert win._foot.grid_info()["row"] > win._panes.grid_info()["row"]
        # The Close button shares that bottom row.
        assert any(isinstance(c, ttk.Button) and c.cget("text") == "Close"
                   for c in win._foot.winfo_children())
        # …and nothing is left above the panes.
        assert win._panes.grid_info()["row"] == 0
    finally:
        win._close()
        root.withdraw()


def test_match_by_content_sits_above_the_folder_tree(root, tmp_path, monkeypatch):
    """It re-pairs images, which moves them between folders and changes the counts
    on the tree rows — so it belongs over the tree. Over the wall it read as a
    filter on the thumbnails on screen, which it is not."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        assert str(win.content_chk.winfo_parent()) == str(win.tree.winfo_parent())
        assert win.content_chk.grid_info()["row"] < win.tree.grid_info()["row"]
    finally:
        win._close()
        root.withdraw()


def test_the_status_line_counts_folders_as_well_as_images(root, tmp_path,
                                                          monkeypatch):
    """'640 images' says nothing about how much of the tree they are spread over,
    which is what tells the user whether to go looking in sub-folders."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    _touch(str(out / "b.jpg"))
    _touch(str(out / "2004" / "c.jpg"))
    _touch(str(out / "2005" / "Trip" / "d.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        status = win.status_var.get()
        # 4 images across 3 folders that actually hold one; "2005" holds none of
        # its own and is a signpost, not a location, so it is not counted.
        assert "4 upscaled image(s) in 3 folder(s)" in status, status
    finally:
        win._close()
        root.withdraw()


def test_the_browser_first_opens_at_the_main_windows_first_run_size(
        root, tmp_path, monkeypatch):
    """It replaces the main window on screen, so opening at a different size
    reads as the app having jumped somewhere else."""
    from gui import browse_upscaled as bu
    from gui.common import DEFAULT_WINDOW_GEOMETRY

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        root.update()
        assert win.geometry().startswith(DEFAULT_WINDOW_GEOMETRY)
    finally:
        win._close()
        root.withdraw()


def test_neither_window_hardcodes_its_default_size():
    """The two sizes are equal because they are the SAME constant, not because
    somebody kept two literals in step."""
    import re
    from gui.common import DEFAULT_WINDOW_GEOMETRY

    for mod in ("app", "browse_upscaled"):
        path = os.path.join(SCRIPTS, "gui", f"{mod}.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        stray = re.findall(r'"\d{3,4}x\d{3,4}"', src)
        assert DEFAULT_WINDOW_GEOMETRY not in stray, (mod, stray)


def test_the_status_line_does_not_repeat_the_checkbox(root, tmp_path,
                                                      monkeypatch):
    """The 'Match by content' checkbox is on that very row with its own tooltip,
    so spelling out 'tick Match by content to …' only crowded the line."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    src, out = tmp_path / "src", tmp_path / "out"
    os.makedirs(str(src), exist_ok=True)
    _touch(str(out / "orphan.jpg"))             # unpaired: the old hint's trigger
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, str(src), str(out), app=app)
    try:
        for _ in range(200):
            root.update()
            if not win._busy:
                break
            time.sleep(0.02)
        status = win.status_var.get()
        assert "0 with the original alongside" in status, status
        assert "tick" not in status.lower(), status
    finally:
        win._close()
        root.withdraw()


def test_the_browser_takes_no_grab(root, tmp_path, monkeypatch):
    """A local grab routes events to the grabbing window AND ITS DESCENDANTS, and
    the shared comparison window is a child of App, not of this one: a grab would
    make it open and then ignore every click, which is the entire feature."""
    from gui import browse_upscaled as bu

    monkeypatch.setattr(bu, "save_settings", lambda *a, **k: None)
    out = tmp_path / "__upscaled__"
    _touch(str(out / "a.jpg"))
    app = SimpleNamespace(settings={}, show_comparison=lambda *a: None)

    win = bu.BrowseUpscaledWindow(root, "", str(out), app=app)
    try:
        assert win.grab_current() is None
        # Nor transient: a transient child is auto-hidden with its master, so it
        # would vanish the instant it withdrew the main window.
        assert not win.wm_transient()
    finally:
        win._close()
        root.withdraw()
