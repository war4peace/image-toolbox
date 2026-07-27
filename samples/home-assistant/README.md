# Image Toolbox - Home Assistant samples

Ready-made Home Assistant content for anyone running **both** Home Assistant and
Image Toolbox. The app already publishes its state to MQTT (see the main README's
**Home Assistant (MQTT)** section); these files turn those topics into sensors and
two ready-to-paste dashboards.

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
   the app publishes RAM and VRAM as raw **MB** on purpose, so these template
   sensors compute `used / total * 100` for the gauges. Add under a top-level
   `template:` key and reload template entities (or restart). *Only needed for the
   dashboards' RAM/VRAM gauges; CPU and GPU utilization are already percentages.*
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
   [`automations.yaml`](automations.yaml): tell me when a run finishes, when it
   finishes badly, and when the app dies mid-run. Paste into your
   `automations.yaml` (or one at a time into a new automation's **Edit in YAML**
   view) and reload from **Developer Tools > YAML > Automations**. See
   [Notifications](#notifications) below for how they avoid the retained-message
   trap.

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

[`automations.yaml`](automations.yaml) has five ready-made ones (run finished, run
finished badly, the same via the retained sensor, app died mid-run, run started).
One thing is worth understanding before you write your own.

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
rather trigger off the sensor you already have on a dashboard, automation 3 in the
sample shows the retained route with the two guards it needs (ignore
`unknown`/`unavailable` state changes, and check `finished_at` is recent - it
carries a UTC offset, so that comparison is correct whatever timezone HA runs in).

The one thing only Home Assistant can tell you: **the app died mid-run**. The
`availability` topic is a Last Will, published by the *broker* when the app's
connection drops without a clean goodbye, so nothing inside the app could ever
send that alert about itself (automation 4).

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

A **video** run counts in frames, not files: `task/progress` is frames done / frames
queued across the whole queue, and the two per-item times are seconds per frame (the
run's running average, and the live measurement from the pod or local GPU). Its
`last_run` carries `processed` / `failed` / `total` job counts, `files`, `stop_reason`,
and a `cost` when a rented pod was billed.
| `system/cpu` | CPU load, % |
| `system/ram` / `system/ram_total` | RAM used / total, MB |
| `system/gpu_vram` / `system/gpu_vram_total` | VRAM used / total, MB |
| `system/gpu_temp` | GPU temperature, C |
| `system/gpu_util` | GPU core load, % |
| `system/gpu_power` / `system/gpu_power_limit` | GPU power draw / cap, W |
| `system/gpu_clock` | GPU core clock, MHz |
| `system/remote/*` | the same telemetry group for the rented pod (during a remote run) |

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
