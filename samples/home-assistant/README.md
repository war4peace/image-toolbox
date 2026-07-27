# Image Toolbox - Home Assistant samples

Ready-made Home Assistant content for anyone running **both** Home Assistant and
Image Toolbox. The app already publishes its state to MQTT (see the main README's
**Home Assistant (MQTT)** section); these files turn those topics into sensors and
two ready-to-paste dashboards.

> **No MQTT broker?** There is a second, much smaller route: the app can POST each
> alert straight to a Home Assistant automation, with no broker and no sensors.
> You get the notifications only (no dashboard, no live progress, and nothing if
> the app crashes). One file here serves it,
> [`automation-webhook.yaml`](automation-webhook.yaml), and the setup is documented
> in [`docs/notifications.md`](../../docs/notifications.md#the-home-assistant-webhook-route).
> Everything else on this page is the MQTT route.

Two tiers:

- **`dashboard-core.yaml`** - built only from Home Assistant's **built-in**
  Lovelace cards. Works on any install, no HACS, nothing to download.
- **`dashboard-custom.yaml`** - the same information with a nicer status header,
  band-coloured metric tiles and real time-series graphs, using a small set of
  named **HACS** custom cards.

## Prerequisites

1. An **MQTT broker** and the Home Assistant **MQTT integration** configured.
2. Image Toolbox pointed at the same broker: **Settings > MQTT**, set the broker
   host (and credentials), press **Test**, then **Save**. Clearing the host
   disables publishing.

## Install order

Add the entities first, then a dashboard.

1. **MQTT sensors** - [`mqtt-sensors.yaml`](mqtt-sensors.yaml): every published
   topic as a Home Assistant MQTT sensor (local machine and remote pod). Add it
   under the MQTT `sensor:` block in your `configuration.yaml` (or a file you
   already `!include`), then restart Home Assistant.
2. **Derived percentages** - [`template-sensors.yaml`](template-sensors.yaml):
   the app publishes raw values on purpose (RAM and VRAM as **MB**, progress as
   `X/Y` text), so these template sensors compute the percentages the gauges
   want. Add under a top-level `template:` key and reload template entities (or
   restart). *Only needed for the dashboards' RAM/VRAM/progress gauges; CPU and
   GPU utilization are already percentages.*
3. **A dashboard card** - each dashboard file is **one card** (a vertical stack),
   pasted into a single card's code editor. This is deliberate: editing the whole
   dashboard's raw YAML is risky, so you never touch it.
   1. Open your dashboard and click **Edit dashboard** (top-right pencil).
   2. Click **+ ADD SECTION**, then **+ ADD CARD** inside it and pick any card
      (a **Heading** is fine as a placeholder).
   3. In that card's editor, click **Show code editor**, select all, and paste
      the whole contents of [`dashboard-core.yaml`](dashboard-core.yaml) **or**
      [`dashboard-custom.yaml`](dashboard-custom.yaml) (everything from
      `type: vertical-stack` down). **Save**.

   To have both tiers, repeat with a second card. You can also lift any single
   sub-card out of the file into its own card.
4. **Notification automations** *(optional)* -
   [`automations-ui.yaml`](automations-ui.yaml): tell me when a run finishes,
   when it finishes badly, and when the app dies mid-run. Five ready-made ones,
   each a self-contained block you paste into the automation editor:
   1. **Settings > Automations & scenes > + CREATE AUTOMATION > Create new
      automation**.
   2. Top-right three-dot menu > **Edit in YAML**.
   3. Select all in that editor and paste **one** block from the file (from its
      `alias:` line down to the next banner). **Save**.

   Repeat for each one you want. No file editing, and nothing to reload. See
   [Notifications](#notifications) below for how they avoid the retained-message
   trap.

## Upgrading these files

You pasted these into your own configuration, so a new app version cannot update
them for you. To make that cheap, every file carries version markers, and each
kind of file is upgraded differently:

| File | Marker | What to do |
|------|--------|------------|
| `mqtt-sensors.yaml` | `# --- Added in 0.5.3 ---`, `# --- Changed in 0.5.3 ---` | Copy only the blocks newer than the version you first pasted. A *Changed* block replaces your copy of that one sensor. |
| `template-sensors.yaml` | `# --- Added in 0.5.8 ---` | Same: copy only the newer sensors. |
| `dashboard-core.yaml` / `dashboard-custom.yaml` | `UPDATED IN 0.5.8` header, `# NEW in 0.5.8` inline | Re-paste the **whole** file over the card. It is one card, so replacing it is easier than patching it, and you lose nothing (the card holds no state). |
| `automations-ui.yaml` | `# NEW: version 0.5.8`, `# CHANGED: version X.Y.Z` | Add the *NEW* ones as above. For a *CHANGED* one, open yours, **Edit in YAML**, select all and paste the new block over it: that keeps its name and enabled state, and is safer than editing in place. |

Lines that begin `# 0.5.8:` are notes about something that already exists and
whose **meaning** widened. They are not a change to copy.

### What changed in 0.5.8

The Video Upscaler now reports to MQTT like the other three tools; before, a
video run left the app looking idle for hours.

- **`mqtt-sensors.yaml`: nothing to re-copy.** The `task/*` and `last_run`
  sensors you already have simply fill in during a video run. Note that a video
  run counts **frames**, so `task/progress` reads e.g. `8412/95160` and the
  per-item times are seconds per frame.
- **`template-sensors.yaml`: one new sensor**, *Task Progress Percent*, which
  turns that `X/Y` text into a percentage a gauge can show.
- **Both dashboards: re-paste.** They gain a progress arc (needs the sensor
  above) and last-run detail rows: processed, failed, and what the pod cost.
- **`automations-ui.yaml` is new in 0.5.8** and triggers on the two new event
  topics, so it wants app 0.5.8 or later.

## The other route: a webhook, without a broker

Everything above needs an MQTT broker. If you do not have one and do not want one,
Image Toolbox can POST its alerts directly to a Home Assistant automation instead:
paste [`automation-webhook.yaml`](automation-webhook.yaml) in (same steps as step 4
above), invent a webhook ID, and put that ID plus your Home Assistant address into
**Settings > Notifications** in the app.

| | MQTT (this page) | Webhook |
|---|---|---|
| Needs | a broker | nothing |
| Gives you | live task state, telemetry, dashboards, run events, **and an alert if the app crashes mid-run** | the finish / failure notifications only |
| Setup | sensors in `configuration.yaml` + automations | one automation + two fields |

MQTT is the superset, and the only route that can tell you the app **died** (that
alert is the broker publishing the app's Last Will; a crashed app cannot POST
anything to say so). The webhook exists for people who will not run a broker.

**The full walkthrough, the payload your automation receives, and how to confirm it
actually works, are in
[`docs/notifications.md`](../../docs/notifications.md#the-home-assistant-webhook-route).**
One thing worth knowing before you start: create the automation **first**, then fill
in the app. Home Assistant answers 200 to a webhook ID it has never heard of, so
testing it in the other order looks like success and proves nothing.

## HACS cards (custom dashboard only)

Install from **HACS > Frontend**. A minimal set is Mushroom + ApexCharts.

| Card | Repo | What it adds |
|------|------|--------------|
| Mushroom | `piitaya/lovelace-mushroom` | status chips + band-coloured metric tiles |
| ApexCharts card | `RomRider/apexcharts-card` | real time-series telemetry graphs |
| card-mod *(optional)* | `thomasloven/lovelace-card-mod` | extra styling hooks |
| auto-entities *(optional)* | `thomasloven/lovelace-auto-entities` | auto-list every `sensor.image_toolbox_*` |

Custom-card versions drift over time; if a card looks off, check its repo for a
config change (captured here as of 2026-07).

## Notifications

[`automations-ui.yaml`](automations-ui.yaml) has five ready-made ones (run finished,
run finished badly, the same via the retained sensor, app died mid-run, run
started). One thing is worth understanding before you write your own.

**Everything in the topic table below is published *retained*.** That is what
makes a dashboard correct the moment Home Assistant starts, instead of blank until
the next run: the broker re-sends the last value to every new subscriber. The cost
is that a retained value arrives again on **every Home Assistant restart and every
broker reconnect** - so an automation that triggers on `last_run` cheerfully
announces a run that finished last Tuesday, every time you restart HA.

So the app also publishes two **events**, which are the opposite: not retained,
delivered live exactly once, never replayed.

| | Retained state | Event |
|---|---|---|
| Topics | `last_run`, `task/*`, `system/*`, `version`, `availability` | `event/run_started`, `event/run_finished` |
| Re-sent when a subscriber connects | yes (that's the point) | no |
| Use it for | dashboards, templates, "what is true now" | **triggers** |

**Trigger on the event topics.** `event/run_finished` carries the exact same JSON
object as `last_run`, so any template you write works on either. If you would
rather trigger off the sensor you already have on a dashboard, block 3 in the
sample shows the retained route with the two guards it needs (ignore
`unknown`/`unavailable` state changes, and check `finished_at` is recent - it
carries a UTC offset, so that comparison is correct whatever timezone HA runs in).

The one thing only Home Assistant can tell you: **the app died mid-run**. The
`availability` topic is a Last Will, published by the *broker* when the app's
connection drops without a clean goodbye, so nothing inside the app could ever
send that alert about itself (block 4).

Writing your own? [`docs/mqtt-integration.md`](../../docs/mqtt-integration.md) is the
full contract: every topic, the exact `last_run` keys each tool reports, and why the
design is the way it is.

## What updates when

- **`system/*`** (this machine) updates only **while a task runs**, plus a slow
  60 s idle sample. Between runs the values hold their last reading.
- **`system/remote/*`** (the rented pod) exists **only during a remote-pod run**
  and is zeroed when the pod is torn down. The dashboards' remote panel hides
  itself when there is no active pod.
- So **gaps in the history graphs are normal**, not a fault - the app is not a
  24/7 sensor, it reports while it works.

## Topic reference

All under the base topic `image-toolbox/`:

| Topic | Meaning |
|-------|---------|
| `version` / `latest_version` / `update` | running version, newest release, update-available flag |
| `availability` | `online` / `offline` (Last-Will, so HA knows if the app is running) |
| `last_run` | JSON summary of the last finished run (state = tool @ finished-at; fields as attributes) |
| `last_used` | timestamp the app last exited cleanly |
| `event/run_started` | **not retained.** `{tool, started_at}`, one shot, when a run starts |
| `event/run_finished` | **not retained.** The same object as `last_run`, one shot, when a run ends. Trigger on this, not on `last_run` (see [Notifications](#notifications)) |
| `task/name` | `idle` / `upscaling` / `tagging` / `conciliating` / `video upscaling` |
| `task/details` | human-readable current phase |
| `task/progress` / `task/eta` | `X/Y` and estimated time remaining |
| `task/runtime` | seconds of active-task runtime |
| `task/average_processing_time` / `task/last_processing_time` | seconds per item |
| `system/cpu` | CPU load, % |
| `system/ram` / `system/ram_total` | RAM used / total, MB |
| `system/gpu_vram` / `system/gpu_vram_total` | VRAM used / total, MB |
| `system/gpu_temp` | GPU temperature, C |
| `system/gpu_util` | GPU core load, % |
| `system/gpu_power` / `system/gpu_power_limit` | GPU power draw / cap, W |
| `system/gpu_clock` | GPU core clock, MHz |
| `system/remote/*` | the same telemetry group for the rented pod (during a remote run) |

A **video** run counts in frames, not files: `task/progress` is frames done / frames
queued across the whole queue, and the two per-item times are seconds per frame (the
run's running average, and the live measurement from the pod or local GPU). Its
`last_run` carries `processed` / `failed` / `total` job counts, `files`, `stop_reason`,
and a `cost` when a rented pod was billed.

## Screenshots

Captured from a live Home Assistant dashboard (dark theme).

### Core dashboard (no HACS)

App status, the live-task card (shown during an upscale run), and the local /
remote-pod telemetry panels:

| App status | Current task |
|---|---|
| ![App status](screenshots/core-section-general-info.png) | ![Current task](screenshots/core-section-current-task.png) |

| Local telemetry | Remote-pod telemetry |
|---|---|
| ![Local telemetry](screenshots/core-section-local-telemetry.png) | ![Remote-pod telemetry](screenshots/core-section-remote-telemetry.png) |

### Custom dashboard (HACS: Mushroom + ApexCharts)

The Mushroom chips header, the live-task card, band-coloured metric tiles, and the
ApexCharts telemetry graphs, in one section:

![Custom dashboard section](screenshots/custom-section-display.png)
