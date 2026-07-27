"""
Tests for the Home Assistant webhook notification backend (idea 2, 0.5.8).

The fourth backend, for a Home Assistant user with no MQTT broker. What makes it
different from the other three, and what these tests are really about:

  * It is WRITE-ONLY. Home Assistant answers 200 to a webhook id it has never
    heard of ("Always respond successfully to not give away if a hook exists or
    not") and to a request `local_only` refused. So no response means anything,
    and the one thing the app must never do is claim it worked. The Test wording
    is pinned here (finding F3), because a future edit "improving" it into a
    confident success message is exactly the regression that would cost a user a
    week of wondering why no alert arrives.
  * Its payload is consumed by Jinja in someone's automation, so the key names
    and the pre-rendered `message` are a CONTRACT: rename one and every user's
    automation silently stops filling in.
  * The webhook id is the endpoint's only credential, so it belongs in the
    untracked overlay (checked in test_config_store.py).

Every test here stubs the transport. conftest's autouse `_block_real_notifications`
already stubs `send_ha_webhook` itself, so the real one is restored per test (the
same pattern test_notification_severity.py uses for ntfy).
"""

import json

import pytest

import notifications as N


_REAL_SEND = N.send_ha_webhook
_REAL_TEST = N.test_ha_webhook

BASE = "http://homeassistant.local:8123"
WID = "imgtbx_a8f3c1"
ENDPOINT = f"{BASE}/api/webhook/{WID}"


@pytest.fixture
def posted(monkeypatch):
    """Capture what _post_json would have sent."""
    sent = {}

    def fake_post(url, payload, timeout=10):
        sent["url"], sent["payload"], sent["timeout"] = url, payload, timeout
        return {}

    monkeypatch.setattr(N, "send_ha_webhook", _REAL_SEND)
    monkeypatch.setattr(N, "test_ha_webhook", _REAL_TEST)
    monkeypatch.setattr(N, "_post_json", fake_post)
    return sent


# ── the two fields, and the mess users paste into them ───────────────────────

def test_the_endpoint_is_composed_from_the_two_fields():
    assert N.ha_webhook_url(BASE, WID) == ENDPOINT


@pytest.mark.parametrize("base,wid", [
    (BASE, WID),
    (BASE + "/", WID),                       # trailing slash on the base
    ("  " + BASE + "  ", "  " + WID + "  "),  # copy-paste whitespace
    (BASE, "/" + WID),
    (BASE, ENDPOINT),                        # whole endpoint pasted into the ID box
    (ENDPOINT, ""),                          # ...or into the URL box, ID left empty
    (ENDPOINT, WID),                         # both, consistently
])
def test_a_pasted_endpoint_is_normalised_either_way_round(base, wid):
    """Users paste the whole URL into whichever box they read first. Naive
    concatenation would build .../api/webhook//api/webhook/<id> and POST into the
    void, with a 200 back to say everything is fine."""
    assert N.ha_webhook_url(base, wid) == ENDPOINT


def test_a_typed_base_wins_over_one_pasted_alongside_it():
    """If the user filled in the URL box AND pasted a full endpoint into the ID
    box, the box they typed on purpose is the one to trust (it may be the LAN
    address, while the pasted one came from a browser on a different host)."""
    base, wid = N.split_ha_webhook("http://192.168.1.9:8123", ENDPOINT)
    assert (base, wid) == ("http://192.168.1.9:8123", WID)


@pytest.mark.parametrize("base,wid", [("", ""), (BASE, ""), ("", WID), (None, None)])
def test_a_half_configured_backend_has_no_endpoint(base, wid):
    assert N.ha_webhook_url(base, wid) == ""


def test_resolve_settings_reads_and_normalises_the_pair():
    cfg = {"notifications": {"ha_url": BASE + "/", "ha_webhook_id": " " + WID + " "}}
    s = N.resolve_settings(cfg)
    assert s["ha_url"] == BASE and s["ha_webhook_id"] == WID


def test_resolve_settings_is_blank_without_the_section():
    for cfg in ({}, None, {"notifications": {}}):
        s = N.resolve_settings(cfg)
        assert s["ha_url"] == "" and s["ha_webhook_id"] == ""


# ── sending ──────────────────────────────────────────────────────────────────

def test_send_posts_to_the_endpoint(posted):
    N.send_ha_webhook(BASE, WID, "Queue finished", "37 processed", N.COLOR_GREEN)
    assert posted["url"] == ENDPOINT


@pytest.mark.parametrize("base,wid", [("", WID), (BASE, ""), ("", "")])
def test_send_is_a_no_op_when_half_configured(posted, base, wid):
    """Unconfigured must mean silent, not a crash and not a stray POST."""
    N.send_ha_webhook(base, wid, "T", "D", N.COLOR_GREEN)
    assert not posted


def test_the_payload_is_the_contract(posted):
    """These key names live in the user's automation templates. Renaming one does
    not break anything here, it breaks every automation already written."""
    N.send_ha_webhook(BASE, WID, "Upscale finished", "37 processed, 2 skipped",
                      N.COLOR_GREEN,
                      [{"name": "Source", "value": "X:\\Poze"},
                       {"name": "Machine", "value": "DESKTOP"}],
                      source="Upscale Bot")
    p = posted["payload"]
    assert set(p) == {"app", "source", "title", "message", "level", "color", "fields"}
    assert p["app"] == "Image Toolbox"
    assert p["source"] == "Upscale Bot"          # which tool spoke
    assert p["title"] == "Upscale finished"
    assert p["level"] == "success"
    assert p["color"] == N.COLOR_GREEN
    assert p["fields"] == [{"name": "Source", "value": "X:\\Poze"},
                           {"name": "Machine", "value": "DESKTOP"}]


