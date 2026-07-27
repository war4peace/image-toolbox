# MQTT / Home Assistant integration

Design + as-built notes for everything the app publishes to an MQTT broker (feature #3,
shipped 0.2.4; extended through 0.5.8). This is the **contract and the reasoning**: what
is published, when, retained or not, and why each decision went the way it did.

Related, deliberately not duplicated here:

| For | Read |
|---|---|
| Setting it up in Home Assistant (sensors, dashboards, automations) | [`samples/home-assistant/README.md`](../samples/home-assistant/README.md) |
| Why the `system/*` telemetry looks the way it does (+ the in-app graphs) | [`telemetry-design.md`](telemetry-design.md) |
| The Video Upscaler itself | [`video-upscaler.md`](video-upscaler.md) |

MQTT is **optional and opt-in**: it activates when a broker **host** is set in
Settings > MQTT and stops when the host is cleared. There is no separate enable toggle,
because a second switch that can disagree with the host field is one more thing to get
wrong. `paho-mqtt` is imported lazily, so an older venv that predates it still launches.

## Contents

- [The shape: retained state vs one-shot events](#the-shape-retained-state-vs-one-shot-events)
- [Topic reference](#topic-reference)
- [The run lifecycle](#the-run-lifecycle)
- [`last_run` and `event/run_finished` payloads](#last_run-and-eventrun_finished-payloads)
- [Timestamps carry an offset](#timestamps-carry-an-offset)
- [Client behaviour](#client-behaviour)
- [As built: the Video Upscaler published nothing (0.5.8)](#as-built-the-video-upscaler-published-nothing-058)
- [As built: retained topics re-fire an automation (0.5.8)](#as-built-retained-topics-re-fire-an-automation-058)
- [Considered and rejected](#considered-and-rejected)
- [Code map](#code-map)
- [Tests](#tests)

---

## The shape: retained state vs one-shot events

Two kinds of topic, and the difference is the single most important thing in this
document.

| | Retained **state** | One-shot **event** |
|---|---|---|
| Topics | everything except `event/*` | `event/run_started`, `event/run_finished` |
| Flags | `retain=True`, qos 0 | `retain=False`, qos 1 |
| Re-sent when a subscriber connects | **yes** | never |
| Replayed by the app on reconnect | yes (`MqttClient._retained`) | never (not stored) |
| Use it for | dashboards, templates, "what is true now" | **triggers** |

Retained is what makes a dashboard correct the instant Home Assistant restarts instead of
blank until the next run. The cost is that a retained value is delivered **again** on
every HA restart and every broker reconnect, which makes it a bad trigger: an automation
on `last_run` re-announces a run that finished days ago, every restart. The events exist
for exactly that reason and carry no other information: `event/run_finished` is the same
JSON object as `last_run`, so a user's template works verbatim on either.

The full reasoning, and what was rejected, is in
[the as-built note below](#as-built-retained-topics-re-fire-an-automation-058).

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Topic reference

All under the base topic `image-toolbox/`. Every topic here is a **constant** in
`mqtt_publisher.py`; nothing in the app or the samples spells one out as a string
(`tests/test_ha_samples.py` enforces that for the samples).

### App

| Topic | Payload | Published by |
|---|---|---|
| `version` / `latest_version` / `update` | running version, newest release, `yes`/`no` | `App._startup_worker`, once per launch after the GitHub check |
| `availability` | `online` / `offline` | `MqttClient` on connect; the **broker** publishes `offline` as the Last Will if the app dies |
| `last_used` | timestamp of the last clean exit | `App._on_close` via `MqttClient.stop` |

### Task state (while a run is live)

| Topic | Payload |
|---|---|
| `task/name` | `idle` / `upscaling` / `tagging` / `conciliating` / `video upscaling` |
| `task/details` | the human phase line, mirroring the tab's status |
| `task/progress` | `X/Y`. **Files** for the image tools, **frames** for the Video Upscaler |
| `task/eta` | estimated time remaining, formatted |
| `task/runtime` | seconds of active-task runtime |
| `task/average_processing_time` | seconds per item (per frame on video) |
| `task/last_processing_time` | seconds for the last item (the live s/frame on video) |
| `last_run` | JSON summary of the last finished run, see below |

### Events (not retained)

| Topic | Payload |
|---|---|
| `event/run_started` | `{"tool": ..., "started_at": ...}` |
| `event/run_finished` | the same object as `last_run` |

### Telemetry

`system/*` for this machine and `system/remote/*` for a rented pod during a remote run:
`cpu`, `ram`, `ram_total`, `gpu_vram`, `gpu_vram_total`, `gpu_temp`, `gpu_util`,
`gpu_power`, `gpu_power_limit`, `gpu_clock`. Design and cadence in
[`telemetry-design.md`](telemetry-design.md); `clear_remote_telemetry` zeroes the remote
set when a pod is torn down, so a dead pod leaves no stale reading.

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## The run lifecycle

All of it lives in **`MqttTaskState`** (`gui/tooltab.py`), a widget-free mixin that
`ToolTab` (Batch Upscaler, Tag & Rename, Conciliation) and `VideoTab` both use, so there
is one implementation rather than one per tab. Every call is a no-op while no MQTT client
exists.

| When | Call | Publishes |
|---|---|---|
| Run starts (`ToolTab.launch` / `VideoTab._begin_run`) | `mqtt_task_started()` | `task/name` = the tool, `details` = `starting`, `progress`/`eta` blank, `runtime` = 0, **and** `event/run_started` |
| During the run | `mqtt_task_update(...)` | whichever of `details`/`progress`/`eta`/`runtime`/`avg`/`last` this tick knows |
| Run ends (`ToolTab.on_exit` / `VideoTab._end_run`) | `mqtt_task_idle()` | `task/name` = `idle`, `progress`/`eta` blank, **`last_run`** and **`event/run_finished`** (only if the runner sent a `DONE`) |

`mqtt_task_update` treats `None` as "no reading this tick", not "clear it", so a tool that
cannot measure one value never wipes what another tick published.

Who calls it during a run:

- **Image tools:** `ToolTab._handle_eta` on each `ETA` event (runtime, progress, eta, avg,
  last), `_scan_progress` on each progress line, and `_on_phase_change` mirrors the status
  line to `task/details`. Cadence follows the runner, i.e. roughly per image.
- **Video Upscaler:** `VideoTab._publish_task_state`, called from the 1 s `_run_tick` but
  **throttled to 10 s** (`MQTT_PUBLISH_S`). A multi-hour run should not push thousands of
  retained messages for numbers a human reads occasionally. The exception is the per-file
  transition, published immediately on the `VIDEO` event, because that is the interesting
  one.

**A run that crashes before its runner reports publishes neither `last_run` nor the
event.** The alternative was inventing a summary the app does not have. What covers that
case is the availability LWT: `availability` going `offline` while `task/name` is not
`idle` means the app died mid-run, and Home Assistant is the only place that can be
noticed (the app cannot send an alert about its own death). The samples ship that
automation.

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## `last_run` and `event/run_finished` payloads

The payload is the runner's own `DONE` event (the `@@TBX@@` marker stream), with two keys
added by `mqtt_task_idle`: `tool` (only if the runner did not set one) and `finished_at`.

Four keys are **shared on purpose**, so one Home Assistant automation covers every tool:

| Key | Meaning |
|---|---|
| `tool` | `upscale` / `tag` / `conciliating` / `video` |
| `processed` | items finished |
| `failed` | items that failed |
| `elapsed_seconds` | how long the run took |

The rest are per tool:

| Tool | Additional keys |
|---|---|
| Batch Upscaler | `skipped`, `corrupt`, `stopped_by_user`, `degraded`, `remote_stopped` |
| Tag & Rename | `rotated`, `skipped`, `stop_reason` |
| Conciliation | `done`, `conflicts`, `errors`, `removed_dirs`, `stopped_by_user` |
| Video Upscaler | `total`, `files`, `stop_reason`, `stopped_by_user`, `cost`, `source` |

Notes worth knowing before writing a template:

- **Conciliation reports both namings.** Its `DONE` predates the shared convention
  (`done`/`conflicts`/`errors`/`removed_dirs`), so 0.5.8 added the shared four beside them
  rather than renaming anything: an existing template still works, and a generic
  automation stops reporting "0 processed" for every conciliation run.
- **Not every tool reports every "went wrong" key.** `stop_reason` comes from Tag & Rename
  and the Video Upscaler, `stopped_by_user` / `degraded` from the Batch Upscaler. A
  condition that wants to catch all of them has to test all of them with defaults; the
  sample automations do, and a test renders them against all four runners' real payloads.
- **The video payload is reduced on purpose.** `batch_video_upscale._done_payload` drops
  the runner summary's `attempted` set (not JSON-serialisable) and turns its per-file
  detail list into a count: that list belongs in the notification body, not in a retained
  MQTT payload. A billed pod's per-file costs are summed into `cost` (null on a local run,
  which bills nothing, rather than a misleading `0.00`).
- **Exactly one `DONE` per video run.** It is emitted from a single seam,
  `batch_video_upscale._run_finished`, which also sends the completion notification. The
  grouped multi-pod (18) and Auto-resume (#6) paths already suppressed their per-pass
  notification and suppress the per-pass `DONE` with it.

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Timestamps carry an offset

`finished_at`, `started_at` and `last_used` all come from **`gui.common.now_stamp()`**:
ISO-8601 **with** the machine's UTC offset (`2026-07-27T18:04:11+03:00`).

This is not cosmetic. The retained route's freshness guard is
`as_timestamp(now()) - as_timestamp(finished_at)`, and a **naive** timestamp is
interpreted in whatever timezone the Home Assistant *process* runs in, commonly UTC in a
container. A Windows box two hours off HA would then look either two hours stale (the
guard suppresses every alert) or two hours in the future: a silent failure of exactly the
guard that route depends on. With the offset the comparison is correct from any timezone.

New code that publishes a timestamp must use `now_stamp()`. Local log-file stamps
deliberately stay naive and local (they are read by a human next to the machine).

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Client behaviour

`mqtt_publisher.MqttClient` holds one connection for the app's lifetime.

- **Availability / LWT.** `will_set(availability, "offline", retain=True, qos=1)` before
  connecting, `online` published on connect. So HA always knows whether the app is alive,
  including after a crash or a pulled power cord.
- **Reconnect replay.** Every retained publish is remembered in `_retained` and re-sent on
  each connect, so a value published before the link was up (or across a reconnect) is not
  lost. Events are **not** stored, which is what makes them un-replayable.
- **Fail-safe.** A publish failure never raises into the app. Failures are logged to
  `logs/debug.log` **once per broken streak** (reset on the next success), because
  publishes can fire every few seconds and a down broker would otherwise flood the file.
- **Teardown is bounded.** `stop()` publishes `last_used` + `offline` and disconnects on a
  daemon thread with a bounded join: paho's `loop_stop()` joins its network thread, and
  that join can stall on the UI thread even with a healthy broker (it did). If teardown
  cannot finish quickly the thread is abandoned and the app exits anyway; the broker still
  delivers the LWT.
- **Threading.** Network calls run off the UI thread. `App.mqtt_publish(values, retain=,
  qos=)` is the only entry point the GUI uses and no-ops when no client exists (tabs are
  constructed before `self.mqtt` is assigned).

**Verifying it works** is easy here, and worth contrasting with the planned HA *webhook*
backend: MQTT is **observable**. The retained topics can be watched landing in MQTT
Explorer or HA's Developer Tools > MQTT, and Settings' **Test** reports a genuine broker
connection. A webhook is write-only and answers 200 whether or not anything is listening,
so only Home Assistant can confirm it.

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## As built: the Video Upscaler published nothing (0.5.8)

**The gap.** `VideoTab` is not a `ToolTab` (its shape is different enough that it carries
its own subprocess plumbing), so it inherited none of the `task/*` publishing, and
`batch_video_upscale` emitted no `DONE` event at all. Consequences: `task/name` never
became anything video-ish, `last_run` was **never** written for a video queue, and
`task/progress` / `eta` / `runtime` sat frozen at whatever the last image run left. An
MQTT "notify me when the queue finishes" automation therefore silently never fired for the
longest, most notification-worthy runs the app does.

**The fix.**

1. The publishing was split out of `ToolTab` into the widget-free `MqttTaskState` mixin,
   which both tabs use. Lifting it beat duplicating it: two copies of a publish path that
   only ever runs on a user's broker would drift unnoticed.
2. `VideoTab` announces `video upscaling`, mirrors the running view on a 10 s throttle,
   and goes `idle` + `last_run` at the end.
3. `batch_video_upscale` gained the `DONE` event from the single `_run_finished` seam.

**One thing the investigation got wrong in a useful way.** The gap was first written up as
"`system/*` still flows, but only on the slow 60 s idle cadence" during a video run. It
does not flow at all: `App._any_task_running()` counts a video run and stands the idle
sampler down, while the tab only started its own sampler for a **local** run. So a remote
video run froze the local telemetry for hours. `VideoTab._launch` now starts a 30 s
keep-alive for remote runs too, without opening a local usage graph (the pod's own history
covers its GPU work, matching `ToolTab._start_telemetry`).

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## As built: retained topics re-fire an automation (0.5.8)

**The trap.** Every topic was retained, and `MqttClient` replays its retained set on
reconnect. An HA `mqtt` trigger fires on the retained message it is handed *when it
subscribes*, so "when `last_run` changes, notify me" fires on **every Home Assistant
restart** and every app reconnect, re-announcing a run that finished days ago.

**The fix** is the standard MQTT split rather than documentation alone: retained topics
for state, non-retained topics for events. `event/run_started` / `event/run_finished` are
published `retain=False, qos=1` and deliberately **not** stored in `MqttClient._retained`,
so neither the broker nor the app can ever resend them. An automation on them cannot
misfire, with no guard at all.

The documentation half still matters, because someone will want to trigger off the sensor
they already have on a dashboard. That retained route needs **two** guards, and the
samples ship it with both (and disabled by default so it cannot double up):

1. `not_from: [unknown, unavailable]` on the state trigger, or HA's own startup transition
   fires it.
2. A freshness condition on `finished_at` (10 minutes), which is only correct because the
   timestamp carries an offset.

qos 1 for events, qos 0 for state: an event has no retained copy to fall back on, so it is
worth the extra round trip. (HA usually subscribes at qos 0, so this mainly guarantees the
broker received it.)

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Considered and rejected

- **Publishing `last_run` non-retained** (the one-line "fix" for the trap). It would make
  the dashboard's last-run card read `unknown` after every Home Assistant restart until
  the next run finishes, which is the exact problem retained solves. State and events want
  opposite flags, so the answer is both topics, not one compromise.
- **A "run finished" event for a crashed run.** There is no summary to put in it, and a
  fabricated one is worse than silence. The availability LWT covers the case honestly.
- **A separate `mqtt.enabled` flag.** The host field is the switch. A second control that
  can disagree with it is a support question waiting to happen.
- **MQTT Discovery** (retained `homeassistant/.../config` topics, auto-created device and
  entities, no YAML to paste). A real feature with real lifecycle work (unique ids,
  availability wiring, config removal) and out of scope so far; the paste-in samples cover
  the need. See [`telemetry-design.md`](telemetry-design.md) section 3.1.
- **Publishing the video run's per-file detail list** in `last_run`. Retained payloads are
  re-delivered forever and HA's state field is capped at 255 characters (which is why
  `last_run` uses `json_attributes_topic` plus a short `value_template`). The counts are
  the state; the detail belongs in the notification body.

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Code map

| File | Owns |
|---|---|
| [`scripts/mqtt_publisher.py`](../scripts/mqtt_publisher.py) | The topic constants, the one-shot helpers (`test_connection` / `publish_state` / `publish_version`) behind Settings' Test and Publish now, and the persistent `MqttClient` (LWT, retained replay, `publish` / `publish_many` with the retain+qos flags). |
| [`scripts/gui/tooltab.py`](../scripts/gui/tooltab.py) | `MqttTaskState`: `mqtt_task_started` / `mqtt_task_update` / `mqtt_task_idle` / `mqtt_event`, mixed into `ToolTab` and `VideoTab`. |
| [`scripts/gui/app.py`](../scripts/gui/app.py) | `start_mqtt` / `restart_mqtt` / `stop_mqtt`, `mqtt_publish(values, retain, qos)`, the version snapshot, and the telemetry publishes (`_apply_telemetry`, `apply_remote_telemetry`, `clear_remote_telemetry`). |
| [`scripts/gui/common.py`](../scripts/gui/common.py) | `mqtt_config` / `mqtt_enabled` (host = the switch) and `now_stamp`. |
| [`scripts/gui/tab_video.py`](../scripts/gui/tab_video.py) | `_publish_task_state` (the throttled video mirror) and the `DONE` handler. |
| Runners | The `DONE` payloads: `batch_upscale.py`, `tag_and_rename.py`, `conciliate.py`, and `batch_video_upscale._done_payload` / `_run_finished`. |
| [`samples/home-assistant/`](../samples/home-assistant/) | The user-facing side: sensors, template sensors, two dashboards, `automations-ui.yaml`, and a README that is the setup guide. These are **pasted into the user's own configuration**, so nothing can update them in place: every file carries version markers (`# --- Added in 0.5.3 ---`, `# NEW: version 0.5.8`, and `# 0.5.8:` for a widened meaning that needs no re-copy) and the README's *Upgrading* table says which kind is patched and which is re-pasted. **Adding a topic means adding its marker here too**, or an existing user never learns it exists. |

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>

## Tests

Stdlib + pytest, no broker and no tkinter window required.

| File | Pins |
|---|---|
| [`tests/test_mqtt_publisher.py`](../tests/test_mqtt_publisher.py) | Retain semantics: a retained publish is remembered, an event is not, and a reconnect replays state but never events. Exercised with no live client, which is also the "published before the link was up" path. |
| [`tests/test_video_mqtt.py`](../tests/test_video_mqtt.py) | The video `DONE` payload (shared key names, the reductions, the cost sum, JSON-serialisability), the single end-of-run seam, the runner-to-tab wire round-trip through the **real** emitter and parser, the retain/qos flags on each topic, and that timestamps carry an offset. |
| [`tests/test_ha_samples.py`](../tests/test_ha_samples.py) | The samples themselves: every file parses, every Jinja template compiles, every topic and entity referenced by the automations **and both dashboards** actually exists (in `mqtt-sensors.yaml` or `template-sensors.yaml`), the retained-route guards are present, the derived progress-percent template renders for a video run / an idle app / a malformed value, every automation is a **standalone mapping** with no `id` / `initial_state` (the shape the UI's YAML editor accepts; a list is what it rejects), and the two "finished" automations **partition** every runner's real `DONE` payload (rendered against all four, exactly one fires). |

<div align="right"><a href="#mqtt--home-assistant-integration">↑ Back to top</a></div>
