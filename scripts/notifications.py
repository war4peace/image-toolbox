"""
notifications.py
----------------
Shared, dependency-light notification layer (0.3.8). One place for the outbound
alerts the runners send when a queue finishes or an error occurs, fanning out to
every configured backend:

  - Discord  (webhook, rich embed)
  - Telegram (Bot API, HTML message)
  - ntfy     (HTTP publish to a topic; public ntfy.sh or a self-hosted server)

Previously each runner (batch_upscale.py, tag_and_rename.py) carried its own copy
of send_discord_notification(); that drifted and made adding a second backend a
double edit. This module is the single source of truth. Stdlib only (urllib),
fail-safe (a broken backend prints a tagged line and never raises), and it does
no config loading of its own: callers pass a resolved settings dict so the GUI,
the runners and tests all share the same code.

Config (config.json "notifications" section):

    "notifications": {
        "discord_webhook_url": "",
        "telegram_bot_token":  "",
        "telegram_chat_id":    "",
        "ntfy_server":         "https://ntfy.sh",
        "ntfy_topic":          "",
        "ntfy_token":          ""
    }

Back-compat: the Discord webhook historically lived at upscale.discord_webhook_url.
resolve_settings() reads the new location first and falls back to the old one, so
existing installs keep working until the next Settings save migrates the value.
"""

import json
import html
import urllib.request
import urllib.parse
import urllib.error

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_NTFY_SERVER = "https://ntfy.sh"

# Telegram has no embed colours, so a known Discord embed colour becomes a leading
# status emoji. Unknown colours fall back to no emoji (still a valid message).
_COLOR_EMOJI = {
    3066993:  "\U0001F7E2",  # green  - success
    15105570: "\U0001F7E0",  # orange - warning / degraded
    16776960: "\U0001F7E1",  # yellow - caution
    15548997: "\U0001F534",  # red    - error
}

# ntfy carries the status via an emoji "tag" (X-Tags, an emoji short code) plus a
# priority (X-Priority 1..5) so errors buzz louder than a normal completion.
_COLOR_NTFY = {
    3066993:  ("green_circle",  3),  # green  - success (default priority)
    15105570: ("orange_circle", 4),  # orange - warning / degraded (high)
    16776960: ("yellow_circle", 4),  # yellow - caution (high)
    15548997: ("red_circle",    5),  # red    - error (max/urgent)
}

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


# ─────────────────────────────────────────────────────────────
#  CONFIG RESOLUTION
# ─────────────────────────────────────────────────────────────

def resolve_settings(cfg):
    """
    Pull the notification settings out of a full config.json dict.
    Returns {"discord_webhook_url", "telegram_bot_token", "telegram_chat_id"},
    each a stripped string. Reads the "notifications" section, falling back to the
    legacy upscale.discord_webhook_url for the Discord URL.
    """
    notif = (cfg or {}).get("notifications", {}) or {}
    legacy_discord = (cfg or {}).get("upscale", {}).get("discord_webhook_url", "")
    return {
        "discord_webhook_url": (notif.get("discord_webhook_url") or legacy_discord or "").strip(),
        "telegram_bot_token":  (notif.get("telegram_bot_token")  or "").strip(),
        "telegram_chat_id":    str(notif.get("telegram_chat_id") or "").strip(),
        "ntfy_server":         (notif.get("ntfy_server") or _DEFAULT_NTFY_SERVER).strip().rstrip("/"),
        "ntfy_topic":          (notif.get("ntfy_topic") or "").strip(),
        "ntfy_token":          (notif.get("ntfy_token") or "").strip(),
    }


# ─────────────────────────────────────────────────────────────
#  LOW-LEVEL HTTP (stdlib, fail-safe)
# ─────────────────────────────────────────────────────────────

