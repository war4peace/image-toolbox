# Notifications

How the app tells you a run finished (or went wrong) when you are not watching it.
Design + as-built notes, plus enough setup detail to pick a backend and get it working.

Runs here are long: an upscale queue is hours, a video queue can be most of a day, and a
remote one is **billing while it works**. So the interesting states (finished, finished
badly, stopped early, could not start) are pushed out rather than left on screen.

Related:

| For | Read |
|---|---|
| Home Assistant via MQTT (a different mechanism, not a backend here) | [`mqtt-integration.md`](mqtt-integration.md) |
| Ready-made Home Assistant automations | [`samples/home-assistant/`](../samples/home-assistant/) |

## Contents

- [When you get one](#when-you-get-one)
- [The three backends](#the-three-backends)
- [Setting one up](#setting-one-up)
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

## The three backends

All optional, all off until you fill something in, and any combination works.

| | **Discord** | **Telegram** | **ntfy** |
|---|---|---|---|
| You need | a Discord server you can add a webhook to | a Telegram account | nothing (or your own server) |
| Arrives as | a coloured embed in a channel | a chat message from your own bot | a push notification on your phone |
| Setup effort | lowest, if you already use Discord | one chat with @BotFather | lowest overall: invent a topic name, install the app |
| Account needed | Discord | Telegram | none |
| Severity shows as | the embed's **colour** | a leading **emoji** | an emoji **tag** + a **priority** (errors buzz harder) |
| Credential | the webhook URL (anyone with it can post) | a bot token | none on the public server; an optional token for self-hosted |
| Privacy note | the message goes to Discord's servers | to Telegram's servers | to ntfy.sh unless you self-host; **anyone who guesses your topic name can read it** |

**If you have no preference: ntfy.** It needs no account, its phone app is the point of the
product, and it is the only one of the three that carries a real priority, so a failed run
can be made to buzz differently from a completed one. The trade-off is that on the public
server the topic name *is* the password, so make it long and unguessable.

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

**Turning one off:** clear its field (webhook URL, bot token, or topic) and Save.

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
  "ntfy_token":          ""    // secret, config-only (self-hosted auth)
}
```

The three secrets are written to the untracked **`config.local.json`** overlay by
`config_store`, never to the tracked `config.json` (see `config_store.SECRET_FIELDS`). The
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

- **A Home Assistant webhook backend** (POST the alert as JSON to an HA webhook trigger,
  for someone who runs Home Assistant but no MQTT broker). Researched, not built. The
  awkward part is that HA answers `200 OK` to a webhook id it has never heard of, on
  purpose, so a Test button could never confirm more than "something answered"; the
  verification would have to be the user's, from their own automation.
- **Email / SMTP.** Rejected: a server, a port, TLS, an app password and a spam-folder
  failure mode, to deliver what a free push notification already does.
- **MQTT as a notification backend.** MQTT already publishes far richer state and Home
  Assistant can notify off it directly, so a fourth sender would duplicate it. See
  [`mqtt-integration.md`](mqtt-integration.md).

<div align="right"><a href="#notifications">↑ Back to top</a></div>

## Code map and tests

| File | Owns |
|---|---|
| [`scripts/notifications.py`](../scripts/notifications.py) | The severity table + `COLOR_*` constants + `level_for`, the three senders, the `notify()` fan-out, `resolve_settings`, and the GUI Test/Detect helpers. |
| Runners | Their own thin `send_notification()` wrapper (which pins the per-runner Discord username) and the pure outcome-to-(title, colour) helpers: `_completion_notice`, `_stop_notice`, `_failure_notice`, `_summary_desc`. |
| [`scripts/gui/tab_settings.py`](../scripts/gui/tab_settings.py) | The Notifications section and its Test / Detect buttons. |
| [`scripts/config_store.py`](../scripts/config_store.py) | Keeps the webhook URL, bot token and ntfy token out of the tracked config. |

| Test file | Pins |
|---|---|
| [`tests/test_notifications.py`](../tests/test_notifications.py) | `resolve_settings`: the new section, the legacy fallback, defaults, whitespace, a `None` config. |
| [`tests/test_notifications_coverage.py`](../tests/test_notifications_coverage.py) | Coverage is even across the runners, and each pure outcome-to-(title, colour) helper picks the right one. |
| [`tests/test_notification_severity.py`](../tests/test_notification_severity.py) | The severity contract (see above), including the structural no-raw-literals scan. |

A safety net worth knowing about when writing tests: `tests/conftest.py` stubs the three
senders to no-ops for **every** test, so the suite can never message a real endpoint from
the developer's live config. A test that needs a real sender must restore it deliberately
and stub the socket instead (`test_notification_severity.py` shows the pattern).

<div align="right"><a href="#notifications">↑ Back to top</a></div>
