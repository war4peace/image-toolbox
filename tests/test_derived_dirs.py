"""
Derived directories must not be re-scanned as input (future-features #16).

The app writes its outputs INSIDE the tree it scans (`<source>/__upscaled__`,
`<source>/__Archive__`). Before this, only Conciliation pruned its own archive, so
after an archive-mode run the Batch Upscaler found every archived original — the
only copies still BELOW the resolution target, therefore all eligible — and
re-upscaled them, billed GPU time on a rented pod.

These tests pin the shared name rule and its use in all four walkers, including
the one property that makes it usable: a user who deliberately points a tool AT an
`__upscaled__` / `__Archive__` folder as their chosen ROOT still gets it scanned.
"""

import os

import pytest

import db
import runner_common as rc


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """An isolated cache.db, so a test never writes memoised hashes into the real one."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def _touch(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ── the shared helper ────────────────────────────────────────────────────────

def test_default_names_cover_every_folder_the_app_creates():
    names = rc.derived_dirnames({})
    assert "__upscaled__" in names          # Batch + Video Upscaler output
    assert "__archive__" in names           # Conciliation archive (lowercased)
    assert ".imgtbx_video" in names         # Video Upscaler work area


def test_config_adds_a_renamed_video_output_subdir():
    names = rc.derived_dirnames({"video": {"output_subdir": "4K Versions"}})
    assert "4k versions" in names
    assert "__upscaled__" in names          # the fixed list still applies


def test_derived_dirnames_never_raises_on_junk_config():
    for junk in ({}, {"video": None}, {"video": {"output_subdir": None}},
                 {"video": {"output_subdir": "   "}}, {"video": {"output_subdir": 7}}):
        assert "__upscaled__" in rc.derived_dirnames(junk)


def test_prune_is_case_insensitive_and_in_place():
    pruner = rc.DerivedPruner({})
    dirs = ["Holidays", "__UPSCALED__", "__archive__", "2019"]
    pruner.prune(dirs)
    assert dirs == ["Holidays", "2019"]     # mutated in place (os.walk honours only that)
    assert pruner.count == 2


def test_extra_names_are_honoured():
    pruner = rc.DerivedPruner({}, extra=["__Archive__", "", None])
    dirs = ["__Archive__", "keep"]
    pruner.prune(dirs)
    assert dirs == ["keep"]


def test_summary_is_none_until_something_is_pruned():
    pruner = rc.DerivedPruner({})
    assert pruner.summary() is None
    pruner.prune(["ordinary"])
    assert pruner.summary() is None
    pruner.prune(["__Archive__"])
    assert "__Archive__" in pruner.summary()


def test_summary_counts_folders_but_lists_distinct_names():
    pruner = rc.DerivedPruner({})
    for _ in range(3):
        pruner.prune(["__Archive__"])
    s = pruner.summary()
    assert "3 folder(s)" in s
    assert s.count("__Archive__") == 1


# ── Batch Upscaler ───────────────────────────────────────────────────────────

def test_upscaler_skips_the_archive_and_a_previous_output_tree(tmp_path):
    """The money bug: archived originals are below the target, so they are all
    eligible again. A second run writing elsewhere must also skip run one's tree."""
    bu = pytest.importorskip("batch_upscale")
    src = str(tmp_path / "Poze")
    _touch(os.path.join(src, "2019", "a.jpg"))
    _touch(os.path.join(src, "__Archive__", "2019", "b.jpg"))        # conciliated original
    _touch(os.path.join(src, "__upscaled__", "2019", "c.jpg"))       # an earlier run's output
    _touch(os.path.join(src, ".imgtbx_video", "seg.jpg"))            # video work area

    items, _folders = bu.collect_work_items(src, str(tmp_path / "out2"))
    rels = {os.path.relpath(p, src) for _d, p, _od, _on in items}
    assert rels == {os.path.join("2019", "a.jpg")}


