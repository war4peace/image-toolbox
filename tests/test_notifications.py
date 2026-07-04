"""
notifications.resolve_settings — the legacy-key migration (item 2). The Discord
webhook historically lived at upscale.discord_webhook_url; the new home is the
notifications section. resolve_settings must read the new location first, fall
back to the old, default the ntfy server, and never raise on a partial/None cfg.
"""

import notifications as n


def test_empty_config_yields_all_blank_with_ntfy_default():
    s = n.resolve_settings({})
    assert s["discord_webhook_url"] == ""
    assert s["telegram_bot_token"] == ""
    assert s["telegram_chat_id"] == ""
    assert s["ntfy_topic"] == ""
    assert s["ntfy_server"] == "https://ntfy.sh"


def test_none_config_does_not_raise():
    s = n.resolve_settings(None)
    assert s["discord_webhook_url"] == ""
    assert s["ntfy_server"] == "https://ntfy.sh"


def test_new_notifications_section_is_read():
    cfg = {"notifications": {
        "discord_webhook_url": "https://discord/new",
        "telegram_bot_token": "tok",
        "telegram_chat_id": 12345,          # numeric id must stringify
        "ntfy_server": "https://ntfy.example.com/",
        "ntfy_topic": "mytopic",
        "ntfy_token": "sekret",
    }}
    s = n.resolve_settings(cfg)
    assert s["discord_webhook_url"] == "https://discord/new"
    assert s["telegram_bot_token"] == "tok"
    assert s["telegram_chat_id"] == "12345"
    assert s["ntfy_server"] == "https://ntfy.example.com"   # trailing slash trimmed
    assert s["ntfy_topic"] == "mytopic"
    assert s["ntfy_token"] == "sekret"


def test_legacy_discord_webhook_is_used_when_new_is_absent():
    cfg = {"upscale": {"discord_webhook_url": "https://discord/legacy"}}
    s = n.resolve_settings(cfg)
    assert s["discord_webhook_url"] == "https://discord/legacy"


def test_new_discord_webhook_wins_over_legacy():
    cfg = {
        "notifications": {"discord_webhook_url": "https://discord/new"},
        "upscale": {"discord_webhook_url": "https://discord/legacy"},
    }
    s = n.resolve_settings(cfg)
    assert s["discord_webhook_url"] == "https://discord/new"


def test_whitespace_is_stripped():
    cfg = {"notifications": {"discord_webhook_url": "  https://d  ",
                             "telegram_bot_token": "  t  "}}
    s = n.resolve_settings(cfg)
    assert s["discord_webhook_url"] == "https://d"
    assert s["telegram_bot_token"] == "t"
