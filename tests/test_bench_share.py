"""
Benchmark sharing (future-features #8): the CSV serializer round-trip, the model-key
decomposition, the summary-row builder over real probe rows, and the contribution
issue-URL (inline vs attach fallback). All GPU-free and Tk-free (the issue-URL helper
only needs urllib); build_share_rows runs against an isolated temp cache.db.
"""

import pytest

import db
import bench_share as bs
import video_benchmark as vb


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


# ── CSV round-trip ───────────────────────────────────────────────────────────

def _row(**over):
    base = {"gpu_id": "RTX 3090", "run_on": "local", "model": "7b", "compile": "OFF",
            "tile": "off", "target": "1080p", "out_w": 1920, "out_h": 1080,
            "max_batch": 13, "used_batch": 9, "overlap": 6, "spf": 1.2345,
            "peak_vram": 18.7, "free_vram": 22.0, "price_usd_hr": None,
            "source": "local", "date": "2026-07-18"}
    base.update(over)
    return base


def test_write_read_roundtrip(tmp_path):
    p = str(tmp_path / "b.csv")
    assert bs.write_csv(p, [_row()]) == 1
    back = bs.read_csv(p)
    assert len(back) == 1
    assert back[0]["gpu_id"] == "RTX 3090"
    assert back[0]["compile"] == "OFF"
    assert back[0]["out_w"] == "1920"          # read is text; consumers coerce


def test_written_file_starts_with_version_sentinel(tmp_path):
    p = str(tmp_path / "b.csv")
    bs.write_csv(p, [_row()])
    first = open(p, encoding="utf-8").readline().strip()
    assert first == bs.SENTINEL
    assert first == "# imgtbx-bench v1"


def test_none_becomes_blank_cell(tmp_path):
    p = str(tmp_path / "b.csv")
    bs.write_csv(p, [_row(price_usd_hr=None)])
    assert bs.read_csv(p)[0]["price_usd_hr"] == ""


def test_read_skips_rows_missing_required_fields(tmp_path):
    p = str(tmp_path / "b.csv")
    bs.write_csv(p, [_row(), _row(gpu_id=""), _row(max_batch="")])
    assert len(bs.read_csv(p)) == 1            # only the complete row survives


def test_read_missing_file_is_empty_not_raising(tmp_path):
    assert bs.read_csv(str(tmp_path / "nope.csv")) == []


def test_read_tolerates_extra_and_missing_columns(tmp_path):
    # A future/foreign CSV with an unknown column and a dropped optional one must not break.
    p = str(tmp_path / "b.csv")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(bs.SENTINEL + "\n")
        fh.write("gpu_id,model,out_w,out_h,max_batch,some_future_col\n")
        fh.write("RTX 4090,7b,1920,1080,13,whatever\n")
    rows = bs.read_csv(p)
    assert len(rows) == 1
    assert rows[0]["gpu_id"] == "RTX 4090"
    assert rows[0]["price_usd_hr"] == ""       # absent column defaults blank


def test_to_text_matches_write_csv(tmp_path):
    p = str(tmp_path / "b.csv")
    bs.write_csv(p, [_row()])
    # newline="" so the csv module's \r\n terminators are not translated on read,
    # matching to_text's in-memory StringIO output byte for byte.
    assert bs.to_text([_row()]) == open(p, newline="", encoding="utf-8").read()


# ── model-key decomposition ──────────────────────────────────────────────────

@pytest.mark.parametrize("key,expect", [
    ("7b",              ("7b", False, "off")),
    ("7b|c",            ("7b", True,  "off")),
    ("7b|cd",           ("7b", True,  "off")),
    ("3b_fp16",         ("3b_fp16", False, "off")),
    ("7b|td1024|c",     ("7b", True,  "d1024")),
    ("7b|te1024_d512",  ("7b", False, "e1024_d512")),
])
def test_decompose_bench_model(key, expect):
    assert vb._decompose_bench_model(key) == expect


# ── summary-row builder over real probe rows ─────────────────────────────────

