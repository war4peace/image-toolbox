"""
Roadmap #18: Conciliation Undo.

Conciliation is the app's only destructive tool and was the only one with no undo
record. A Run now journals every file action to db.conc_runs / conc_actions BEFORE
performing it, and `--undo` reverses an ARCHIVE run: the processed files go back to
the processed tree and the originals come back out of __Archive__.

The rules these tests pin down, in order of how much damage getting them wrong
would do:

  * Undo NEVER overwrites. A file changed since the run, or a name something else
    now occupies, is a conflict: reported, and the pair left exactly as it was.
  * A DELETE run is refused, not attempted. The bytes are gone; the journal is only
    evidence of what was removed.
  * Undo works from the DISK, not from a row's status, so an interrupted run (a
    'pending' row, one of the two moves done) unwinds correctly, and re-running an
    undo is harmless.
  * Recording is fail-safe and free: a journal failure never aborts a conciliation,
    and no file is read to record it.

Small text files stand in for photos throughout: conciliation is pure file I/O.
"""

import os
import time

import pytest

import db
import conciliate as cc


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


class _FakeLog:
    """Logger stand-in that keeps the lines, so a test can assert on what the user
    was told (a conflict the run does not REPORT is as bad as one it ignores)."""

    def __init__(self):
        self.lines = []

    def tee(self, msg=""):
        self.lines.append(msg)

    def log_only(self, msg=""):
        self.lines.append(msg)

    def close(self):
        pass

    @property
    def text(self):
        return "\n".join(self.lines)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return path


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _pair(tmp_path, name="beach.jpg", proc_name=None, folder="2006"):
    """One original + its processed counterpart, in mirrored trees. Returns
    (original_root, processed_root, plan) ready for execute()."""
    orig_root = str(tmp_path / "orig")
    proc_root = str(tmp_path / "orig" / "__upscaled__")
    o_rel = os.path.join(folder, name)
    o_abs = _write(os.path.join(orig_root, o_rel), b"the-original-photo")
    p_abs = _write(os.path.join(proc_root, folder, proc_name or name),
                   b"the-upscaled-photo-which-is-bigger")
    return orig_root, proc_root, [(o_abs, p_abs, o_rel)]


def _run(conn, orig_root, proc_root, plan, mode="archive", log=None):
    """Perform a conciliation with the undo journal armed, as main() does."""
    log = log or _FakeLog()
    rec = cc.UndoRecorder(conn, orig_root, proc_root, mode, log)
    rec.begin()
    result = cc.execute(plan, orig_root, mode, log, recorder=rec)
    rec.finish()
    return rec, result, log


# ── the journal ──────────────────────────────────────────────────────────────

