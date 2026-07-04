"""
config_store (item 9): the two-file split that keeps secrets out of the tracked
config.json. All tests run against a throwaway app root in tmp_path, so the real
config.json / config.local.json are never touched.
"""

import json

import config_store as cs


SECRET_CFG = {
    "upscale": {"resolution": 2160, "discord_webhook_url": "https://legacy/hook"},
    "runpod":  {"api_key": "rp-SECRET", "gpu_type_id": "RTX 5090"},
    "mqtt":    {"host": "10.0.0.5", "username": "user", "password": "pw-SECRET"},
    "notifications": {
        "discord_webhook_url": "https://disc/hook",
        "telegram_bot_token":  "tg-SECRET",
        "telegram_chat_id":    "12345",          # not a secret
        "ntfy_server":         "https://ntfy.sh",  # not a secret
        "ntfy_topic":          "mytopic",          # not a secret
        "ntfy_token":          "ntfy-SECRET",
    },
}

# Every literal we consider a secret, for the leak assertion.
_SECRET_VALUES = ["rp-SECRET", "pw-SECRET", "tg-SECRET", "ntfy-SECRET",
                  "https://disc/hook", "https://legacy/hook"]


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── load / merge ─────────────────────────────────────────────────────────────

def test_load_merges_overlay_over_base(tmp_path):
    _write(cs.base_path(str(tmp_path)), {"runpod": {"api_key": "", "gpu_type_id": "x"}})
    _write(cs.overlay_path(str(tmp_path)), {"runpod": {"api_key": "SECRET"}})
    merged = cs.load(str(tmp_path))
    assert merged["runpod"]["api_key"] == "SECRET"     # overlay wins
    assert merged["runpod"]["gpu_type_id"] == "x"      # base preserved


def test_load_without_overlay_returns_base(tmp_path):
    _write(cs.base_path(str(tmp_path)), {"mqtt": {"password": ""}})
    assert cs.load(str(tmp_path)) == {"mqtt": {"password": ""}}


def test_load_missing_base_is_none(tmp_path):
    assert cs.load(str(tmp_path)) is None


def test_load_malformed_base_is_none(tmp_path):
    cs.base_path(str(tmp_path))
    with open(cs.base_path(str(tmp_path)), "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert cs.load(str(tmp_path)) is None


# ── split / save ─────────────────────────────────────────────────────────────

def test_split_blanks_base_and_collects_overlay():
    base, overlay = cs.split_secrets(SECRET_CFG)
    # base keeps every secret KEY but blanked
    assert base["runpod"]["api_key"] == ""
    assert base["mqtt"]["password"] == ""
    assert base["notifications"]["telegram_bot_token"] == ""
    assert base["upscale"]["discord_webhook_url"] == ""
    # non-secrets untouched in base
    assert base["upscale"]["resolution"] == 2160
    assert base["mqtt"]["username"] == "user"
    assert base["notifications"]["telegram_chat_id"] == "12345"
    # overlay holds the real values
    assert overlay["runpod"]["api_key"] == "rp-SECRET"
    assert overlay["notifications"]["ntfy_token"] == "ntfy-SECRET"


def test_split_omits_empty_secrets_from_overlay():
    _, overlay = cs.split_secrets({"runpod": {"api_key": ""}, "mqtt": {"password": ""}})
    assert overlay == {}


def test_save_then_load_round_trips(tmp_path):
    assert cs.save(SECRET_CFG, str(tmp_path)) is True
    assert cs.load(str(tmp_path)) == SECRET_CFG      # merged view identical


def test_saved_config_json_contains_no_secret(tmp_path):
    # The core security invariant: the tracked file must never carry a secret.
    cs.save(SECRET_CFG, str(tmp_path))
    with open(cs.base_path(str(tmp_path)), "r", encoding="utf-8") as f:
        raw = f.read()
    for secret in _SECRET_VALUES:
        assert secret not in raw, secret


def test_save_removes_empty_overlay(tmp_path):
    import os
    cs.save(SECRET_CFG, str(tmp_path))
    assert os.path.exists(cs.overlay_path(str(tmp_path)))
    # Clear every secret and save again: the overlay should be deleted, not left
    # holding a stale value.
    cleared = json.loads(json.dumps(SECRET_CFG))
    cleared["runpod"]["api_key"] = ""
    cleared["mqtt"]["password"] = ""
    cleared["notifications"]["discord_webhook_url"] = ""
    cleared["notifications"]["telegram_bot_token"] = ""
    cleared["notifications"]["ntfy_token"] = ""
    cleared["upscale"]["discord_webhook_url"] = ""
    cs.save(cleared, str(tmp_path))
    assert not os.path.exists(cs.overlay_path(str(tmp_path)))


# ── migration detection ──────────────────────────────────────────────────────

def test_base_has_secrets_true_before_migration(tmp_path):
    _write(cs.base_path(str(tmp_path)), SECRET_CFG)     # old install: secrets in base
    assert cs.base_has_secrets(str(tmp_path)) is True


def test_base_has_secrets_false_after_migration(tmp_path):
    cs.save(SECRET_CFG, str(tmp_path))                  # splits secrets out
    assert cs.base_has_secrets(str(tmp_path)) is False


def test_migration_flow_moves_secrets_out(tmp_path):
    # Simulate an old install with secrets in config.json, then the one-time GUI
    # migration: load the merged view, save it back through config_store.
    _write(cs.base_path(str(tmp_path)), SECRET_CFG)
    assert cs.base_has_secrets(str(tmp_path)) is True
    merged = cs.load(str(tmp_path))
    assert cs.save(merged, str(tmp_path)) is True
    # config.json is now scrubbed, the overlay holds the secrets, the merged view
    # is unchanged.
    assert cs.base_has_secrets(str(tmp_path)) is False
    assert _read(cs.overlay_path(str(tmp_path)))["runpod"]["api_key"] == "rp-SECRET"
    assert cs.load(str(tmp_path)) == SECRET_CFG