def test_build_share_rows_summarises_ceiling_and_compile(db_conn):
    gpu = "RTX 3090"
    # Uncompiled cell: ceiling 13, but 9 is throughput-optimal (same speed, smaller window).
    for b, secs in ((5, 60.0), (9, 90.0), (13, 300.0)):
        db.record_bench_probe(db_conn, gpu, "7b", 1920, 1080, b, "ok",
                              frames=b, seconds=secs, peak_alloc=18.0, free_vram=22.0)
    db.record_bench_probe(db_conn, gpu, "7b", 1920, 1080, 17, "oom")
    # A compiled cell under the regime-tagged key.
    db.record_bench_probe(db_conn, gpu, "7b|c", 1920, 1080, 5, "ok",
                          frames=5, seconds=40.0, peak_alloc=19.0, free_vram=22.0)

    rows = vb.build_share_rows(db_conn, gpu, run_on="local")
    by_compile = {r["compile"]: r for r in rows}
    assert set(by_compile) == {"OFF", "ON"}
    off = by_compile["OFF"]
    assert off["max_batch"] == 13               # raw ceiling
    assert off["used_batch"] == 9               # throughput-optimal, below the ceiling
    assert off["target"] == "1080p"
    assert off["run_on"] == "local"
    assert off["source"] == "local"
    assert off["tile"] == "off"
    assert by_compile["ON"]["compile"] == "ON"


def test_build_share_rows_skips_cells_with_no_fit(db_conn):
    gpu = "RTX 3090"
    db.record_bench_probe(db_conn, gpu, "7b", 3840, 2160, 5, "oom")   # 4K never fit
    assert vb.build_share_rows(db_conn, gpu, run_on="local") == []


def test_build_share_rows_stamps_remote_price(db_conn):
    gpu = "NVIDIA A100 80GB PCIe"
    # Compiled key: remote is compile-ON only, so an ON row is what a pod contributes.
    db.record_bench_probe(db_conn, gpu, "7b|c", 1920, 1080, 9, "ok",
                          frames=9, seconds=30.0, peak_alloc=40.0)
    rows = vb.build_share_rows(db_conn, gpu, run_on="remote", price_usd_hr=1.39)
    assert rows[0]["run_on"] == "remote"
    assert rows[0]["compile"] == "ON"
    assert rows[0]["price_usd_hr"] == 1.39


def test_remote_drops_compile_off_but_local_keeps_it(db_conn):
    # A card with BOTH regimes: '7b' (OFF) and '7b|c' (ON).
    gpu = "RTX 3090"
    db.record_bench_probe(db_conn, gpu, "7b", 1920, 1080, 9, "ok",
                          frames=9, seconds=30.0, peak_alloc=20.0)
    db.record_bench_probe(db_conn, gpu, "7b|c", 1920, 1080, 5, "ok",
                          frames=5, seconds=20.0, peak_alloc=21.0)
    # Remote pods run compile ON only: the OFF regime is dropped (incl. old mislabelled '7b').
    remote = vb.build_share_rows(db_conn, gpu, run_on="remote")
    assert {r["compile"] for r in remote} == {"ON"}
    # Local keeps both.
    local = vb.build_share_rows(db_conn, gpu, run_on="local")
    assert {r["compile"] for r in local} == {"ON", "OFF"}


def test_build_share_rows_empty_for_unknown_card(db_conn):
    assert vb.build_share_rows(db_conn, "No Such GPU", run_on="local") == []


def test_bench_gpu_ids_lists_all_cards_regardless_of_stock(db_conn):
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 9, "ok", frames=9, seconds=30.0)
    db.record_bench_probe(db_conn, "NVIDIA A100 80GB PCIe", "7b|c", 1920, 1080, 9, "ok",
                          frames=9, seconds=30.0)
    assert db.bench_gpu_ids(db_conn) == ["NVIDIA A100 80GB PCIe", "RTX 3090"]


def test_infer_run_on_matches_local_else_remote():
    assert vb.infer_run_on("NVIDIA GeForce RTX 3090",
                           local_name="NVIDIA GeForce RTX 3090") == "local"
    assert vb.infer_run_on("RTX 3090", local_name="NVIDIA GeForce RTX 3090") == "local"
    assert vb.infer_run_on("NVIDIA A100 80GB PCIe",
                           local_name="NVIDIA GeForce RTX 3090") == "remote"
    assert vb.infer_run_on("NVIDIA A100 80GB PCIe", local_name=None) == "remote"


# ── contribution issue URL (inline vs attach fallback) ───────────────────────

def test_contribute_inline_then_attach(monkeypatch):
    import gui.common as gc
    cap = {}
    monkeypatch.setattr(gc.webbrowser, "open", lambda u: cap.update(url=u) or True)

    assert gc.contribute_benchmark("RTX 3090", "small,csv,text", "C:/logs/x.csv") == "inline"
    assert "labels=benchmark" in cap["url"]

    big = "row," * 4000
    assert gc.contribute_benchmark("RTX 3090", big, "C:/logs/x.csv") == "attach"
    assert len(cap["url"]) < gc._MAX_ISSUE_URL