def test_upscaler_still_scans_an_archive_chosen_as_the_root(tmp_path):
    """Prune SUBdirectories, never refuse the root: a user pointing the tool at an
    __Archive__ folder on purpose must still get it scanned."""
    bu = pytest.importorskip("batch_upscale")
    root = str(tmp_path / "Poze" / "__Archive__")
    _touch(os.path.join(root, "2019", "b.jpg"))

    items, _folders = bu.collect_work_items(root, str(tmp_path / "out"))
    rels = {os.path.relpath(p, root) for _d, p, _od, _on in items}
    assert rels == {os.path.join("2019", "b.jpg")}


# ── Tag & Rename ─────────────────────────────────────────────────────────────

def test_tag_skips_derived_dirs_but_scans_one_chosen_as_the_root(tmp_path, monkeypatch):
    tr = pytest.importorskip("tag_and_rename")
    src = str(tmp_path / "Poze")
    _touch(os.path.join(src, "a.jpg"))
    _touch(os.path.join(src, "__upscaled__", "a.jpg"))
    _touch(os.path.join(src, "__Archive__", "a.jpg"))

    # force_tag skips the dimension read, so empty files are enough here.
    got = {os.path.relpath(p, src) for p in tr.collect_work_items(src, force_tag=True)}
    assert got == {"a.jpg"}

    up = os.path.join(src, "__upscaled__")
    got_up = {os.path.relpath(p, up) for p in tr.collect_work_items(up, force_tag=True)}
    assert got_up == {"a.jpg"}


# ── Video Upscaler ───────────────────────────────────────────────────────────

def test_video_walk_skips_the_archive(tmp_path):
    bv = pytest.importorskip("batch_video_upscale")
    src = str(tmp_path / "Filme")
    _touch(os.path.join(src, "holiday.avi"))
    _touch(os.path.join(src, "__Archive__", "holiday.avi"))          # conciliated original

    rels = {rel for _ap, rel in bv.iter_videos(src)}
    assert rels == {"holiday.avi"}


def test_video_walk_reports_what_it_pruned(tmp_path):
    bv = pytest.importorskip("batch_video_upscale")
    src = str(tmp_path / "Filme")
    _touch(os.path.join(src, "a.mp4"))
    _touch(os.path.join(src, "__Archive__", "a.mp4"))

    pruner = rc.DerivedPruner()
    list(bv.iter_videos(src, pruner=pruner))
    assert "__Archive__" in pruner.summary()


# ── Conciliation ─────────────────────────────────────────────────────────────

def test_conciliation_skips_a_nested_archive(tmp_path):
    """The old check was a single `<original_root>/__Archive__` path compare, so an
    archive created deeper in the tree (a per-folder conciliation) was walked."""
    cc = pytest.importorskip("conciliate")
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    os.makedirs(proc, exist_ok=True)
    _touch(os.path.join(orig, "2019", "a.jpg"), b"a")
    _touch(os.path.join(orig, "2019", "__Archive__", "old.jpg"), b"old")

    _plan, folders, _kept, _v = cc.build_plan(orig, proc, tr_index=None, conn=None)
    scanned = {rel for rel, _r, _s, _k in folders}
    assert not any("__Archive__" in r for r in scanned)


def test_conciliation_hash_index_skips_the_video_work_area(tmp_path, db_conn):
    """`.imgtbx_video` holds every per-segment intermediate. Hashing multi-GB
    segments is pure waste, and a segment must never become a match candidate."""
    cc = pytest.importorskip("conciliate")
    proc = str(tmp_path / "proc")
    _touch(os.path.join(proc, "a_4K.mp4"), b"real output")
    _touch(os.path.join(proc, ".imgtbx_video", "a_ab12", "up", "seg_00000.mkv"), b"segment")

    index = cc.build_processed_hash_index(proc, db_conn)
    assert all("imgtbx_video" not in p for p in index.values())
    assert any(p.endswith("a_4K.mp4") for p in index.values())
