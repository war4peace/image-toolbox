"""
Regression test for item 4: batch_upscale._merge_pass_stats must combine the two
passes' stats without a KeyError.

When the rescan finds new files, a second pass runs and the end-of-run summary
calls merge(stats1, stats2). merge()'s per-folder template used to omit the
"skipped_corrupt" key that run_pass records, so `merged[d]["skipped_corrupt"] +=
...` raised KeyError on the first folder — taking down the whole summary (table +
DONE event + MQTT last_run + completion notification) on exactly the runs that had
a second pass. Both templates now come from one factory (_new_folder_stats), and
merge() is a module-level, unit-testable function.

Pure function, no torch/PIL — runs everywhere.
"""

import batch_upscale as bu


def _pass(folders, **totals):
    """Build a run_pass-shaped stats dict. `folders` maps folder path -> the
    non-zero per-folder counters; the rest are filled from the shared factory."""
    fs = {}
    for name, vals in folders.items():
        s = bu._new_folder_stats()
        s.update(vals)
        fs[name] = s
    base = {"total_processed": 0, "total_skipped_done": 0, "total_skipped_size": 0,
            "total_skipped_missing": 0, "total_skipped_corrupt": 0, "total_failed": 0,
            "corrupt_files": []}
    base.update(totals)
    base["folder_stats"] = fs
    return base


def test_factory_has_every_key():
    # The keys run_pass and merge both rely on. If this set changes, both sides
    # change together (that is the whole point of the shared factory).
    s = bu._new_folder_stats()
    for k in ("processed", "skipped_done", "skipped_size", "skipped_missing",
              "skipped_corrupt", "failed", "elapsed"):
        assert k in s


def test_merge_sums_skipped_corrupt_without_keyerror():
    # The exact crash: the same folder appears in both passes with a corrupt skip.
    s1 = _pass({"A": {"processed": 2, "skipped_corrupt": 1}},
               total_processed=2, total_skipped_corrupt=1)
    s2 = _pass({"A": {"processed": 3, "skipped_corrupt": 2}},
               total_processed=3, total_skipped_corrupt=2)

    merged = bu._merge_pass_stats(s1, s2)            # used to raise KeyError

    assert merged["folder_stats"]["A"]["processed"] == 5
    assert merged["folder_stats"]["A"]["skipped_corrupt"] == 3
    assert merged["total_processed"] == 5
    assert merged["total_skipped_corrupt"] == 3


def test_merge_none_returns_first_pass_unchanged():
    s1 = _pass({"A": {"processed": 1}}, total_processed=1)
    assert bu._merge_pass_stats(s1, None) is s1


def test_merge_combines_disjoint_folders_and_corrupt_lists():
    s1 = _pass({"A": {"processed": 1}}, total_processed=1,
               corrupt_files=["a/bad.jpg"])
    s2 = _pass({"B": {"failed": 1}}, total_failed=1,
               corrupt_files=["b/broken.png"])

    merged = bu._merge_pass_stats(s1, s2)

    assert merged["folder_stats"]["A"]["processed"] == 1
    assert merged["folder_stats"]["B"]["failed"] == 1
    assert merged["total_failed"] == 1
    assert merged["corrupt_files"] == ["a/bad.jpg", "b/broken.png"]