def test_contribute_failed_when_browser_raises(monkeypatch):
    import gui.common as gc

    def boom(_u):
        raise RuntimeError("no browser")
    monkeypatch.setattr(gc.webbrowser, "open", boom)
    assert gc.contribute_benchmark("RTX 3090", "x", "C:/logs/x.csv") == "failed"


# ── maintainer merge tool (sanity gate, dedupe, master export) ────────────────

def test_sanity_check_accepts_a_plausible_row():
    assert bs.sanity_check(_row()) is None


@pytest.mark.parametrize("over,frag", [
    ({"out_w": "0"},              "output size"),
    ({"out_w": "20000"},         "output size"),
    ({"max_batch": "0"},         "max_batch"),
    ({"max_batch": "9999"},      "max_batch"),
    ({"max_batch": "9.5"},       "max_batch"),      # non-integer ceiling
    ({"max_batch": "13", "used_batch": "20"}, "used_batch"),  # above the ceiling
    ({"spf": "0"},               "spf"),
    ({"spf": "5000"},            "spf"),
    ({"peak_vram": "500"},       "peak_vram"),      # over the absolute ceiling
    ({"gpu_id": "NVIDIA GeForce RTX 3090", "peak_vram": "40"}, "capacity"),  # 24 GB card
    ({"overlap": "-2"},          "overlap"),
])
def test_sanity_check_rejects_impossible(over, frag):
    reason = bs.sanity_check(_row(**over))
    assert reason and frag in reason


def test_sanity_check_tolerates_blank_optionals():
    # An import may carry only the required fields; blanks must not be rejected.
    assert bs.sanity_check(_row(used_batch="", spf="", peak_vram="", overlap="")) is None


def test_card_vram_specific_beats_generic():
    assert bs._card_vram("NVIDIA A100 80GB PCIe") == 80
    assert bs._card_vram("NVIDIA A100-PCIE-40GB") == 40
    assert bs._card_vram("NVIDIA GeForce RTX 3090") == 24
    assert bs._card_vram("Some Unknown Card") is None


def test_merge_dedupes_newest_date_wins(tmp_path):
    db_path = str(tmp_path / "work.db")
    master = str(tmp_path / "master.csv")
    older = str(tmp_path / "old.csv")
    newer = str(tmp_path / "new.csv")
    bs.write_csv(older, [_row(max_batch=9, date="2026-01-01")])
    bs.write_csv(newer, [_row(max_batch=13, date="2026-07-01")])

    # Feed the NEWER first, then the OLDER: newest date must still win regardless of order.
    rep = bs.merge_files([newer, older], master, db_path)
    assert rep["master_rows"] == 1
    out = bs.read_csv(master)
    assert len(out) == 1
    assert out[0]["max_batch"] == "13"


def test_merge_accumulates_across_sessions(tmp_path):
    db_path = str(tmp_path / "work.db")
    master = str(tmp_path / "master.csv")
    a = str(tmp_path / "a.csv")
    b = str(tmp_path / "b.csv")
    bs.write_csv(a, [_row(gpu_id="RTX 3090", target="1080p")])
    bs.write_csv(b, [_row(gpu_id="NVIDIA A100 80GB PCIe", run_on="remote",
                          compile="ON", model="7b", target="4K",
                          out_w=3840, out_h=2160, price_usd_hr=1.39)])

    bs.merge_files([a], master, db_path)
    rep = bs.merge_files([b], master, db_path)      # second session, same DB
    assert rep["master_rows"] == 2                  # first card persisted, second added
    gpus = {r["gpu_id"] for r in bs.read_csv(master)}
    assert gpus == {"RTX 3090", "NVIDIA A100 80GB PCIe"}


def test_merge_reports_rejections_and_keeps_good_rows(tmp_path):
    db_path = str(tmp_path / "work.db")
    master = str(tmp_path / "master.csv")
    sub = str(tmp_path / "sub.csv")
    bs.write_csv(sub, [_row(target="1080p"),
                       _row(target="4K", out_w=3840, out_h=2160, max_batch=9999)])  # bad ceiling
    rep = bs.merge_files([sub], master, db_path)
    assert rep["accepted"] == 1
    assert rep["rejected"] == 1
    assert rep["master_rows"] == 1
    assert rep["files"][0]["reasons"][0][1]         # a reason string is recorded


