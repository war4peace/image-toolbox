"""
The optional support link (bottom status bar, next to "Report an issue").

Two things worth pinning even for a one-label feature: the URL must be a real
buymeacoffee page and not a leftover placeholder (it ships inside installed copies,
where a broken link cannot be corrected after the fact), and opening it must be
fail-safe like report_issue - a browser that refuses to launch must never take the
app down. gui.common is tkinter-free, so this runs everywhere the suite does.
"""

import urllib.parse

from gui import common


def test_donate_url_is_a_real_buymeacoffee_page():
    parts = urllib.parse.urlparse(common.DONATE_URL)
    assert parts.scheme == "https"
    assert parts.netloc in ("buymeacoffee.com", "www.buymeacoffee.com")
    # A non-empty path segment: "<username>" or an empty path would ship a link
    # that lands nowhere useful.
    name = parts.path.strip("/")
    assert name
    assert "<" not in name and ">" not in name


def test_open_donate_uses_the_constant(monkeypatch):
    opened = []
    monkeypatch.setattr(common.webbrowser, "open", lambda url: opened.append(url))
    common.open_donate()
    assert opened == [common.DONATE_URL]


def test_open_donate_is_failsafe(monkeypatch):
    def boom(_url):
        raise RuntimeError("no browser")
    monkeypatch.setattr(common.webbrowser, "open", boom)
    common.open_donate()        # must not raise
