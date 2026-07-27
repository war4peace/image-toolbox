# Notifications

How the app tells you a run finished (or went wrong) when you are not watching it.
Design + as-built notes, plus enough setup detail to pick a backend and get it working.

Runs here are long: an upscale queue is hours, a video queue can be most of a day, and a
remote one is **billing while it works**. So the interesting states (finished, finished
badly, stopped early, could not start) are pushed out rather than left on screen.

Related:

| For | Read |
|---|---|
| Home Assistant via **MQTT** (a different mechanism, and a richer one) | [`mqtt-integration.md`](mqtt-integration.md) |
| Ready-made Home Assistant automations, sensors and dashboards | [`samples/home-assistant/`](../samples/home-assistant/) |

Home Assistant users: there are **two** ways in, and they are not the same thing. The
webhook backend below is one of the four notification backends and is documented here. The
MQTT integration is not a notification backend at all: it publishes live state, telemetry
and a crash alert, and is documented in [`mqtt-integration.md`](mqtt-integration.md).
[Which one you want](#choosing-between-the-two-home-assistant-routes) is answered below.

## Contents

- [When you get one](#when-you-get-one)
- [The four backends](#the-four-backends)
- [Setting one up](#setting-one-up)
- [The Home Assistant webhook route](#the-home-assistant-webhook-route)
- [Choosing between the two Home Assistant routes](#choosing-between-the-two-home-assistant-routes)
- [Severity](#severity)
- [What a notification contains](#what-a-notification-contains)
- [Where the settings live](#where-the-settings-live)
- [Design rules](#design-rules)
- [As built: the Video Upscaler's alerts were the quietest (0.5.8)](#as-built-the-video-upscalers-alerts-were-the-quietest-058)
- [Not built](#not-built)
- [Code map and tests](#code-map-and-tests)

---

## When you get one

Every tool reports the end of a run, and the situations worth interrupting you for in the
middle of one. Nothing is sent while a run is simply progressing.

| Tool | Sends on |
|---|---|
| Batch Upscaler | queue finished (clean / with failures / stopped by you), the **degraded-GPU watchdog** auto-stopping the run, a remote pod stopping, the engine failing to start |
| Tag & Rename | queue finished (clean / with failures / stopped by you), Ollama unreachable, a remote pod stopping |
| Conciliation | finished (clean / with conflicts or errors / stopped by you) |
| Video Upscaler | queue finished (clean / with errors), stopped early (your Stop, a per-run minute or cost cap, GPU thrashing, a staging-folder refusal), a **slow segment** mid-run (pod contention, cost is accruing), waiting for a GPU to come back in stock and resuming, and a failure to start the pod |
| Benchmarks | a GPU benchmark sweep finishing or being stopped |

Every configured backend gets **the same alert**. They are independent: having Discord
misconfigured never blocks Telegram.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## The four backends

All optional, all off until you fill something in, and any combination works.

| | **Discord** | **Telegram** | **ntfy** | **HA webhook** |
|---|---|---|---|---|
| You need | a Discord server you can add a webhook to | a Telegram account | nothing (or your own server) | Home Assistant on your network |
| Arrives as | a coloured embed in a channel | a chat message from your own bot | a push notification on your phone | whatever your automation does with it |
| Setup effort | lowest, if you already use Discord | one chat with @BotFather | lowest overall: invent a topic name, install the app | one automation, then two fields |
| Account needed | Discord | Telegram | none | none |
| Severity shows as | the embed's **colour** | a leading **emoji** | an emoji **tag** + a **priority** (errors buzz harder) | a `level` field **you** branch on |
| Credential | the webhook URL (anyone with it can post) | a bot token | none on the public server; an optional token for self-hosted | the webhook ID (the endpoint's only protection) |
| Leaves your network | yes, to Discord | yes, to Telegram | yes, to ntfy.sh unless you self-host | **no** |
| Can you see it working? | yes: the Test lands in the channel | yes: the Test lands in the chat | yes: the Test reaches the app | **no**: see [Did it work?](#did-it-work) |

**If you have no preference: ntfy.** It needs no account, its phone app is the point of the
product, and it carries a real priority, so a failed run can be made to buzz differently
from a completed one. The trade-off is that on the public server the topic name *is* the
password, so make it long and unguessable.

**If you already run Home Assistant:** you probably want the [MQTT
integration](#choosing-between-the-two-home-assistant-routes) rather than the webhook. The
webhook exists for the HA user who has **no broker** and does not want one.

The last row is the honest asymmetry, and the reason the webhook gets its own setup section
below: the other three prove themselves the moment you press Test. A webhook cannot, ever.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Setting one up

All of it lives in **Settings > Notifications**, and each backend has a **Test** button
that sends a real sample alert. Settings only take effect on **Save**.

**Discord.** In Discord: *Server Settings > Integrations > Webhooks > New Webhook*, pick a
channel, **Copy Webhook URL**. Paste it into **Discord webhook** and press Test. That URL
is the whole credential, so treat it like a password.

**Telegram.** In Telegram, open **@BotFather**, send `/newbot`, follow the two prompts, and
copy the token it gives you into **Telegram bot token**. Then open your new bot and press
**Start** (a bot cannot message someone who has never contacted it), come back and press
**Detect**: it reads the bot's recent messages and fills in **Telegram chat ID** for you.
Press Test.

**ntfy.** Install the ntfy app (Android / iOS) or open [ntfy.sh](https://ntfy.sh). Make up a
topic name, something like `imgtbx-7f3a91c2b4`, and subscribe to it in the app. Put the same
name in **ntfy topic**, leave **ntfy server** at `https://ntfy.sh`, press Test. To self-host,
point the server field at your own instance; if it needs auth, put a token in the
config-only `notifications.ntfy_token` field.

**Home Assistant webhook.** Its own section, [below](#the-home-assistant-webhook-route):
unlike the other three, the order matters.

**Turning one off:** clear its field (webhook URL, bot token, topic, or the Home Assistant
URL) and Save.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## The Home Assistant webhook route

For running Home Assistant **without** an MQTT broker. The app POSTs each alert as JSON
straight to an automation of yours, and that automation decides what to do with it. Nothing
leaves your network.

### Do it in this order

**1. Create the automation in Home Assistant first.** Not second. Copy
[`samples/home-assistant/automation-webhook.yaml`](../samples/home-assistant/automation-webhook.yaml),
or build it by hand:

- Settings > Automations & scenes > **+ CREATE AUTOMATION** > Create new automation, then
  the three-dot menu > **Edit in YAML**, and paste the sample (everything from `alias:`).
- Change `webhook_id` to something long and unguessable, e.g. `imgtbx_7f3a9c21b8`. **That
  ID is the only thing protecting the endpoint** (there is no password, no token): anyone
  on your network who knows it can fire your automation. `imgtbx` is not good enough.
- Leave `local_only: true`. It means Home Assistant only accepts the request from your own
  network, which is what you want. (It is also HA's default.)
- Save.

**2. Then fill in the app.** Settings > Notifications:

| Field | What goes in it |
|---|---|
| **Home Assistant URL** | The address you open Home Assistant at, e.g. `http://homeassistant.local:8123`. No path. |
| **HA webhook ID** | The same ID you invented in step 1. |

Save. (If you paste the whole endpoint, `http://homeassistant.local:8123/api/webhook/xxx`,
into either box, the app splits it for you.)

**3. Press Test** and then read the next section, because what it tells you is limited.

**Why this order.** Home Assistant answers `200 OK` to a webhook ID it has never heard of.
Press Test before the automation exists and everything looks fine, and you find out weeks
later that no alert ever arrived.

### Did it work?

The app **cannot tell you**, and does not pretend to. HA answers 200 to an unknown webhook
ID on purpose ("Always respond successfully to not give away if a hook exists or not"), and
also to a request `local_only` refused. So a successful Test proves only that *some* Home
Assistant answered. That is exactly what it says, and no "verified" state is ever stored or
shown.

A **failure**, on the other hand, is real information: no route to the host, a timeout, a
TLS error, or a 404 (Home Assistant never 404s a webhook, so a 404 means the request never
reached it).

To actually confirm delivery, on the Home Assistant side, in under a minute:

- **The automation's Traces.** Settings > Automations > your automation > **Traces**. A
  trace exists only if it really ran, so one appearing after you press Test is proof, and
  none appearing is proof of the opposite.
- **Or make it visible.** Temporarily give the automation a
  `notify.persistent_notification` action (the sample already uses one), press Test, and
  look in the Home Assistant sidebar. Then swap in the action you actually want.

If Test says 200 and neither shows anything, in order of likelihood:

1. **The webhook ID does not match.** A typo, or the ID in Settings belongs to a different
   automation.
2. **`local_only` refused the request.** The app reached Home Assistant through a reverse
   proxy or a Nabu Casa URL, so the request did not arrive from the LAN. Point the app at
   the LAN address.

### The payload

`Content-Type: application/json`, so it lands in `trigger.json`:

```json
{
  "app":     "Image Toolbox",
  "source":  "Upscale Bot",
  "title":   "Upscale Queue -- Finished",
  "message": "37 processed, 2 skipped\nSource: X:\\Poze\nMachine: DESKTOP",
  "level":   "success",
  "color":   3066993,
  "fields":  [{"name": "Source", "value": "X:\\Poze"},
              {"name": "Machine", "value": "DESKTOP"}]
}
```

| Key | Use it for |
|---|---|
| `title` | the notification title |
| `message` | the body, **already written out**: the summary plus one line per detail. This is why the sample's action is one line rather than a template that walks the payload |
| `level` | `success` / `caution` / `warning` / `error` (`info` if a future version sends a severity this one does not know). What you branch on: "only buzz me for bad news" |
| `source` | which tool sent it: `Upscale Bot`, `Tag & Rename Bot`, `Conciliate Bot`, `Video Bot` |
| `fields` | the details, kept as a list. Names contain spaces, so pick one by name: `{{ trigger.json.fields \| selectattr('name','eq','Source') \| map(attribute='value') \| first }}` |
| `color` | the Discord colour int, passed through for completeness |
| `app` | always `Image Toolbox`, so one automation can serve several senders |

The sample automation shows a phone notification that escalates on `level`.

### HTTPS and self-signed certificates

Two setups are supported: plain `http://` on your LAN (the normal case, and what
`local_only` presumes), or a **valid** certificate. A self-signed certificate will fail
verification and the alert will not be sent. There is deliberately no "skip certificate
check" toggle: it is a permanent hole in every HTTPS call the app makes, to save one
config change on a LAN where plain HTTP is already fine.

A Nabu Casa or reverse-proxy URL works, but only with `local_only: false`, which gives up
the only protection the endpoint has. Not recommended.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Choosing between the two Home Assistant routes

| | **MQTT integration** | **Webhook backend** |
|---|---|---|
| Needs | an MQTT broker | nothing |
| Gives you | live task state, progress, ETA, GPU/CPU telemetry, dashboards, run-start and run-finish events, **and an alert if the app crashes mid-run** | the same finish/failure alerts the other backends send |
| Set up in | `configuration.yaml` (sensors) + automations | one automation + two fields |
| Documented in | [`mqtt-integration.md`](mqtt-integration.md) + [`samples/home-assistant/`](../samples/home-assistant/) | this page |

**Have a broker, or willing to run one: use MQTT.** It is a superset. In particular it is
the only way to learn the app **died** mid-run: that alert is the broker publishing the
app's Last Will, and a crashed app cannot POST a webhook to say it crashed.

**No broker and no appetite for one: use the webhook.** You lose the dashboard and the
crash alert; you keep every notification.

Running both is allowed and simply means two notifications per event.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Severity

Four levels. The colour is the canonical form (Discord was the first backend, so the
severity travels as a Discord embed colour int); the other two derive their own rendering
from it.

| Level | Constant | Means | Telegram | ntfy tag | ntfy priority |
|---|---|---|---|---|---|
| success | `COLOR_GREEN` | finished cleanly | 🟢 | `green_circle` | 3 (normal) |
| warning | `COLOR_ORANGE` | degraded or interrupted, needs attention | 🟠 | `orange_circle` | 4 (high) |
| caution | `COLOR_YELLOW` | finished with issues, or stopped early | 🟡 | `yellow_circle` | 4 (high) |
| error | `COLOR_RED` | failed, or could not start | 🔴 | `red_circle` | 5 (max/urgent) |

Two rules that matter more than they look:

1. **Never pass a raw int.** Use the `notifications.COLOR_*` constants. Everything else in
   the app derives from them, and a stray literal is exactly the bug described
   [below](#as-built-the-video-upscalers-alerts-were-the-quietest-058).
2. **An unrecognised colour degrades quietly**: level `info`, no emoji, no tag, priority 3.
   That is deliberate (a notification must never raise into a run), which is precisely why
   the mistake is invisible in production and has to be caught by a test.

`notifications.level_for(color)` returns the level as a word for a consumer that has no
notion of colour.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## What a notification contains

Every backend renders the same four things:

- **title** - what happened, e.g. `Upscale Queue -- Finished with Failures`.
- **description** - the body: counts, elapsed time, and for a video run one line per file
  (source and output resolution, runtime, and cost where a pod was billed) plus a totals
  line. Long queues are capped at 20 files with an "...and N more".
- **fields** - name/value pairs, typically `Source` (the folder) and `Machine` (the PC's
  name, so alerts from two machines are distinguishable).
- **severity** - see above.

Discord also gets a per-runner **username** (`Upscale Bot`, `Tag & Rename Bot`,
`Conciliate Bot`), which is why alerts from different tools look like different senders in
one channel.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Where the settings live

`config.json`, the `notifications` section:

```jsonc
"notifications": {
  "discord_webhook_url": "",   // secret: stored in config.local.json
  "telegram_bot_token":  "",   // secret: stored in config.local.json
  "telegram_chat_id":    "",
  "ntfy_server":         "https://ntfy.sh",
  "ntfy_topic":          "",
  "ntfy_token":          "",   // secret, config-only (self-hosted auth)
  "ha_url":              "",   // e.g. http://homeassistant.local:8123
  "ha_webhook_id":       ""    // secret: stored in config.local.json
}
```

The secrets are written to the untracked **`config.local.json`** overlay by
`config_store`, never to the tracked `config.json` (see `config_store.SECRET_FIELDS`). A
webhook ID counts as one: it is the endpoint's only credential, so it is treated like the
bot token, while the Home Assistant URL is not a secret and stays in `config.json`. The
Discord webhook used to live at `upscale.discord_webhook_url`; `resolve_settings` still
reads that as a fallback, and the next Settings save migrates it.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Design rules

- **One module.** `notifications.py` is the single source. Each runner had its own copy of
  `send_discord_notification()` before 0.3.8; they drifted, and adding a backend meant
  editing every one of them.
- **Stdlib only** (`urllib`, `html`). No SDKs for three HTTP calls.
- **Fail-safe, always.** A backend that is misconfigured, unreachable or slow prints one
  tagged line (`  [ntfy] Failed to send notification: ...`) and returns. Nothing here ever
  raises into a run, and no backend can block another. A notification is not worth losing
  a queue over.
- **No config loading of its own.** Callers pass a resolved settings dict, so the GUI, the
  runners and the tests all drive the same code.
- **No retry, no queue.** A missed alert stays missed. Buffering would mean persistence,
  ordering and staleness rules for something whose value expires in minutes.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## As built: the Video Upscaler's alerts were the quietest (0.5.8)

**The bug.** Severity is a Discord colour int, and until 0.5.8 every runner wrote its own
literal. The image runners used one palette; `batch_video_upscale` used the flat-UI one, so
its red (`0xE74C3C`) and amber (`0xF1C40F`) matched **no** entry in the emoji and ntfy maps.
Since an unknown colour degrades quietly by design, a **failed video run** went out on ntfy
at the default priority 3 with no tag, and on Telegram with no status emoji.

That is the worst possible case to get wrong: the video runner is the one whose failures
cost money, and its alerts were the least noticeable the app can produce. Nothing would
ever have reported it, because "quietly degrade" is the correct behaviour for an unknown
colour.

**The fix**, and why it is shaped this way:

- One `_SEVERITY` table maps a colour to *all three* renderings (level, Telegram emoji, ntfy
  tag, ntfy priority). Two parallel maps were two chances to add a colour to one and forget
  the other.
- Named `COLOR_*` constants, and every runner emits those instead of a literal, so the
  palettes cannot diverge again. The ints are unchanged, so existing Discord embeds look
  exactly as before.
- `_COLOR_ALIASES` still resolves the old flat-UI values, so an equivalent colour from
  anywhere renders as itself rather than degrading.
- `tests/test_notification_severity.py` pins it: every constant has a complete row, errors
  outrank successes in priority, the runners' pure outcome-to-colour helpers only ever
  return known severities, and a **structural** test scans the runner sources and fails on
  any raw colour literal. The last one is what stops this class of bug coming back.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Not built

- **Email / SMTP.** Rejected: a server, a port, TLS, an app password and a spam-folder
  failure mode, to deliver what a free push notification already does.
- **MQTT as a notification backend.** MQTT already publishes far richer state and Home
  Assistant can notify off it directly, so a sender would duplicate it. See
  [`mqtt-integration.md`](mqtt-integration.md).
- **Any attempt to verify the HA webhook.** Rejected, so it is not re-proposed: checking
  through HA's REST API with a long-lived access token (a second credential, a different
  auth model, and it still would not prove the ID maps to an automation); probing
  `/api/config` to at least confirm "this is Home Assistant" (proves the host, never the
  hook, and invites the same false confidence); and any round-trip where HA calls back into
  the app (that needs the app to listen on a port, which it does not and should not do).
  The verification is the user's, on the HA side. See [Did it work?](#did-it-work).
- **A "skip certificate check" toggle** for a self-signed HTTPS Home Assistant. A permanent
  hole in every HTTPS call the app makes, to avoid one config change on a LAN.

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Code map and tests

| File | Owns |
|---|---|
| [`scripts/notifications.py`](../scripts/notifications.py) | The severity table + `COLOR_*` constants + `level_for`, the four senders, the `notify()` fan-out, `resolve_settings` (incl. `split_ha_webhook`), the webhook's `ha_payload`, and the GUI Test/Detect helpers. |
| Runners | Their own thin `send_notification()` wrapper (which pins the per-runner Discord username) and the pure outcome-to-(title, colour) helpers: `_completion_notice`, `_stop_notice`, `_failure_notice`, `_summary_desc`. |
| [`scripts/gui/tab_settings.py`](../scripts/gui/tab_settings.py) | The Notifications section and its Test / Detect buttons. |
| [`scripts/config_store.py`](../scripts/config_store.py) | Keeps the Discord URL, bot token, ntfy token and HA webhook ID out of the tracked config. |
| [`samples/home-assistant/automation-webhook.yaml`](../samples/home-assistant/automation-webhook.yaml) | The receiving end: a paste-ready HA automation, with the `level`-aware phone variant in its comments. |

| Test file | Pins |
|---|---|
| [`tests/test_notifications.py`](../tests/test_notifications.py) | `resolve_settings`: the new section, the legacy fallback, defaults, whitespace, a `None` config. |
| [`tests/test_notifications_coverage.py`](../tests/test_notifications_coverage.py) | Coverage is even across the runners, and each pure outcome-to-(title, colour) helper picks the right one. |
| [`tests/test_notification_severity.py`](../tests/test_notification_severity.py) | The severity contract (see above), including the structural no-raw-literals scan. |
| [`tests/test_notifications_ha.py`](../tests/test_notifications_ha.py) | The webhook backend: endpoint composition (incl. a full URL pasted into either box), the no-op when half-configured, the payload **contract** (key names live in users' templates), `level` per colour, fail-safety, and the Test wording, pinned clause by clause so it can never be "improved" into claiming delivery. |
| [`tests/test_settings_ha_webhook.py`](../tests/test_settings_ha_webhook.py) | The Settings wiring: both fields collected, a pasted endpoint split on save, the unsaved-changes guard covering them, revert, and the Test row showing a success muted rather than green. |
| [`tests/test_ha_samples.py`](../tests/test_ha_samples.py) | The sample automation: `local_only` + `POST`, the placeholder ID, and its templates rendered against a **real** `ha_payload` so a renamed key is caught here rather than in a user's automation. |

A safety net worth knowing about when writing tests: `tests/conftest.py` stubs every
backend sender to a no-op for **every** test (add a new backend's sender to that list the
moment it exists: it is all that stands between the suite and a real endpoint, including
the developer's own Home Assistant), so the suite can never message a real endpoint from
the developer's live config. A test that needs a real sender must restore it deliberately
and stub the socket instead (`test_notification_severity.py` shows the pattern).

<div align="right"><a href="#notifications">↑ Back to top</a></div>