def test_merge_master_is_sorted_deterministically(tmp_path):
    db_path = str(tmp_path / "work.db")
    master = str(tmp_path / "master.csv")
    sub = str(tmp_path / "sub.csv")
    # Deliberately out of order: two cards, two targets.
    bs.write_csv(sub, [
        _row(gpu_id="RTX 3090", target="4K", out_w=3840, out_h=2160),
        _row(gpu_id="NVIDIA A100 80GB PCIe", run_on="remote", compile="ON", target="1080p"),
        _row(gpu_id="RTX 3090", target="1080p"),
    ])
    bs.merge_files([sub], master, db_path)
    out = bs.read_csv(master)
    ordered = [(r["gpu_id"], int(r["out_w"])) for r in out]
    assert ordered == sorted(ordered)               # gpu_id then out_w ascending


# ── download side: fetch_community (mocked network) ──────────────────────────

def test_fetch_community_parses_and_caches(tmp_path, monkeypatch):
    import io
    import urllib.request
    csv_text = bs.to_text([_row()])

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp(csv_text.encode("utf-8")))
    dest = str(tmp_path / "cached.csv")
    rows = bs.fetch_community(dest=dest)
    assert len(rows) == 1 and rows[0]["gpu_id"] == "RTX 3090"
    assert bs.read_csv(dest)                          # the raw text was cached to disk


def test_fetch_community_failsafe_on_error(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert bs.fetch_community() == []                 # never raises, empty on failure


# ── download side: import into the cache (video_bench + learned + precedence) ─

def _import_row(**over):
    base = {"gpu_id": "RTX 3090", "run_on": "local", "model": "7b", "compile": "OFF",
            "tile": "off", "target": "1080p", "out_w": 1920, "out_h": 1080,
            "max_batch": "13", "used_batch": "9", "overlap": "6", "spf": "1.2",
            "peak_vram": "18.7", "free_vram": "22", "price_usd_hr": "", "source": "local",
            "date": "2026-07-18"}
    base.update(over)
    return base


@pytest.mark.parametrize("base,compile_on,tile,expect", [
    ("7b", False, "off", "7b"),
    ("7b", True, "off", "7b|c"),
    ("7b", True, "d1024", "7b|td1024|c"),
    ("3b_fp16", False, "e1024_d512", "3b_fp16|te1024_d512"),
])
def test_compose_bench_model(base, compile_on, tile, expect):
    assert vb._compose_bench_model(base, compile_on, tile) == expect


def test_compose_decompose_roundtrip():
    for key in ("7b", "7b|c", "7b|td1024|c", "3b", "3b_fp16|te1024_d512"):
        b, con, tile = vb._decompose_bench_model(key)
        assert vb._compose_bench_model(b, con, tile) == key   # compose uses '|c' (never '|cd')


def test_import_rows_writes_bench_and_learned(db_conn):
    import video_vram_sizer as sizer
    imp, skp = vb.import_rows([_import_row()], db_conn)
    assert (imp, skp) == (1, 0)
    batches = {r["batch"]: r for r in db.get_bench_probes(db_conn, "RTX 3090", "7b")}
    assert set(batches) == {9, 13}                    # used + ceiling probes
    assert batches[9]["seconds"] and batches[9]["frames"] == 9   # timing on the used probe
    assert batches[13]["seconds"] is None                        # ceiling has no timing
    assert db_conn.execute("SELECT DISTINCT source FROM video_bench").fetchone()[0] == "imported"
    got = db.get_learned_batch_ge(db_conn, "RTX 3090|7b", sizer.mp_bucket(1920 * 1080 / 1e6))
    assert got == 9                                   # learned batch seeded at the used batch


def test_import_compiled_row_key_has_compile_tag(db_conn):
    vb.import_rows([_import_row(compile="ON")], db_conn)
    assert db.bench_models(db_conn, "RTX 3090") == ["7b|c"]


def test_import_local_precedence_skips_measured_cell(db_conn):
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 9, "ok",
                          frames=9, seconds=30.0, peak_alloc=20.0)     # user's own measurement
    imp, skp = vb.import_rows([_import_row(max_batch="13", used_batch="13")], db_conn)
    assert (imp, skp) == (0, 1)
    rows = db.get_bench_probes(db_conn, "RTX 3090", "7b")
    assert [r["batch"] for r in rows] == [9]         # untouched
    assert db_conn.execute("SELECT source FROM video_bench").fetchone()[0] == "local"


