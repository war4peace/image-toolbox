# Telemetry design (in-app graphs #9 + Home Assistant foundation)

> Planning deliverable for future-features **#9 (telemetry usage graphs)** and the
> telemetry half of **#10 (Home Assistant dashboard samples)**. Written 2026-07-22.
> This is the design of record; it becomes the work-order once green-lit.
>
> **Status:** Phases A (widen the telemetry model), B (the matplotlib graph
> window + per-run history) and C (the `samples/home-assistant/` dashboards, #10)
> are all **implemented** on 0.5.3-experimental. Only the dashboard screenshots
> remain (a live Home Assistant capture).
>
> **Decisions locked (2026-07-22):** add GPU utilization %, power (draw + limit),
> and core clock to the sampled set; skip fan speed. Keep RAM/VRAM published as
> raw MB only (no derived `%` topics); consumers derive `%` from used/total. Ship
> Home Assistant as manual paste-in sample YAML for now (defer MQTT Discovery).
>
> **Rendering (2026-07-22, locked):** the graph window is **matplotlib** (embedded
> `FigureCanvasTkAgg`), not hand-drawn tkinter Canvas. A blitted crosshair makes
> hover smooth; range zoom is `set_xlim`; axes/ticks/legends/resize come for free.
> matplotlib is already present on Local/Both (a seedvr2 dependency); it is added
> to the **Remote-only** bootstrap so a rented-pod graph works there too. Imported
> **lazily and fail-safe** (like libVLC): absent matplotlib disables only the graph
> window, never the readout row or MQTT. This is a deliberate third GUI dependency.
>
> **Graph model (2026-07-22, refined):** graphs are **per-run** and valid for that
> run only (in-memory); continuous idle local sampling feeds only the readout row,
> never a graph. Y-axis ceilings are **pinned to hardware capacity** (RAM total,
> VRAM total, power limit), never auto-scaled to the observed maximum. Range
> buttons (1h/3h/…) enable **dynamically** as the run's runtime surpasses them, and
> act as **global zoom-to-recent toggles** over a default whole-run view.

---

## 1. Why this comes before the graphs

The graph window (#9) and the HA dashboards (#10) both read *the same* telemetry.
Building either on today's 6-field sample would bake in the gaps, so the data
model is widened **first**, once, and both features consume the widened set.

The single most important gap: **GPU core utilization is not sampled at all**
today. The app tracks VRAM and temperature but not whether the GPU is actually
*working*, which is the headline series any "usage graph" exists to show. It is
free to add: `nvidia-smi` already returns it in the one call the app makes.

## 2. The telemetry data model (widened)

One sample dict, identical shape on local and remote (the pod worker mirrors it),
so **one** history buffer, **one** graph window class and **one** MQTT publish
map serve both. Fields (new ones marked ★):

| Key | Meaning | Source | Notes |
|---|---|---|---|
| `cpu` | CPU load % | GetSystemTimes / `/proc/stat` | unchanged |
| `ram_used_mb` / `ram_total_mb` | physical RAM | GlobalMemoryStatusEx | unchanged, **MB only** |
| `gpu_used_mb` / `gpu_total_mb` | VRAM | nvidia-smi `memory.used/total` | unchanged, **MB only** |
| `gpu_temp_c` | GPU temperature | nvidia-smi `temperature.gpu` | unchanged |
| ★ `gpu_util_pct` | GPU core load % | nvidia-smi `utilization.gpu` | the headline missing signal |
| ★ `gpu_power_w` | power draw, watts | nvidia-smi `power.draw` | throttle / degradation story |
| ★ `gpu_power_limit_w` | power cap, watts | nvidia-smi `power.limit` | graph reference ceiling for draw |
| ★ `gpu_clock_mhz` | core clock, MHz | nvidia-smi `clocks.gr` | corroborates throttling |

**Everything stays best-effort / fail-safe to `None`.** A field the card does not
report (`[N/A]`, common for `power.limit`/`clocks` on some cards) parses to `None`
and is simply absent from the row, the graph and MQTT, exactly as VRAM already
degrades on a GPU-less host.

### 2.1 The one `nvidia-smi` call

Widen the existing single query (no extra subprocess, same cost):

```
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.gr --format=csv,noheader,nounits
```

The parser must change from positional `int()` on 3 fixed fields to a
**per-field safe parse** keyed by column order: split the line, then for each
column try `int(float(x))` and yield `None` on `ValueError` / `[N/A]`. This makes
the addition of any future column a one-line change and immunises the parse
against a single unsupported field poisoning the whole sample.

Local (`system_telemetry.sample_gpu`) and remote (`pod/worker._sample_gpu`) share
the identical query string and parse logic; keep them literally in sync (a short
comment on each pointing at the other). `sample_gpu` returns a **dict** now, not a
3-tuple, so new fields don't reshuffle positional unpacking at the one call site
(`App._telemetry_worker`).

### 2.2 What we deliberately did NOT add

- **Derived `%` topics (`ram_pct`, `vram_pct`).** Kept MB-only. The in-app graph
  and readout row already compute `%` from used/total in memory; HA templates it
  in the sensor (one `value_template`). Fewer topics, one source of truth for the
  raw numbers.
- **Fan speed.** Reports `[N/A]` on most rented/datacenter cards and many laptops;
  lowest signal-to-noise of the candidates.
- **Throughput as sampled telemetry.** Seconds/image, ETA, progress stay in the
  event-driven `task/*` topics (they are per-item events, not a time series). The
  graph is a *system-load* view; mixing in throughput would muddy both.

## 3. MQTT surface (additions)

Additive only: every existing topic keeps its name and meaning. New retained
scalar topics under the same `system/` (and mirrored `system/remote/`) base, in
`mqtt_publisher.py` as new constants, published from `App._apply_telemetry` /
`apply_remote_telemetry` beside the current ones:

| Topic | Payload | Unit |
|---|---|---|
| `image-toolbox/system/gpu_util` | GPU core load | `%` |
| `image-toolbox/system/gpu_power` | power draw | `W` |
| `image-toolbox/system/gpu_power_limit` | power cap | `W` |
| `image-toolbox/system/gpu_clock` | core clock | `MHz` |
| `image-toolbox/system/remote/gpu_util` | (remote pod) | `%` |
| `image-toolbox/system/remote/gpu_power` | (remote pod) | `W` |
| `image-toolbox/system/remote/gpu_power_limit` | (remote pod) | `W` |
| `image-toolbox/system/remote/gpu_clock` | (remote pod) | `MHz` |

`clear_remote_telemetry` zeroes the four new remote topics too (so a torn-down pod
leaves no stale non-zero reading in HA), matching the existing six.

### 3.1 Sample-sensor file fixes (part of the foundation)

The sample-sensor file (now `samples/home-assistant/mqtt-sensors.yaml`, moved
there from `docs/` in Phase C) is the single place a user is sent. As part of
this work, regardless of the dashboards (#10):

1. **Add the four new GPU sensors** (local + remote), with `unit_of_measurement`.
2. **Add the two already-published-but-missing sensors** `task/progress` and
   `task/eta` (the app publishes them; the file omits them).
3. **Fix `last_run`**: it is a JSON object (runner summary + `tool` /
   `finished_at`), not a scalar. Bound as a plain `state_topic` it exceeds HA's
   255-char state limit and shows truncated. Rebind with `json_attributes_topic`
   (fields become attributes) plus a short `value_template` for the state (e.g.
   the tool name or `finished_at`). Dashboards then read fields via attribute
   templates.

MQTT **Discovery** (retained `homeassistant/.../config`, auto-created device +
entities, no paste) is explicitly **out of scope for this pass**: it is a real
app-side feature (unique_ids, availability wiring, config lifecycle) and the
dependency-light samples approach covers the need now. Logged as a separate future
item, not scheduled.

## 4. In-app telemetry graphs (#9)

### 4.0 A graph is per-run, not app-lifetime

**Every graph belongs to one run and is valid for that run only.** A "run" is a
period of active processing: a local batch/tag/video run on this machine, or a
remote-pod run (each already has a clear start and end). This unifies local and
remote, which the earlier draft split.

- **Continuous idle local sampling stays informational-only:** it keeps the
  readout **row** live (watch VRAM free up between runs) but does **not** build a
  graph history. No run in progress means no new graph.
- A run's history is **created at its start**, appended to for its duration, and
  **sealed at its end** (local *and* remote alike). If no run has happened this
  session, there is simply no graph to open.
- **Sealing freezes the plotted data but keeps the window interactive.** When the
  run ends the series stop growing (live redraw stops), yet the user can still
  open the graph and work the 1h/3h/6h/… range toggles to review the finished run.
  A sealed run is read-only, not gone.
- **Reset on the next run:** starting a new run of the same source discards the
  previous run's frozen history and begins a fresh one. Only the most recent
  run (live or sealed) is ever held.
- **Destroyed on app close:** it is in-memory only, so nothing survives a restart.
- Because history is bounded by run length, the old "24 h only fills after 24 h of
  uptime" caveat is gone: the graph's span is exactly the run's elapsed time.
- **Timeline anchors to the first sample, not run-start** (`TelemetryHistory.
  anchor_ts`). A batch's start includes a long eligibility/scan phase that does no
  GPU work and emits no telemetry; anchoring to run-start would leave the graph
  blank for those minutes. So `bounds()` (the whole-run x-axis), `runtime()` (which
  drives the range-button enablement) and the "started HH:MM" header all begin at
  the first recorded sample, i.e. when the first image/video is actually processed.

### 4.1 `TelemetryHistory` (new, pure, unit-testable)

Lives in `system_telemetry.py` (tkinter-free, so it unit-tests without a display).
One buffer **per active run**, keyed by source (`local`, `remote:<tab-id>`):

- `start(ts)` opens a run buffer (drops any previous run's, or keeps it as the
  "last sealed" for viewing until this one produces its first point);
- `append(ts, sample)` (wall-clock `ts` from the sampler);
- `seal()` marks the run ended (buffer frozen, still queryable);
- `series(field) -> (times, values)` arrays for plotting. matplotlib renders the
  full run (a few thousand points is trivial) and range zoom just moves
  `set_xlim`, so no manual decimation is needed. The buffer still **inserts a
  `NaN`** where adjacent samples are more than ~3x the median interval apart, so
  matplotlib **breaks the line** across a gap (a pod that briefly went away)
  instead of interpolating a straight line across it. `enabled_spans(now)` returns
  which range buttons are live (runtime >= span), driving 4.4.

In-memory only. Volume is trivial: even a long run at the 5 s upscale cadence is a
few thousand points per series.

Hooked with one line each in `App._apply_telemetry` (source `local`, appends only
while a local task is active) and `apply_remote_telemetry` (source
`remote:<tab>`). Run start/seal ride the existing run-lifecycle hooks
(`ToolTab` start / `on_exit`; the remote row's start / `clear_remote_telemetry`).

### 4.2 Axis ceilings are pinned to hardware capacity

Y-axes are **never auto-scaled to the observed maximum.** Each metric's ceiling is
its hardware capacity, so the graph honestly shows *headroom*, not just relative
wiggle:

| Series | Y-axis ceiling |
|---|---|
| RAM (GB) | system RAM total (`ram_total_mb`) |
| VRAM (GB) | card VRAM total (`gpu_total_mb`) |
| GPU power (W) | power limit (`gpu_power_limit_w`), drawn as a faint ceiling line |
| CPU %, GPU util % | 100 (intrinsic percentages) |
| GPU temp (°C) | the one exception: no capacity metric in the query, so **fixed 0-100 °C** (locked 2026-07-22). Revisit only if a card is ever seen above 100 °C, which is thermal-shutdown territory and highly unlikely. |

RAM/VRAM/power thus read directly against what the card/box actually has, exactly
as the user framed it ("max RAM/VRAM graph value = the nvidia-smi total"). A run
that pins VRAM to the top of the chart is visibly at the wall; one that sits low
visibly has room.

### 4.3 `TelemetryGraphWindow` (new `gui/` module)

Follows the established shared-instance floating-window pattern (log window /
`ComparisonWindow`): non-modal `Toplevel`, one shared instance per source,
geometry persisted in `gui_settings.json` as `telemetry_geometry` (sibling of
`compare_geometry`). Rendered with **matplotlib** embedded via
`FigureCanvasTkAgg` (locked; see the decisions block), imported lazily and
fail-safe. Title shows the run (e.g. the tool + the pod's GPU) and the run start
time.

Why matplotlib over hand-drawn Canvas: the throwaway previews (both stdlib and
matplotlib) proved the hover-lag was unthrottled per-event redraw, not the
toolkit; but matplotlib still wins on substance: axes, ticks, tick-formatting,
legends and resize are free, range zoom is a one-line `set_xlim` (no manual
reslice/decimation/tick math), and a **blitted** crosshair (cache the background,
redraw only the moving artists, blit that region) is smooth. It is already a
Local/Both dependency; Remote-only gains it via bootstrap.

**Series, grouped into three thin stacked charts** so units never share an axis,
each with its capacity-pinned ceiling from 4.2:
1. **Load %** (0-100): CPU %, GPU util %, colour-matched to the row's band palette.
2. **Memory** (GB): RAM and VRAM, each against its own total.
3. **Power** (W): GPU power draw under the `power_limit` ceiling line.

GPU temperature rides chart 3 as a light secondary trace (or a compact chart 4 if
it reads cluttered); core clock (MHz) is not charted (it tracks power) but shows in
the hover readout. All charts plot by **timestamp, never by sample index** (cadence
is 5/30/10 s). While the run is live the window appends each new sample and calls
`draw_idle` (which re-caches the blit background); once sealed the **data** is
frozen (no updates) but the window and its range toggles stay interactive for
review (§4.0). Read-only, fail-safe.

### 4.4 Range control: dynamic, toggle, global

The default view is the **whole run** (`window(None)`). Above the charts sits one
row of range buttons: **1h · 3h · 6h · 12h · 24h**.

- **Dynamic enable:** a button is **disabled while the run's total runtime is below
  its span** (you cannot ask for "the last hour" of a 20-minute run). Each crosses
  from disabled to enabled the moment runtime passes it.
- **Toggle:** pressing an enabled button zooms the view to the **most recent** span
  of that length (older samples retained in the background); pressing the **same**
  button again reverts to the whole-run view. Selecting a different span switches
  to it. So the state is one of {whole-run (default), last-1h, last-3h, …}, and
  re-pressing the active span returns to whole-run.
- **Global (per-window):** the active range applies to **every stacked chart at
  once** in that window (one control governs the whole window, not per-chart), so
  its panels always share the same time window. Scope is **per-window** (locked
  2026-07-22): the local and the remote graph windows keep **independent** range
  selections, since they are different runs on different timelines.

### 4.5 Opening the window

Clicking anywhere in a telemetry row opens the graph window for **that** source's
current-or-last run (local row -> local run history; a tab's remote row -> that
tab's pod run). `TelemetryRow` destroys and recreates its labels on every `_set`,
so the `<Button-1>` binding must be applied **inside `_set`** as each label is
created (plus once on the frame), or clicks land on stale widgets. Clicking during
idle with no run this session is a no-op (nothing to show).

### 4.6 Readout row (`TelemetryRow.show`)

Add **GPU util %** as a band-coloured segment (like CPU) next to VRAM: it is the
headline new signal and belongs in the always-visible row, not only the graph.
Power/clock stay graph- and MQTT-only to keep the single line compact (especially
the Upscale tab's paired local+remote rows). Final segment order is a small UI
call during dev; util-in-row is the requirement.

## 5. Work breakdown

**Phase A: widen the telemetry model (the foundation, ships on its own).**
1. `system_telemetry.sample_gpu`: widen the query, per-field safe parse, return a
   dict. Update the one caller (`App._telemetry_worker`).
2. `pod/worker.py` `_sample_gpu` / `_sample_telemetry`: identical widening (keep
   in sync with the local copy).
3. `mqtt_publisher.py`: four new local + four new remote topic constants.
4. `App._apply_telemetry` / `apply_remote_telemetry` / `clear_remote_telemetry`:
   publish (and zero, for remote) the new topics.
5. `TelemetryRow.show`: add the banded GPU-util segment.
6. the sample-sensor file (`samples/home-assistant/mqtt-sensors.yaml`): add the
   four GPU sensors + `task/progress` + `task/eta`, and fix the `last_run` JSON
   binding.
7. `remote_upscale_engine.telemetry` docstring: update the field list.
8. Tests: extend the telemetry parse test with `[N/A]` fields and the new columns;
   assert the new MQTT topics publish when present and are absent when `None`.

**Phase B: in-app graphs (#9).**
9. `TelemetryHistory` in `system_telemetry.py` + unit tests: per-run
   `start`/`append`/`seal`, `window(None | span)`, decimation, gap break, and the
   "runtime >= span -> range enabled" predicate.
10. Run-lifecycle wiring: `start` a local run buffer on a local task start and
    `seal` it on `on_exit` (idle sampling does **not** append); the remote source
    starts on first remote sample and seals via `clear_remote_telemetry`.
11. `TelemetryGraphWindow` (`gui/`), **matplotlib** via `FigureCanvasTkAgg`,
    imported lazily and fail-safe: capacity-pinned stacked charts (`set_ylim`),
    the dynamic/global range-toggle bar (`set_xlim`, 4.4), a blitted crosshair
    readout, `telemetry_geometry` in `gui_settings`, the `<Button-1>` open binding
    on the row, `draw_idle` append while live, static once sealed. If matplotlib
    fails to import, the row's click is a no-op with a one-line "install to enable"
    hint (mirrors the libVLC-absent path); the readout row + MQTT are unaffected.
12. **Bootstrap:** add `matplotlib` (+ its `numpy` dep) to the **Remote-only**
    branch of `bootstrap.ps1` (Local/Both already have it via seedvr2), and add
    matplotlib to CLAUDE.md's deliberate-GUI-dependency list (with `paho-mqtt` and
    `python-vlc`) at implementation time.

**Phase C: HA dashboards (#10, separate deliverable).** The `samples/home-assistant/`
folder (core + custom Lovelace YAML, README, screenshots) as scoped in
`future-features.md` #10. It consumes Phase A's widened topics and the fixed
sensor file; no further app code.

## 6. Risk

Low throughout. Phase A is additive and fail-safe (a missing metric is `None`
everywhere, exactly like a missing GPU today); no runner or protocol change. Phase
B is read-only and in-memory; its one new element is the **matplotlib** GUI
dependency, contained by lazy + fail-safe import (absent matplotlib disables only
the graph window) and already present on Local/Both, so the only real delta is the
Remote-only bootstrap download. Phase C touches no pipeline code.
