"""
config_store.py
---------------
Load and save the app's settings with secrets kept out of the tracked file.

Historically everything lived in one `config.json`, which at runtime held live
credentials (the RunPod API key, the MQTT password, the Telegram/Discord/ntfy
tokens and webhook URLs). That file is tracked, so it was protected only by a
git `skip-worktree` bit plus a "maintainers: remember to scrub" note (and a git
clean filter for the webhook): several non-obvious git states one person had to
remember, and a standing leak risk.

This module removes that fragility by splitting the settings across two files at
the app root:

  * ``config.json``        - the tracked template. NEVER contains a secret value
                             (every secret field is present but blank).
  * ``config.local.json``  - an untracked overlay (``.gitignore``'d) that holds
                             ONLY the secret fields. Absent on a fresh machine,
                             which is fine.

``load()`` deep-merges the overlay over the base so the rest of the app still
sees one merged dict, exactly as before. ``save()`` does the reverse: the secret
fields go to the overlay and a secret-free copy goes to ``config.json``. A
one-time ``base_has_secrets()`` check lets the GUI migrate an older install
(secrets still in ``config.json``) on first launch. Stdlib only, fail-safe.
"""

import os
import json
import copy

BASE_NAME    = "config.json"
OVERLAY_NAME = "config.local.json"

# section -> the secret keys within it. These are the only fields ever moved to
# the untracked overlay; everything else stays in the tracked config.json. A
# webhook URL is itself a credential (anyone holding it can post), so it counts.
SECRET_FIELDS = {
    "runpod":        ("api_key",),
    "mqtt":          ("password",),
    # ha_webhook_id: a webhook has no other credential, so the id IS the password
    # (anyone who has it can fire the automation). The HA base URL is not secret.
    "notifications": ("discord_webhook_url", "telegram_bot_token", "ntfy_token",
                      "ha_webhook_id"),
    "upscale":       ("discord_webhook_url",),   # legacy (moved to notifications 0.3.8)
}


def base_path(app_root):
    return os.path.join(app_root, BASE_NAME)


def overlay_path(app_root):
    return os.path.join(app_root, OVERLAY_NAME)


def _read_json(path):
    """Parse a JSON file, or None if it is missing/unreadable/malformed."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _deep_merge(base, over):
    """A copy of `base` with `over` merged in (over wins; nested dicts merged)."""
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load(app_root):
    """Return the merged settings dict, or None if config.json is missing/unreadable.
    The overlay is optional (absent on a fresh machine or before the first save)."""
    base = _read_json(base_path(app_root))
    if base is None:
        return None
    overlay = _read_json(overlay_path(app_root)) or {}
    return _deep_merge(base, overlay)


def split_secrets(cfg):
    """Split `cfg` into (base, overlay): `base` is a copy with every secret field
    blanked (the key kept as a placeholder), `overlay` holds only the non-empty
    secret fields. Non-destructive (deep-copies its input)."""
    base = copy.deepcopy(cfg)
    overlay = {}
    for section, keys in SECRET_FIELDS.items():
        sec = base.get(section)
        if not isinstance(sec, dict):
            continue
        for key in keys:
            if key not in sec:
                continue
            val = sec[key]
            if val not in (None, "", [], {}):
                overlay.setdefault(section, {})[key] = val
            sec[key] = ""   # blank in the tracked file; keep the key as a placeholder
    return base, overlay


def base_has_secrets(app_root):
    """True if the on-disk config.json still holds a non-empty secret (an older
    install to migrate to the overlay). Fail-safe False."""
    base = _read_json(base_path(app_root))
    if not isinstance(base, dict):
        return False
    for section, keys in SECRET_FIELDS.items():
        sec = base.get(section)
        if isinstance(sec, dict):
            for key in keys:
                if sec.get(key):
                    return True
    return False


def save(cfg, app_root):
    """Write `cfg` split into config.json (secret-free) + config.local.json (secrets
    only). Returns True on success. The overlay is written only when it has content;
    an existing but now-empty overlay is removed so a cleared secret doesn't linger.

    The base (secret-free) file is written first, so even if the overlay write
    fails the tracked file never gains a secret (fail-safe direction)."""
    base, overlay = split_secrets(cfg)
    try:
        with open(base_path(app_root), "w", encoding="utf-8") as f:
            json.dump(base, f, indent=4)
    except OSError:
        return False
    op = overlay_path(app_root)
    try:
        if overlay:
            with open(op, "w", encoding="utf-8") as f:
                json.dump(overlay, f, indent=4)
        elif os.path.exists(op):
            os.remove(op)
    except OSError:
        return False
    return True
