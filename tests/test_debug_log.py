"""
The fail-safe debug trail (recommendations item 7). debug_log appends a
timestamped line to logs/debug.log for the otherwise-silent `except: pass`
handlers, and must itself never raise. Tests point its logs dir at a tmp path
(monkeypatching _logs_dir) so they never touch the real logs/.
"""

import os

import debug_log


def _use_tmp_logs(monkeypatch, tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(debug_log, "_logs_dir", lambda: str(d))
    return d / "debug.log"


def test_writes_timestamped_line(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    debug_log.debug_log("hello world")
    text = log.read_text(encoding="utf-8")
    assert "hello world" in text
    assert f"[{debug_log._SOURCE}]" in text
    # Leading "YYYY-MM-DD HH:MM:SS " timestamp.
    assert text[:4].isdigit() and text[4] == "-"


def test_appends_not_truncates(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    debug_log.debug_log("first")
    debug_log.debug_log("second")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert "first" in lines[0] and "second" in lines[1]


def test_exc_arg_formats_type_and_message(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    debug_log.debug_log("save failed", exc=ValueError("bad row"))
    text = log.read_text(encoding="utf-8")
    assert "save failed: ValueError: bad row" in text


def test_tb_appends_traceback(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    try:
        raise KeyError("missing")
    except KeyError as exc:
        debug_log.debug_log("in handler", exc=exc, tb=True)
    text = log.read_text(encoding="utf-8")
    assert "in handler: KeyError: 'missing'" in text
    assert "Traceback (most recent call last)" in text


def test_tb_outside_handler_no_bogus_trace(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    debug_log.debug_log("no active exc", tb=True)   # not inside an except block
    text = log.read_text(encoding="utf-8")
    assert "no active exc" in text
    assert "NoneType: None" not in text


def test_rolls_over_past_max(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    monkeypatch.setattr(debug_log, "_MAX_BYTES", 200)
    for i in range(50):
        debug_log.debug_log(f"line number {i} padding padding padding")
    # Once it passed the cap the active file rolled to debug.log.1.
    assert os.path.exists(str(log) + ".1")
    assert log.exists()


def test_never_raises_on_unwritable_dir(monkeypatch):
    # A logs dir that can't be created must be swallowed, not raised.
    monkeypatch.setattr(debug_log, "_logs_dir",
                        lambda: (_ for _ in ()).throw(OSError("nope")))
    debug_log.debug_log("should not raise")   # no assertion needed: must not throw


def test_non_string_message(monkeypatch, tmp_path):
    log = _use_tmp_logs(monkeypatch, tmp_path)
    debug_log.debug_log(12345)
    assert "12345" in log.read_text(encoding="utf-8")