def _post_json(url, payload, timeout=10):
    """POST a JSON body; return the decoded JSON response (or {} if not JSON)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except Exception:
        return {}


def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


# ─────────────────────────────────────────────────────────────
#  DISCORD
# ─────────────────────────────────────────────────────────────

def send_discord(webhook_url, title, description, color, fields=None,
                 username="Image Toolbox", timeout=10):
    """Send an embed to a Discord webhook. No-op if webhook_url is empty."""
    if not webhook_url:
        return
    embed = {"title": title, "description": description,
             "color": color, "fields": fields or []}
    try:
        _post_json(webhook_url, {"username": username, "embeds": [embed]}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try: body = exc.read().decode("utf-8", "replace")
        except Exception: pass
        print(f"  [Discord] Failed to send notification: HTTP {exc.code} {exc.reason} -- {body}")
    except Exception as exc:
        print(f"  [Discord] Failed to send notification: {exc}")


# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────

def _telegram_text(title, description, color, fields=None):
    """Build an HTML message body mirroring the Discord embed."""
    emoji = _COLOR_EMOJI.get(color, "")
    head = f"{emoji} " if emoji else ""
    lines = [f"{head}<b>{html.escape(title or '')}</b>"]
    if description:
        lines.append("")
        lines.append(html.escape(description))
    for f in (fields or []):
        name = html.escape(str(f.get("name", "")))
        value = html.escape(str(f.get("value", "")))
        lines.append(f"<b>{name}</b>: {value}")
    return "\n".join(lines)


def send_telegram(bot_token, chat_id, title, description, color, fields=None, timeout=10):
    """Send a message via the Telegram Bot API. No-op unless token AND chat_id set."""
    if not bot_token or not chat_id:
        return
    url = _TELEGRAM_API.format(token=bot_token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": _telegram_text(title, description, color, fields),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = _post_json(url, payload, timeout=timeout)
        if not resp.get("ok", False):
            print(f"  [Telegram] Failed to send notification: {resp.get('description', resp)}")
    except urllib.error.HTTPError as exc:
        body = ""
        try: body = exc.read().decode("utf-8", "replace")
        except Exception: pass
        print(f"  [Telegram] Failed to send notification: HTTP {exc.code} {exc.reason} -- {body}")
    except Exception as exc:
        print(f"  [Telegram] Failed to send notification: {exc}")


# ─────────────────────────────────────────────────────────────
#  NTFY  (https://ntfy.sh or a self-hosted server)
# ─────────────────────────────────────────────────────────────

def _ntfy_body(description, fields=None):
    """Plain-text message body (ntfy has no embeds; the title rides a header)."""
    lines = []
    if description:
        lines.append(description)
    for f in (fields or []):
        lines.append(f"{f.get('name', '')}: {f.get('value', '')}")
    return "\n".join(lines) or " "


def send_ntfy(server, topic, title, description, color, fields=None, token=None, timeout=10):
    """
    Publish to an ntfy topic. No-op unless a topic is set (server defaults to the
    public ntfy.sh). The status colour maps to an emoji tag + priority so an error
    is louder than a normal completion. `token` is an optional Bearer token for a
    self-hosted server with auth.
    """
    topic = (topic or "").strip()
    if not topic:
        return
    server = (server or _DEFAULT_NTFY_SERVER).strip().rstrip("/")
    tag, priority = _COLOR_NTFY.get(color, ("", 3))
    headers = {
        "Title":     (title or "Image Toolbox").encode("ascii", "replace").decode("ascii"),
        "Priority":  str(priority),
        "User-Agent": _BROWSER_UA,
    }
    if tag:
        headers["Tags"] = tag
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = _ntfy_body(description, fields).encode("utf-8")
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try: body = exc.read().decode("utf-8", "replace")
        except Exception: pass
        print(f"  [ntfy] Failed to send notification: HTTP {exc.code} {exc.reason} -- {body}")
    except Exception as exc:
        print(f"  [ntfy] Failed to send notification: {exc}")


# ─────────────────────────────────────────────────────────────
#  UNIFIED FAN-OUT  (what the runners call)
# ─────────────────────────────────────────────────────────────

def notify(settings, title, description, color, fields=None, username="Image Toolbox"):
    """
    Send the same alert to every configured backend. `settings` is the dict from
    resolve_settings(). Each backend is independent and fail-safe: one being
    misconfigured never blocks the other, and neither ever raises into the run.
    color: Discord embed integer (15548997=red, 15105570=orange, 3066993=green).
    """
    settings = settings or {}
    send_discord(settings.get("discord_webhook_url", ""),
                 title, description, color, fields, username=username)
    send_telegram(settings.get("telegram_bot_token", ""),
                  settings.get("telegram_chat_id", ""),
                  title, description, color, fields)
    send_ntfy(settings.get("ntfy_server", ""),
              settings.get("ntfy_topic", ""),
              title, description, color, fields,
              token=settings.get("ntfy_token", ""))


# ─────────────────────────────────────────────────────────────
#  GUI HELPERS  (Test / Detect buttons)
# ─────────────────────────────────────────────────────────────

def test_discord(url, timeout=10):
    """Verify a Discord webhook by reading its metadata. Returns (ok, message)."""
    url = (url or "").strip()
    if not url:
        return False, "No webhook URL entered."
    try:
        info = _get_json(url, timeout=timeout)
        channel = info.get("channel_id") or info.get("name") or "(unknown)"
        return True, f"Webhook OK. Connected to channel: {channel}"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Invalid webhook (401 Unauthorized). Check the URL."
        if exc.code == 404:
            return False, "Webhook not found (404). It may have been deleted."
        return False, f"Webhook error: HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"Could not reach webhook: {exc}"


def _telegram_call(bot_token, method, timeout=10):
    """Call a Telegram Bot API GET method; return (ok, result_or_error_str)."""
    token = (bot_token or "").strip()
    if not token:
        return False, "No bot token entered."
    url = _TELEGRAM_API.format(token=token, method=method)
    try:
        data = _get_json(url, timeout=timeout)
        if data.get("ok"):
            return True, data.get("result")
        return False, data.get("description", "Telegram rejected the request.")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Invalid bot token (401 Unauthorized). Check the token from BotFather."
        return False, f"Telegram error: HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"Could not reach Telegram: {exc}"


def detect_telegram_chat(bot_token, timeout=10):
    """
    Find the chat to notify by reading the bot's recent updates. The user must
    have opened the bot and pressed Start (or sent any message) first, since a bot
    cannot message a user who hasn't contacted it. Returns (chat_id, message);
    chat_id is None on failure.
    """
    ok, result = _telegram_call(bot_token, "getUpdates", timeout=timeout)
    if not ok:
        return None, result
    # Walk newest-first; accept a private/group message or a channel post.
    for upd in reversed(result or []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member")
        chat = (msg or {}).get("chat")
        if chat and chat.get("id") is not None:
            cid = str(chat["id"])
            who = (chat.get("title") or chat.get("username")
                   or chat.get("first_name") or chat.get("type") or "chat")
            return cid, f"Found chat: {who} ({cid})"
    return None, ("No chat found. Open your bot in Telegram, press Start (or send "
                  "it any message), then click Detect again.")


def test_telegram(bot_token, chat_id, timeout=10):
    """
    Verify the bot token and, if a chat_id is set, send a test message.
    Returns (ok, message).
    """
    token = (bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token:
        return False, "No bot token entered."
    ok, result = _telegram_call(token, "getMe", timeout=timeout)
    if not ok:
        return False, result
    bot_name = (result or {}).get("username") or (result or {}).get("first_name") or "bot"
    if not chat_id:
        return False, f"Token OK (@{bot_name}), but no chat set. Click Detect first."
    url = _TELEGRAM_API.format(token=token, method="sendMessage")
    try:
        resp = _post_json(url, {
            "chat_id": chat_id,
            "text": "\U0001F7E2 <b>Image Toolbox</b>\n\nTelegram notifications are working.",
            "parse_mode": "HTML",
        }, timeout=timeout)
        if resp.get("ok"):
            return True, f"Sent a test message via @{bot_name}. Check Telegram."
        return False, f"@{bot_name} OK, but send failed: {resp.get('description', resp)}"
    except urllib.error.HTTPError as exc:
        body = ""
        try: body = exc.read().decode("utf-8", "replace")
        except Exception: pass
        if exc.code == 400 and "chat not found" in body.lower():
            return False, "Chat not found. Re-run Detect (press Start in your bot first)."
        return False, f"Send failed: HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"Could not reach Telegram: {exc}"


def test_ntfy(server, topic, token="", timeout=10):
    """Publish a test message to the ntfy topic. Returns (ok, message)."""
    topic = (topic or "").strip()
    if not topic:
        return False, "No ntfy topic entered."
    server = (server or _DEFAULT_NTFY_SERVER).strip().rstrip("/")
    headers = {
        "Title": "Image Toolbox",
        "Tags": "green_circle",
        "Priority": "3",
        "User-Agent": _BROWSER_UA,
    }
    if (token or "").strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    data = "ntfy notifications are working.".encode("utf-8")
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=timeout)
        return True, f"Sent a test message to {server}/{topic}. Check the ntfy app."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "Access denied. This topic needs an auth token (self-hosted server)."
        if exc.code == 404:
            return False, "Server not found (404). Check the ntfy server URL."
        return False, f"Send failed: HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"Could not reach the ntfy server: {exc}"