def test_the_message_is_pre_rendered_so_an_automation_is_one_line(posted):
    """`message` carries the description AND the fields, already formatted: that
    is what makes the HA side `message: "{{ trigger.json.message }}"` instead of a
    template that has to walk the field list."""
    N.send_ha_webhook(BASE, WID, "Done", "37 processed", N.COLOR_GREEN,
                      [{"name": "Source", "value": "X:\\Poze"}])
    message = posted["payload"]["message"]
    assert "37 processed" in message
    assert "Source: X:\\Poze" in message
    assert message == N._ntfy_body("37 processed", [{"name": "Source", "value": "X:\\Poze"}])


@pytest.mark.parametrize("color,level", [
    (N.COLOR_GREEN, "success"), (N.COLOR_ORANGE, "warning"),
    (N.COLOR_YELLOW, "caution"), (N.COLOR_RED, "error"), (424242, "info"),
])
def test_level_is_the_severity_word_an_automation_branches_on(posted, color, level):
    N.send_ha_webhook(BASE, WID, "T", "D", color)
    assert posted["payload"]["level"] == level


def test_the_payload_is_json_serialisable(posted):
    """It is POSTed as JSON; a value that cannot serialise would raise inside the
    run rather than at the boundary."""
    N.send_ha_webhook(BASE, WID, "T", "D", N.COLOR_RED,
                      [{"name": "Elapsed", "value": "01:12:33"}])
    json.dumps(posted["payload"])


def test_send_never_raises(monkeypatch, capsys):
    """Same fail-safe contract as the other three: a broken backend prints a tagged
    line and the run carries on."""
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError(ENDPOINT, 500, "Server Error", {}, None)

    monkeypatch.setattr(N, "send_ha_webhook", _REAL_SEND)
    monkeypatch.setattr(N, "_post_json", boom)
    N.send_ha_webhook(BASE, WID, "T", "D", N.COLOR_RED)
    assert "[HA]" in capsys.readouterr().out

    monkeypatch.setattr(N, "_post_json", lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    N.send_ha_webhook(BASE, WID, "T", "D", N.COLOR_RED)
    assert "[HA]" in capsys.readouterr().out


# ── the fan-out ──────────────────────────────────────────────────────────────

def test_notify_reaches_the_ha_backend(monkeypatch):
    calls = []
    for name in ("send_discord", "send_telegram", "send_ntfy", "send_ha_webhook"):
        monkeypatch.setattr(N, name, lambda *a, _n=name, **k: calls.append(_n))
    N.notify(N.resolve_settings({"notifications": {"ha_url": BASE, "ha_webhook_id": WID}}),
             "T", "D", N.COLOR_GREEN)
    assert calls == ["send_discord", "send_telegram", "send_ntfy", "send_ha_webhook"]


def test_notify_passes_the_tool_name_through_as_source(monkeypatch):
    seen = {}
    monkeypatch.setattr(N, "send_ha_webhook",
                        lambda *a, **k: seen.update(args=a, kwargs=k))
    N.notify({"ha_url": BASE, "ha_webhook_id": WID}, "T", "D", N.COLOR_GREEN,
             username="Video Bot")
    assert seen["kwargs"]["source"] == "Video Bot"


# ── the Test button says only what it knows (finding F3) ─────────────────────

def test_test_reports_success_without_claiming_delivery(posted):
    ok, msg = N.test_ha_webhook(BASE, WID)
    assert ok is True
    assert posted["url"] == ENDPOINT
    assert msg == N.HA_TEST_OK


def test_the_success_wording_disclaims_what_it_cannot_prove():
    """Pinned deliberately. Home Assistant answers 200 for a webhook ID it has
    never heard of, so a confident "Connected!" here is a lie the user cannot
    detect until no notification ever arrives. Every clause below is load-bearing:
    that HA answered, that this is ALL it proves, and what to do instead."""
    msg = N.HA_TEST_OK.lower()
    assert "200" in msg
    assert "all this can tell you" in msg
    assert "never heard of" in msg
    assert "actually ran" in msg
    for word in ("connected", "verified", "success", "working", "confirmed"):
        assert word not in msg, f"{word!r} claims more than a 200 can support"


@pytest.mark.parametrize("base,wid,expect", [
    ("", WID, "no home assistant url"),
    (BASE, "", "no webhook id"),
])
def test_test_refuses_a_half_filled_form(posted, base, wid, expect):
    ok, msg = N.test_ha_webhook(base, wid)
    assert ok is False and expect in msg.lower()
    assert not posted, "nothing should be sent for an incomplete form"


def test_a_404_is_reported_as_meaningful(monkeypatch):
    """A failure DOES prove something, unlike a success: HA itself never 404s a
    webhook, so a 404 means the request never reached it."""
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError(ENDPOINT, 404, "Not Found", {}, None)

    monkeypatch.setattr(N, "test_ha_webhook", _REAL_TEST)
    monkeypatch.setattr(N, "_post_json", boom)
    ok, msg = N.test_ha_webhook(BASE, WID)
    assert ok is False
    assert "404" in msg and "answers 200 even for an unknown" in msg


def test_an_unreachable_host_is_reported(monkeypatch):
    monkeypatch.setattr(N, "test_ha_webhook", _REAL_TEST)
    monkeypatch.setattr(N, "_post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route to host")))
    ok, msg = N.test_ha_webhook(BASE, WID)
    assert ok is False and "could not reach home assistant" in msg.lower()
