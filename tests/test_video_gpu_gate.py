"""
The remote GPU picker must not offer a card that can't run the selected SeedVR2 job.

A 16 GB card (e.g. RTX 2000 Ada) is physically incapable of a 4K SeedVR2 upscale, yet the
picker used to list it (the list was gated by the QUEUE's floor, not by the target being
added, and the Target combo never re-filtered the card list). VideoTab._seedvr2_gpu_ok is the
rule that fixes it: a card qualifies only when it clears the target's VRAM floor (>= 32 GB, and
the higher per-target floor for 1440p/4K) OR has been successfully benchmarked / already run to
at least that target's output size. Real-ESRGAN is not gated (it tiles on OOM).

These drive the REAL predicate on a minimal fake tab (no tkinter window), pinning the fix so a
future refactor can't silently re-open the "impossible card offered" bug.
"""

import pytest

pytest.importorskip("tkinter")

import db                                   # noqa: E402
import video_estimate as ve                 # noqa: E402
from gui.tab_video import VideoTab           # noqa: E402


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


class FakeVideoTab:
    """Carries just what _seedvr2_gpu_ok touches: a db connection accessor."""
    _seedvr2_gpu_ok = VideoTab._seedvr2_gpu_ok

    def __init__(self, conn):
        self._conn_obj = conn

    def _conn(self):
        return self._conn_obj


def _gpu(name, gb):
    return {"id": name, "name": name, "memory_gb": gb}


# A landscape 1080p source: output at 4K = 3840x2160 (8.29 MP), at 1080p = 1920x1080 (2.07 MP).
SRC_W, SRC_H = 1920, 1080


def test_small_card_rejected_for_4k(db_conn):
    tab = FakeVideoTab(db_conn)
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "4K", SRC_W, SRC_H) is False
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "1080p", SRC_W, SRC_H) is False


def test_32gb_card_passes_1080p_but_not_4k(db_conn):
    tab = FakeVideoTab(db_conn)
    # 1080p floor is 32; a 32 GB card clears it. 4K floor is 90; a 32 GB card does not.
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "1080p", SRC_W, SRC_H) is True
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "4K", SRC_W, SRC_H) is False
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "1440p", SRC_W, SRC_H) is False


def test_big_card_passes_every_target(db_conn):
    tab = FakeVideoTab(db_conn)
    for t in ("1080p", "1440p", "4K"):
        assert tab._seedvr2_gpu_ok(_gpu("H200", 141), t, SRC_W, SRC_H) is True


def test_benchmarked_small_card_is_offered(db_conn):
    """A 24 GB card proven (benchmarked / already run) to a target is offered below the floor."""
    tab = FakeVideoTab(db_conn)
    # No proof yet: a 24 GB card is below the 1080p floor of 32, so it is rejected.
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H) is False
    # Record a successful 1080p probe (2.07 MP output) for this card.
    db.record_bench_probe(db_conn, "RTX 3090", "7b", out_w=1920, out_h=1080, batch=1,
                          outcome="ok", frames=8, seconds=8.0)
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H) is True
    # But it is still NOT offered for 4K, which it has not proven.
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "4K", SRC_W, SRC_H) is False