def test_a_run_is_journalled_before_it_touches_anything(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    rec, (done, _c, _e, _r), _log = _run(db_conn, orig_root, proc_root, plan)

    assert done == 1
    run = db.latest_conc_run(db_conn, orig_root)
    assert run is not None
    assert run["mode"] == "archive"
    assert run["schema_version"] == db.CONC_UNDO_SCHEMA
    assert run["finished_at"]
    rows = db.get_conc_actions(db_conn, run["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "archived"
    assert row["status"] == "done"
    assert row["original_path"] == plan[0][0]
    assert row["processed_path"] == plan[0][1]
    assert os.path.basename(os.path.dirname(row["archive_path"])) == "2006"
    assert cc.ARCHIVE_DIRNAME in row["archive_path"]
    # Both fingerprints were taken while the files were still in place.
    assert row["orig_size"] == len(b"the-original-photo")
    assert row["proc_size"] == len(b"the-upscaled-photo-which-is-bigger")


def test_the_run_is_found_however_the_folder_is_spelled(db_conn, tmp_path):
    # The GUI hands over whatever the user typed; the journal is path-normalised.
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)
    assert db.latest_conc_run(db_conn, orig_root + os.sep) is not None
    assert db.latest_conc_run(db_conn, orig_root.upper()) is not None


def test_a_recorded_hash_is_only_the_memoised_one(db_conn, tmp_path):
    # Recording runs on EVERY conciliation, so it must never read a file: the hash
    # is stored when db already has one (the lineage matching just hashed the
    # original) and left NULL otherwise.
    orig_root, proc_root, plan = _pair(tmp_path)
    db.hash_file_cached(db_conn, plan[0][0])       # as build_plan's matching does
    db_conn.commit()
    _run(db_conn, orig_root, proc_root, plan)

    row = db.get_conc_actions(db_conn, db.latest_conc_run(db_conn, orig_root)["id"])[0]
    assert row["orig_hash"]                        # memoised -> recorded
    assert row["proc_hash"] is None                # never hashed -> not read now


def test_cached_hash_never_reads_the_file(db_conn, tmp_path):
    p = _write(str(tmp_path / "x.bin"), b"abc")
    assert db.cached_hash(db_conn, p) is None      # nothing memoised yet
    digest = db.hash_file_cached(db_conn, p)
    db_conn.commit()
    assert db.cached_hash(db_conn, p) == digest
    _write(p, b"xyz")                              # changed -> the memo is stale
    assert db.cached_hash(db_conn, p) is None


def test_a_journal_failure_never_aborts_the_conciliation(db_conn, tmp_path):
    """The journal is a safety net, not a precondition. Losing it costs the undo;
    refusing to run would cost the user the thing they came for."""
    orig_root, proc_root, plan = _pair(tmp_path)
    log = _FakeLog()

    class _BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("disk full")

    rec = cc.UndoRecorder(_BrokenConn(), orig_root, proc_root, "archive", log)
    rec.begin()
    done, _c, errors, _r = cc.execute(plan, orig_root, "archive", log, recorder=rec)

    assert (done, errors) == (1, 0)                # the run completed
    assert not rec.enabled
    assert "will not be undoable" in log.text      # and said so, once, loudly


# ── the undo round trip ──────────────────────────────────────────────────────

def test_undo_puts_both_files_back(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    assert not os.path.exists(p_abs)               # moved into the original tree
    assert _read(o_abs) == b"the-upscaled-photo-which-is-bigger"

    log = _FakeLog()
    undone, conflicts, errors, reason = cc.run_undo(orig_root, log)

    assert (undone, conflicts, errors, reason) == (1, 0, 0, None)
    assert _read(o_abs) == b"the-original-photo"   # the original is back
    assert _read(p_abs) == b"the-upscaled-photo-which-is-bigger"
    assert not os.path.exists(os.path.join(orig_root, cc.ARCHIVE_DIRNAME, "2006",
                                           "beach.jpg"))


def test_undo_puts_back_a_renamed_processed_file(db_conn, tmp_path):
    # Tag & Rename gives the processed file a different name, so it does NOT take
    # the original's name and both files coexist in the tree for a moment.
    orig_root, proc_root, plan = _pair(tmp_path, proc_name="beach_Sunset_Over_Sea.jpg")
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    moved_in = os.path.join(orig_root, "2006", "beach_Sunset_Over_Sea.jpg")
    assert os.path.isfile(moved_in) and not os.path.exists(o_abs)

    undone, conflicts, errors, reason = cc.run_undo(orig_root, _FakeLog())

    assert (undone, conflicts, errors, reason) == (1, 0, 0, None)
    assert not os.path.exists(moved_in)
    assert _read(o_abs) == b"the-original-photo"
    assert _read(p_abs) == b"the-upscaled-photo-which-is-bigger"


def test_undo_removes_the_emptied_archive_folders(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)
    assert os.path.isdir(os.path.join(orig_root, cc.ARCHIVE_DIRNAME))

    cc.run_undo(orig_root, _FakeLog())

    assert not os.path.exists(os.path.join(orig_root, cc.ARCHIVE_DIRNAME))


def test_undo_marks_the_run_so_it_is_not_offered_twice(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)
    cc.run_undo(orig_root, _FakeLog())

    assert db.latest_conc_run(db_conn, orig_root) is None
    assert db.latest_conc_run(db_conn, orig_root, include_undone=True) is not None


def test_undo_is_idempotent(db_conn, tmp_path):
    """Re-running an undo (a second click, a retry after a partial one) must be a
    no-op, not a second set of moves."""
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    cc.run_undo(orig_root, _FakeLog())

    run = db.latest_conc_run(db_conn, orig_root, include_undone=True)
    rows = db.get_conc_actions(db_conn, run["id"], statuses=None)
    undone, conflicts, errors, _r = cc.run_undo(orig_root, _FakeLog())

    assert (conflicts, errors) == (0, 0)
    assert undone == 0                             # nothing left to move
    assert rows[0]["status"] == "undone"
    assert _read(o_abs) == b"the-original-photo"
    assert _read(p_abs) == b"the-upscaled-photo-which-is-bigger"


def test_undo_completes_an_interrupted_pair(db_conn, tmp_path):
    """A crash between the two moves leaves a 'pending' row and a half-moved pair.
    Undo reads the DISK, not the status, so it finishes the reversal."""
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    arch = os.path.join(orig_root, cc.ARCHIVE_DIRNAME, "2006", "beach.jpg")

    rec = cc.UndoRecorder(db_conn, orig_root, proc_root, "archive", _FakeLog())
    rec.begin()
    rec.record("archived", o_abs, arch, p_abs,
               os.path.join(orig_root, "2006", "beach.jpg"))
    os.makedirs(os.path.dirname(arch), exist_ok=True)
    os.rename(o_abs, arch)                         # ... and then the power went out

    undone, conflicts, errors, reason = cc.run_undo(orig_root, _FakeLog())

    assert (undone, conflicts, errors, reason) == (1, 0, 0, None)
    assert _read(o_abs) == b"the-original-photo"
    assert _read(p_abs) == b"the-upscaled-photo-which-is-bigger"


# ── undo refuses rather than guesses ─────────────────────────────────────────

def test_undo_refuses_a_processed_file_edited_since_the_run(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    _write(o_abs, b"the-user-edited-this-photo-afterwards")   # different size

    log = _FakeLog()
    undone, conflicts, errors, reason = cc.run_undo(orig_root, log)

    assert (undone, conflicts, errors, reason) == (0, 1, 0, None)
    assert "has changed since the run" in log.text
    assert _read(o_abs) == b"the-user-edited-this-photo-afterwards"   # untouched
    assert not os.path.exists(p_abs)               # and NOTHING was half-moved
    assert os.path.isfile(os.path.join(orig_root, cc.ARCHIVE_DIRNAME, "2006",
                                       "beach.jpg"))


def test_undo_catches_a_same_size_replacement_by_hash(db_conn, tmp_path):
    """The (size, mtime) gate is the cheap check; the recorded content hash is what
    catches a deliberate replacement that kept both."""
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    db.hash_file_cached(db_conn, p_abs)            # memoise, so a hash is recorded
    db_conn.commit()
    _run(db_conn, orig_root, proc_root, plan)

    st = os.stat(o_abs)
    _write(o_abs, b"THE-UPSCALED-PHOTO-WHICH-IS-BIGGER")      # same length
    os.utime(o_abs, (st.st_atime, st.st_mtime))               # same timestamp

    undone, conflicts, errors, _r = cc.run_undo(orig_root, _FakeLog())
    assert (undone, conflicts, errors) == (0, 1, 0)
    assert _read(o_abs) == b"THE-UPSCALED-PHOTO-WHICH-IS-BIGGER"


def test_undo_refuses_a_changed_archived_original(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    arch = os.path.join(orig_root, cc.ARCHIVE_DIRNAME, "2006", "beach.jpg")
    _write(arch, b"somebody-swapped-the-archived-original")

    undone, conflicts, errors, _r = cc.run_undo(orig_root, _FakeLog())

    assert (undone, conflicts, errors) == (0, 1, 0)
    # The processed file is still in the original tree: neither half ran.
    assert _read(o_abs) == b"the-upscaled-photo-which-is-bigger"
    assert not os.path.exists(p_abs)


def test_undo_refuses_when_the_processed_name_is_taken(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan)
    _write(p_abs, b"a-newer-upscale-produced-since")           # the slot is retaken

    log = _FakeLog()
    undone, conflicts, errors, _r = cc.run_undo(orig_root, log)

    assert (undone, conflicts, errors) == (0, 1, 0)
    assert "occupied by another file" in log.text
    assert _read(p_abs) == b"a-newer-upscale-produced-since"
    assert _read(o_abs) == b"the-upscaled-photo-which-is-bigger"


@pytest.mark.parametrize("proc_name", [None, "beach_Sunset_Over_Sea.jpg"])
def test_undo_refuses_a_missing_archived_original(db_conn, tmp_path, proc_name):
    """Both namings, because they fail differently: when the processed file took
    the original's own name, a file DOES sit at the original's path, and reading
    that as 'already restored' would move the processed file out and leave the
    folder with neither copy."""
    orig_root, proc_root, plan = _pair(tmp_path, proc_name=proc_name)
    o_abs, p_abs, _rel = plan[0]
    moved_in = os.path.join(orig_root, "2006", proc_name or "beach.jpg")
    _run(db_conn, orig_root, proc_root, plan)
    os.remove(os.path.join(orig_root, cc.ARCHIVE_DIRNAME, "2006", "beach.jpg"))

    log = _FakeLog()
    undone, conflicts, errors, _r = cc.run_undo(orig_root, log)

    assert (undone, conflicts, errors) == (0, 1, 0)
    assert "archived original is missing" in log.text
    # Nothing moved: the processed file is still where the run put it.
    assert _read(moved_in) == b"the-upscaled-photo-which-is-bigger"
    assert not os.path.exists(p_abs)


def test_a_conflicting_run_stays_offerable(db_conn, tmp_path):
    """A partial undo must not mark the run done: the user fixes the conflict and
    tries again, and the rows already undone are skipped on that second pass."""
    orig_root, proc_root, plan = _pair(tmp_path)
    _pair_two = _pair(tmp_path, name="dog.jpg")[2][0]
    plan = plan + [_pair_two]
    _run(db_conn, orig_root, proc_root, plan)
    _write(os.path.join(orig_root, "2006", "dog.jpg"), b"edited-after-the-run")

    undone, conflicts, _e, _r = cc.run_undo(orig_root, _FakeLog())
    assert (undone, conflicts) == (1, 1)
    assert db.latest_conc_run(db_conn, orig_root) is not None      # still offered


# ── delete mode, and the cases undo will not act on ──────────────────────────

def test_undo_refuses_a_delete_run_and_lists_what_it_removed(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    o_abs, p_abs, _rel = plan[0]
    _run(db_conn, orig_root, proc_root, plan, mode="delete")
    assert _read(o_abs) == b"the-upscaled-photo-which-is-bigger"

    log = _FakeLog()
    undone, conflicts, errors, reason = cc.run_undo(orig_root, log)

    assert (undone, conflicts, errors) == (0, 0, 0)
    assert "cannot be restored" in reason
    assert o_abs in log.text                       # the record earns its keep
    assert _read(o_abs) == b"the-upscaled-photo-which-is-bigger"    # untouched


def test_a_delete_run_still_journals_every_file(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan, mode="delete")

    row = db.get_conc_actions(db_conn, db.latest_conc_run(db_conn, orig_root)["id"])[0]
    assert row["action"] == "deleted"
    assert row["archive_path"] is None             # nothing to come back from


def test_undo_with_nothing_recorded(db_conn, tmp_path):
    orig_root = str(tmp_path / "orig")
    os.makedirs(orig_root)
    undone, conflicts, errors, reason = cc.run_undo(orig_root, _FakeLog())
    assert (undone, conflicts, errors) == (0, 0, 0)
    assert "nothing to undo" in reason


def test_undo_refuses_a_journal_it_does_not_understand(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)
    db_conn.execute("UPDATE conc_runs SET schema_version = 99")
    db_conn.commit()

    undone, _c, _e, reason = cc.run_undo(orig_root, _FakeLog())
    assert undone == 0
    assert "different version" in reason


# ── journal housekeeping ─────────────────────────────────────────────────────

def test_old_runs_are_pruned(db_conn, tmp_path):
    """One row per file per run would grow without limit, and only the most recent
    run is ever offered."""
    orig_root = str(tmp_path / "orig")
    for _ in range(14):
        run_id = db.begin_conc_run(db_conn, orig_root, "p", "archive")
        db.record_conc_action(db_conn, run_id, "archived", "o", "a", "p", "d")
    db.prune_conc_runs(db_conn, orig_root, keep=10)

    kept = db_conn.execute("SELECT COUNT(*) FROM conc_runs").fetchone()[0]
    actions = db_conn.execute("SELECT COUNT(*) FROM conc_actions").fetchone()[0]
    assert kept == 10
    assert actions == 10                           # ON DELETE CASCADE took the rest


def test_the_newest_run_wins(db_conn, tmp_path):
    orig_root = str(tmp_path / "orig")
    db.begin_conc_run(db_conn, orig_root, "p", "archive")
    time.sleep(0.01)
    second = db.begin_conc_run(db_conn, orig_root, "p", "delete")
    assert db.latest_conc_run(db_conn, orig_root)["id"] == second


# ── the button: what it offers has to match what undo will actually do ───────
#
# The tab reads the journal on every refresh rather than remembering a flag, so
# these bind the method to a stub `self` — no tkinter window needed.

pytest.importorskip("tkinter")

from gui.tab_conciliate import ConciliateTab          # noqa: E402


class _Stub:
    """The handful of attributes _refresh_undo touches."""

    def __init__(self, folder):
        self._folder = folder
        self.running = False
        self.state = None
        self.tip = ""
        self.orig_var = self
        self.undo_btn = self
        self.undo_tip = self

    def get(self):                     # orig_var.get()
        return self._folder

    def configure(self, state=None, **_kw):
        self.state = state

    def set_text(self, text):          # undo_tip.set_text()
        self.tip = text


def _button_for(folder):
    stub = _Stub(folder)
    ConciliateTab._refresh_undo(stub)
    return stub


def test_the_button_offers_an_archive_run(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)

    stub = _button_for(orig_root)
    assert stub.state == "normal"
    assert "1 original(s) come back out of __Archive__" in stub.tip


def test_the_button_is_honest_about_a_delete_run(db_conn, tmp_path):
    """The roadmap's rule: a delete run cannot be undone, so the button must stay
    disabled and SAY why rather than offer a restore it cannot perform."""
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan, mode="delete")

    stub = _button_for(orig_root)
    assert stub.state == "disabled"
    assert "DELETED its originals" in stub.tip


def test_the_button_stops_offering_an_undone_run(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)
    cc.run_undo(orig_root, _FakeLog())

    stub = _button_for(orig_root)
    assert stub.state == "disabled"
    assert "no conciliation of this folder has been recorded" in stub.tip


def test_the_button_says_so_with_no_folder_and_with_no_record(db_conn, tmp_path):
    assert _button_for("").state == "disabled"
    assert "Choose the Original Files folder" in _button_for("").tip
    never = str(tmp_path / "never_conciliated")
    os.makedirs(never)
    assert _button_for(never).state == "disabled"


def test_the_button_stays_disabled_while_a_run_is_active(db_conn, tmp_path):
    orig_root, proc_root, plan = _pair(tmp_path)
    _run(db_conn, orig_root, proc_root, plan)

    stub = _Stub(orig_root)
    stub.running = True
    ConciliateTab._refresh_undo(stub)
    assert stub.state == "disabled"