def test_reimport_replaces_stale_imported_probes(db_conn):
    vb.import_rows([_import_row(used_batch="9", max_batch="13")], db_conn)
    imp, skp = vb.import_rows([_import_row(used_batch="11", max_batch="17")], db_conn)
    assert (imp, skp) == (1, 0)
    assert {r["batch"] for r in db.get_bench_probes(db_conn, "RTX 3090", "7b")} == {11, 17}


def test_import_share_csv_from_disk(tmp_path, db_conn):
    p = str(tmp_path / "share.csv")
    bs.write_csv(p, [_import_row(), _import_row(target="4K", out_w=3840, out_h=2160,
                                               max_batch="5", used_batch="5")])
    imp, skp = vb.import_share_csv(p, db_conn)
    assert (imp, skp) == (2, 0)
    assert len(db.bench_gpu_ids(db_conn)) == 1


# ── startup auto-update (community first, shipped-CSV fallback offline) ───────

def test_auto_update_prefers_community(db_conn, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "fetch_community", lambda **k: [_import_row()])   # network up
    local = str(tmp_path / "local.csv")
    bs.write_csv(local, [_import_row(target="4K", out_w=3840, out_h=2160)])   # a DIFFERENT cell
    imp, skp, src = vb.auto_update(conn=db_conn, local_csv=local)
    assert src == "community" and imp == 1
    # Only the community cell (1080p) landed; the local file was not consulted.
    assert {r["out_w"] for r in db.get_bench_probes(db_conn, "RTX 3090", "7b")} == {1920}


def test_auto_update_falls_back_to_local_when_offline(db_conn, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "fetch_community", lambda **k: [])                # offline / failed
    local = str(tmp_path / "local.csv")
    bs.write_csv(local, [_import_row()])
    imp, skp, src = vb.auto_update(conn=db_conn, local_csv=local)
    assert src == "local" and imp == 1


def test_auto_update_none_when_nothing_available(db_conn, monkeypatch):
    monkeypatch.setattr(bs, "fetch_community", lambda **k: [])
    assert vb.auto_update(conn=db_conn, local_csv=None) == (0, 0, "none")


# ── contribution dedup against the published corpus (new_rows) ────────────────

def test_new_rows_drops_identical_keeps_new():
    existing = [_row(target="1080p")]
    same = _row(target="1080p")
    fresh = _row(target="4K", out_w=3840, out_h=2160, max_batch=5, used_batch=5)
    assert bs.new_rows([same, fresh], existing) == [fresh]


def test_new_rows_ignores_volatile_fields():
    # Same measurement, different date / price / free_vram: recognised as already present.
    existing = [_row(price_usd_hr=1.39, date="2026-07-18", free_vram=80.0)]
    cand = [_row(price_usd_hr=2.50, date="2026-07-19", free_vram=60.0)]
    assert bs.new_rows(cand, existing) == []


def test_new_rows_keeps_changed_measurement():
    # A re-measured cell with a different ceiling is genuinely new data, so it is kept.
    assert bs.new_rows([_row(max_batch=17)], [_row(max_batch=13)]) == [_row(max_batch=17)]


def test_new_rows_empty_existing_keeps_all():
    cand = [_row(), _row(target="4K", out_w=3840, out_h=2160)]
    assert bs.new_rows(cand, []) == cand


# ── build_share_rows contributes only locally-measured cells ─────────────────

def test_build_share_rows_excludes_imported_cells(db_conn):
    gpu = "RTX 3090"
    db.record_bench_probe(db_conn, gpu, "7b", 1920, 1080, 9, "ok",
                          frames=9, seconds=30.0, peak_alloc=20.0)          # LOCAL measurement
    vb.import_rows([_import_row(gpu_id=gpu, target="4K", out_w=3840, out_h=2160,
                               max_batch="5", used_batch="5")], db_conn)    # IMPORTED community cell
    rows = vb.build_share_rows(db_conn, gpu, run_on="local")
    assert {(int(r["out_w"]), int(r["out_h"])) for r in rows} == {(1920, 1080)}


def test_build_share_rows_empty_for_import_only_card(db_conn):
    vb.import_rows([_import_row(gpu_id="NVIDIA A100 80GB PCIe", run_on="remote",
                               compile="ON")], db_conn)
    assert vb.build_share_rows(db_conn, "NVIDIA A100 80GB PCIe", run_on="remote") == []
