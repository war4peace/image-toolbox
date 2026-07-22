"""
gui/app.py
----------
The App window (tk.Tk) that hosts the six tabs plus main(). Split out of the
former toolbox_gui.py (0.4.3). Constructs the tabs, owns the shared log/comparison
windows, the bottom status bar + funds readout, system-telemetry sampling, MQTT
and taskbar publishing, and the single-instance/DPI startup in main().
"""

import os
import sys
import time
import datetime
import threading

import updater
import mqtt_publisher
import system_telemetry
import taskbar_progress
import runpod_client
import config_store

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import).
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass
# Single-instance guard is optional — a packaging miss must not brick startup.
try:
    import single_instance
except Exception:
    single_instance = None

import tkinter as tk
from tkinter import ttk, messagebox

# App's slice of the shared foundation + widgets + windows + tabs (the rest of
# the package). Only what App itself references; each tab imports its own needs.
from gui.common import (
    APP_ROOT, APP_TITLE, APP_VERSION, CFG, save_config,
    update_auto_check_enabled, update_skipped_version, report_issue,
    _FUNDS_GREY, funds_color, config_funds_floor, fmt_funds,
    mqtt_config, mqtt_enabled, load_settings, save_settings, _geometry_on_screen,
)
from gui.widgets import Tooltip, LogViewer
from gui.comparison import ComparisonWindow
from gui.tooltab import ToolTab
from gui.tab_upscale import UpscaleTab

from gui.tab_tag import TagTab

from gui.tab_settings import SettingsTab

from gui.tab_runpod import RunPodTab

from gui.tab_conciliate import ConciliateTab

from gui.dialogs import UpdateDialog

from gui.tab_video import VideoTab

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.minsize(900, 560)
        # App icon for the title bar and taskbar button (app.ico at APP_ROOT, shipped
        # to {app} by the installer). `default=` sets it as the interpreter default, so
        # the root AND every Toplevel created afterwards (Compare, Segments, the log
        # window, dialogs, the wizard) inherit it instead of the tkinter feather. Set on
        # the root before any child window is created. Fail-safe: a missing icon just
        # falls back to the default, never blocks startup.
        try:
            self.iconbitmap(default=os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.settings = load_settings()
        self._last_normal_geo = None
        self.log_window = None          # single shared LogViewer for both tools
        self.comparison_window = None   # single shared ComparisonWindow (images)
        self.video_comparison_window = None  # single shared VideoComparisonWindow (frames)
        self.video_playback_window = None    # single shared VideoPlaybackWindow (libVLC)
        self._migrate_default_folders()
        self._migrate_secrets_to_overlay()
        self._restore_geometry()
        self._install_picklist_wheel_guard()

        self.nb = ttk.Notebook(self)
        self.upscale_tab    = UpscaleTab(self.nb, self)
        self.video_tab      = VideoTab(self.nb, self)
        self.tag_tab        = TagTab(self.nb, self)
        self.conciliate_tab = ConciliateTab(self.nb, self)
        self.settings_tab   = SettingsTab(self.nb, self)
        self.runpod_tab     = RunPodTab(self.nb, self)
        self.nb.add(self.upscale_tab,    text="  Batch Upscaler  ")
        self.nb.add(self.tag_tab,        text="  Tag & Rename  ")
        self.nb.add(self.conciliate_tab, text="  Conciliation  ")
        self.nb.add(self.video_tab,      text="  Video Upscaler  ")
        self.nb.add(self.settings_tab,   text="  Settings  ")
        self.nb.add(self.runpod_tab,     text="  RunPod  ")
        # Tabs whose selection is remembered across restarts (gui_settings.json
        # "last_tab", item 3). Settings and RunPod are deliberately excluded, so the
        # app never reopens on a config screen: the app reopens on the last TOOL tab
        # the user was on (or the default first tab if that was Settings/RunPod).
        self._savable_tabs = [
            ("upscale",    self.upscale_tab),
            ("tag",        self.tag_tab),
            ("conciliate", self.conciliate_tab),
            ("video",      self.video_tab),
        ]
        # RunPod funds readout state (bottom bar). Cached briefly so switching tabs
        # doesn't hammer the balance API.
        self._funds_cache = None
        self._funds_cache_at = 0.0
        self._funds_fetching = False
        self._funds_live_job = None
        # Bottom status bar with a right-aligned "Report an issue" link (Future
        # Feature #3). Packed before the notebook so it reserves the bottom strip;
        # always visible regardless of the active tab.
        self._build_statusbar()
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        # System telemetry (Feature #3a): one CPU sampler for the whole app, and
        # the per-tab readout rows it feeds. Samples run off the UI thread; the
        # lock keeps a slow nvidia-smi call from overlapping with the next tick.
        self._cpu_sampler   = system_telemetry.CpuSampler()
        self._telemetry_lock = threading.Lock()
        self.telemetry_rows = [t.telemetry_row for t in
                               (self.upscale_tab, self.tag_tab,
                                self.conciliate_tab, self.video_tab)
                               if t.telemetry_row is not None]
        # Per-run telemetry history + graph windows (usage graphs, #9). Keyed by
        # source: "local" (one machine, fanned out to every tab's local row) and
        # "remote:<id(tab)>" (each tab's pod). A history records only between a
        # run's start and seal; the window is a lazy, one-per-source matplotlib
        # Toplevel opened by clicking a telemetry row.
        self._tel_history = {}
        self._tel_windows = {}
        self._tel_titles = {}
        # Idle sampler: keep the readout live between runs (e.g. so the user can
        # watch another app's VRAM free up before starting an upscale). Fires on
        # a slow 60 s cadence and only when no task is running — task-driven
        # sampling owns the readout during a run.
        self._idle_telemetry_job = None
        self.after(2000, self._idle_telemetry_tick)
        # Keep the RunPod funds readout LIVE during a run: it otherwise only refreshed
        # on a tab-switch / remote-toggle, so a long remote job left it frozen at the
        # run-start balance (a ~$1 drift after an hour of billing). This slow tick
        # re-queries the balance on a cadence; refresh_funds_readout self-gates (fetches
        # only when a remote-capable tab is active + remote-enabled) and the ~30 s cache
        # keeps rapid tab-switches from stacking on top of it, so it never hammers the API.
        self._funds_live_job = None
        self.after(self.FUNDS_LIVE_MS, self._funds_live_tick)

        # Guard against leaving the Settings tab with unsaved edits.
        self._suppress_tab_event = False
        self._prev_tab_widget    = self.nb.nametowidget(self.nb.select())
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # Reopen on the tab the user last left (item 3). AFTER the binding, so the
        # restored tab gets the same on-enter / funds-readout treatment as a click.
        self._restore_last_tab()

        self.bind("<Configure>", self._track_geometry)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Persistent MQTT client (Home Assistant integration). Starts here so the
        # availability LWT is registered before anything else happens.
        self.mqtt = None
        if mqtt_enabled():
            self.start_mqtt()

        # Shortly after the window is up, run the startup checks off the UI
        # thread: one GitHub release check feeds both the update prompt (if the
        # auto-check is on) and the MQTT version/update topics (if enabled).
        self._update_dialog = None
        if update_auto_check_enabled() or mqtt_enabled():
            self.after(1500, lambda: threading.Thread(
                target=self._startup_worker, daemon=True).start())

        # Benchmark corpus auto-refresh (#8): silently pull the community video-benchmark
        # dataset from GitHub in the background so the local set is kept current with no button
        # and no prompt. Fail-safe; local measurements always take precedence on import.
        self.after(2000, lambda: threading.Thread(
            target=self._startup_bench_sync, daemon=True).start())

        # First-start Wizard (0.4.6): one-time GPU-aware model setup. Shown just
        # after the window is up (before the 1500 ms update worker even starts, so
        # the two modal dialogs never fight for the grab on a fresh install).
        self.after(600, self._maybe_show_wizard)

    def _install_picklist_wheel_guard(self):
        """Stop a mouse-wheel scroll over a ttk Combobox/Spinbox from silently
        changing its value — a Windows footgun that can flip a setting unnoticed
        (and then trip the unsaved-changes guard). Replaces the default class
        binding once, so it covers every current and future picklist; the wheel
        is redirected to the nearest scrollable canvas so the surrounding page
        still scrolls. The open dropdown list is a separate widget class, so it
        keeps its own scrolling."""
        for cls in ("TCombobox", "TSpinbox"):
            self.bind_class(cls, "<MouseWheel>", self._picklist_wheel)

    def _picklist_wheel(self, event):
        # Forward the scroll to the first scrollable Canvas ancestor (if any),
        # then return "break" so the widget's own value-changing binding — and
        # the page's bind_all handler — don't also fire.
        w = getattr(event.widget, "master", None)
        while w is not None:
            if isinstance(w, tk.Canvas):
                try:
                    w.yview_scroll(int(-event.delta / 120), "units")
                except tk.TclError:
                    pass
                break
            w = getattr(w, "master", None)
        return "break"

    def _build_statusbar(self):
        """A thin bottom strip with a right-aligned 'Report an issue' link (Future
        Feature #3) and a left-aligned RunPod 'Funds' readout. The link opens a
        pre-filled GitHub new-issue page; the funds readout shows the account
        balance (coloured against the funds floor) on the remote-capable tabs."""
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        link = tk.Label(bar, text="Report an issue", fg="#3a86ff",
                        cursor="hand2", font=("Segoe UI", 9, "underline"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: report_issue())
        # Subtle hover feedback (darker blue) so it reads as a link.
        link.bind("<Enter>", lambda _e: link.configure(fg="#1a5fd0"))
        link.bind("<Leave>", lambda _e: link.configure(fg="#3a86ff"))
        # Funds readout on the far left of the same row. Disabled (grey) unless the
        # active tab bills a remote pod: Batch Upscaler / Tag & Rename only when
        # 'Run on remote pod' is ticked; the Video Upscaler always.
        self.funds_caption = tk.Label(bar, text="Funds:", fg=_FUNDS_GREY,
                                      font=("Consolas", 9))
        self.funds_caption.pack(side="left")
        self.funds_value = tk.Label(bar, text="n/a", fg=_FUNDS_GREY,
                                    font=("Consolas", 9))
        self.funds_value.pack(side="left", padx=(4, 0))
        self._funds_shown = True     # tracks whether the two labels are packed
        Tooltip(self.funds_value,
                "Your RunPod account balance, coloured by how far it sits above the "
                "funds floor (Settings -> RunPod). Active only when the current tab "
                "runs on a remote pod. 'Unknown' if it can't be read.")

    # ── RunPod funds readout (bottom bar) ────────────────────────────────────

    FUNDS_LIVE_MS = 60000   # re-query the account balance at most this often

    def _funds_live_tick(self):
        """Periodic balance refresh so the bottom-bar funds readout stays live during a
        remote run (not just on tab-switch). refresh_funds_readout self-gates: it only
        fetches when a remote-capable tab is active and remote-enabled, so this is a
        no-op cost otherwise. Fail-safe; reschedules itself for the app's lifetime."""
        try:
            self.refresh_funds_readout()
        except Exception:
            pass
        self._funds_live_job = self.after(self.FUNDS_LIVE_MS, self._funds_live_tick)
    def _selected_widget(self):
        try:
            return self.nb.nametowidget(self.nb.select())
        except Exception:
            return None

    def _funds_visible_for(self, tab):
        """Whether to SHOW the readout at all. Hidden on Conciliation and Settings
        (they never bill a pod, so it would only ever read 'n/a' there). Shown on
        the three tool tabs and the RunPod tab."""
        return tab in (self.upscale_tab, self.tag_tab, self.video_tab, self.runpod_tab)

    def _funds_enabled_for(self, tab):
        """Is the readout LIVE (fetches + colours) for this tab? Always for the
        Video Upscaler and the RunPod tab; for Batch Upscaler / Tag & Rename only
        when 'Run on remote pod' is ticked (otherwise it shows a grey 'n/a')."""
        if tab in (self.video_tab, self.runpod_tab):
            return True
        if tab in (self.upscale_tab, self.tag_tab):
            rv = getattr(tab, "remote_var", None)
            return bool(rv and rv.get())
        return False

    def _show_funds_readout(self, show):
        """Pack or unpack the funds labels (far left of the status bar)."""
        if show and not self._funds_shown:
            self.funds_caption.pack(side="left")
            self.funds_value.pack(side="left", padx=(4, 0))
            self._funds_shown = True
        elif not show and self._funds_shown:
            self.funds_caption.pack_forget()
            self.funds_value.pack_forget()
            self._funds_shown = False

    def refresh_funds_readout(self):
        """Update the bottom-bar funds readout for the active tab. Hidden on
        Conciliation / Settings; grey 'n/a' when a tool tab isn't running remote;
        otherwise the (cached, then freshly fetched) balance. Fail-safe."""
        try:
            tab = self._selected_widget()
            if not self._funds_visible_for(tab):
                self._show_funds_readout(False)
                return
            self._show_funds_readout(True)
            if self._funds_enabled_for(tab):
                self._render_funds()          # show what we have immediately
                self._fetch_funds_async()     # then refresh in the background
            else:
                self.funds_caption.configure(fg=_FUNDS_GREY)
                self.funds_value.configure(text="n/a", fg=_FUNDS_GREY)
        except Exception:
            pass

    def _render_funds(self):
        """Paint the funds value from the cache, coloured against the saved floor."""
        text, bal = fmt_funds(self._funds_cache)
        self.funds_caption.configure(fg=_FUNDS_GREY)
        self.funds_value.configure(text=text, fg=funds_color(bal, config_funds_floor()))

    def _fetch_funds_async(self):
        """Fetch the balance off the UI thread (cached ~30 s to avoid hammering the
        API on rapid tab switches). No key → 'Unknown'. Fail-safe throughout."""
        key = (CFG.get("runpod", {}).get("api_key") or "").strip()
        if not key:
            self._funds_cache = None
            self._render_funds()
            return
        if self._funds_fetching:
            return
        if self._funds_cache is not None and (time.time() - self._funds_cache_at) < 30:
            return
        self._funds_fetching = True

        def work():
            info = runpod_client.account_balance(key)   # fail-safe (None on failure)

            def apply():
                self._funds_fetching = False
                self._funds_cache = info
                self._funds_cache_at = time.time()
                if self._funds_enabled_for(self._selected_widget()):
                    self._render_funds()
            try:
                self.after(0, apply)
            except Exception:
                self._funds_fetching = False

        threading.Thread(target=work, daemon=True).start()

    def set_funds_cache(self, info):
        """Adopt a balance fetched elsewhere (the RunPod tab's Refresh) as the shared
        cache and repaint the bottom-bar readout, so the two stay in sync."""
        self._funds_cache = info
        self._funds_cache_at = time.time()
        self.refresh_funds_readout()

    def _migrate_secrets_to_overlay(self):
        """One-time: an older install kept secrets (RunPod key, MQTT password,
        notification tokens/webhooks) inside the tracked config.json. If any are
        still there, resave through config_store, which writes them to the
        untracked config.local.json overlay and blanks them in config.json - so the
        tracked file stops holding credentials (item 9). Idempotent: once migrated,
        config.json has no secret left and this is a no-op. Fail-safe: CFG already
        carries the merged values, so a save here never loses a secret."""
        try:
            if config_store.base_has_secrets(APP_ROOT):
                save_config()   # partitions CFG into config.json + config.local.json
        except Exception:
            debug_log("App._migrate_secrets_to_overlay")

    def _maybe_show_wizard(self):
        """Show the one-time first-start Wizard (0.4.6) when it has not run before.
        Fail-safe: a wizard that fails to build must never block the app from
        launching, so any failure is logged and the flag is marked done (so it does
        not retry, and nag, on every subsequent launch). See gui/wizard.py."""
        try:
            from gui.wizard import should_show, FirstStartWizard
            if should_show(self):
                FirstStartWizard(self)
        except Exception:
            debug_log("App._maybe_show_wizard", tb=True)
            # Mark done through the SHARED settings dict (a geometry save would
            # clobber a separate disk write), so a broken wizard doesn't nag every
            # launch.
            try:
                self.settings["wizard_done"] = True
                save_settings(self.settings)
            except Exception:
                pass

    def _migrate_default_folders(self):
        """Carry default folders saved by older builds in gui_settings.json over
        to config.json, where they now live (one-time, before the tabs load)."""
        defs = CFG.setdefault("defaults", {})
        moved = False
        for old_key, new_key in (("default_source", "upscale_source"),
                                  ("default_output", "upscale_output")):
            if not defs.get(new_key) and self.settings.get(old_key):
                defs[new_key] = self.settings[old_key]
                moved = True
        if moved:
            save_config()

    def sync_settings_defaults(self):
        """Push the latest default folders into the Settings tab's fields."""
        st = getattr(self, "settings_tab", None)
        if st is not None:
            st.load_defaults()

    # ── Window geometry persistence ──────────────────────────────────────────

    def _restore_geometry(self):
        geo = self.settings.get("main_geometry")
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "980x720")
        if self.settings.get("main_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

    def _track_geometry(self, event):
        # Remember the last *restored* (non-maximised) geometry so closing while
        # maximised still records a sensible size to come back to.
        if event.widget is self and self.state() == "normal":
            self._last_normal_geo = self.geometry()

    def _save_geometry(self):
        try:
            zoomed = (self.state() == "zoomed")
        except tk.TclError:
            zoomed = False
        geo = self._last_normal_geo or self.geometry()
        self.settings["main_geometry"] = geo
        self.settings["main_zoomed"]   = zoomed
        save_settings(self.settings)

    # ── Unsaved-settings guard ───────────────────────────────────────────────

    def _config_tab_name(self, widget):
        """Name of a settings-style (save-bar) tab, or None for any other tab."""
        if widget is self.settings_tab:
            return "Settings"
        if widget is self.runpod_tab:
            return "RunPod"
        return None

    # ── Last-used tab persistence (item 3) ───────────────────────────────────

    def _savable_tab_key(self, widget):
        """Stable key for a remembered tool tab, or None for Settings/RunPod (never
        recorded) and anything unknown."""
        for key, w in self._savable_tabs:
            if w is widget:
                return key
        return None

    def _restore_last_tab(self):
        """Select the tool tab the user last left (gui_settings.json 'last_tab').
        Fail-safe: an unknown/missing key just keeps the default first tab."""
        key = self.settings.get("last_tab")
        for k, w in self._savable_tabs:
            if k == key and w is not self._prev_tab_widget:
                try:
                    self.nb.select(w)
                except Exception:
                    pass
                return

    def _on_tab_changed(self, event=None):
        """When leaving a settings-style tab (Settings or RunPod) with unsaved
        edits, prompt to save."""
        if self._suppress_tab_event:
            return
        try:
            new_widget = self.nb.nametowidget(self.nb.select())
        except Exception:
            return
        prev = self._prev_tab_widget
        prev_name = self._config_tab_name(prev)
        if prev_name and new_widget is not prev and prev.is_dirty():
            if self._confirm_unsaved("leaving", prev, prev_name) == "cancel":
                # Bounce back to the tab we tried to leave, without re-triggering.
                self._suppress_tab_event = True
                try:
                    self.nb.select(prev)
                finally:
                    self._suppress_tab_event = False
                return   # _prev_tab_widget stays on the settings tab
        self._prev_tab_widget = new_widget
        # Remember the last TOOL tab (item 3): record + persist immediately (crash-safe)
        # only for savable tabs, so Settings/RunPod never become the reopen target.
        key = self._savable_tab_key(new_widget)
        if key is not None:
            self.settings["last_tab"] = key
            save_settings(self.settings)
        # Entering a tool tab: fill any empty folder field from the pinned
        # default, so a default set in Settings after startup takes effect.
        if isinstance(new_widget, ToolTab):
            new_widget.restore_defaults_if_empty()
        elif new_widget is self.video_tab:
            # Re-check remote readiness on entry, so an API key / SSH key / volume
            # configured in the RunPod tab AFTER startup is picked up (not only the
            # one-time check at launch), and refresh the durable queue.
            self.video_tab.on_enter()
        # Update the bottom-bar funds readout for the tab we just entered.
        self.refresh_funds_readout()

    def _confirm_unsaved(self, context, tab=None, name="Settings"):
        """Modal Save / Don't save / Cancel prompt for unsaved edits in a
        settings-style tab. Returns 'ok' (handled — safe to proceed) or 'cancel'
        (stay put). `context` is 'leaving' or 'closing' for the wording."""
        tab = tab or self.settings_tab
        verb = "closing" if context == "closing" else f"leaving the {name} tab"
        ans = messagebox.askyesnocancel(
            APP_TITLE,
            f"You have unsaved changes in {name}.\n\nSave them before {verb}?")
        if ans is None:
            return "cancel"
        if ans:
            return "ok" if tab._save() else "cancel"
        # "Don't save" — discard the edits so the form matches config.json again.
        tab.revert()
        return "ok"

    def active_remote_pod_ids(self):
        """Pod ids currently in use by a running remote task, so the RunPod tab
        won't offer to terminate a pod a live run depends on."""
        ids = set()
        for t in (self.upscale_tab, self.tag_tab, self.video_tab):
            pid = getattr(t, "active_pod_id", None)
            if pid and t.running:
                ids.add(pid)
        return ids

    def notify_active_pods_changed(self):
        """A remote run started/ended: let the RunPod tab re-mark its pod list."""
        rt = getattr(self, "runpod_tab", None)
        if rt is not None:
            rt.on_active_pods_changed()

    def other_tab(self, tab):
        return self.tag_tab if tab is self.upscale_tab else self.upscale_tab

    def set_tag_tab_enabled(self, enabled):
        """Grey out the Tag & Rename tab while an upscale run owns the GPU."""
        self.nb.tab(self.tag_tab, state="normal" if enabled else "disabled")

    def refresh_conciliate_lock(self):
        """Grey out the Conciliation tab while the Batch Upscaler or Tag & Rename
        is running — they may be reading or writing the same folders."""
        busy = self.upscale_tab.running or self.tag_tab.running
        self.nb.tab(self.conciliate_tab, state="disabled" if busy else "normal")

    def local_gpu_job_running(self):
        """True if any tool is running a job on the LOCAL GPU: a Batch Upscale or Tag & Rename
        with the remote-pod toggle OFF, or a Video Upscale in Local-GPU mode. Remote-pod runs
        use no local GPU, so they don't count. Used to lock the Video tab's GPU benchmark,
        which would fight a local run for the card."""
        def local(tab, remote_attr="remote_var"):
            if not getattr(tab, "running", False):
                return False
            rv = getattr(tab, remote_attr, None)
            return not (rv is not None and rv.get())   # no toggle == local
        if local(self.upscale_tab) or local(self.tag_tab):
            return True
        v = self.video_tab
        mode = getattr(v, "mode_var", None)
        return bool(getattr(v, "running", False) and mode is not None and mode.get() == "local")

    def refresh_benchmark_lock(self):
        """Re-evaluate the Video tab's GPU-benchmark button against local-job state. Called
        from every tool's run start/stop transition."""
        fn = getattr(self.video_tab, "_refresh_benchmark_lock", None)
        if fn is not None:
            fn()

    def refresh_tab_exclusivity(self):
        """Exclusivity: while ANY tool run is active, disable every OTHER notebook tab so a
        second run can't be started. Two concurrent runs would fight over the local GPU and
        the shared SQLite cache (the case that motivated this: a local Video Upscale left the
        Batch Upscaler's Start reachable). The running tab stays enabled so its progress and
        Stop button remain in reach; Settings/RunPod are locked too, since a mid-run config
        change is unsafe. Called from every tool's run start/stop transition, where `.running`
        is already accurate. Supersedes the older per-pair locks (which stay as a subset)."""
        tool_tabs = (self.upscale_tab, self.tag_tab, self.conciliate_tab, self.video_tab)
        active = next((t for t in tool_tabs if getattr(t, "running", False)), None)
        for tab_id in self.nb.tabs():
            widget = self.nb.nametowidget(tab_id)
            keep = active is None or widget is active
            self.nb.tab(tab_id, state="normal" if keep else "disabled")

    # ── Shared log window ────────────────────────────────────────────────────

    def show_log(self, console, title):
        """Open (or focus) the single shared log window, bound to `console`."""
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.bind_console(console, title)
            self.log_window.lift()
            self.log_window.focus_set()
        else:
            self.log_window = LogViewer(self, console, title, app=self)

    def rebind_log_if_open(self, console, title):
        """If the log window is open, switch it to follow `console`."""
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.bind_console(console, title)

    # ── Shared comparison window ─────────────────────────────────────────────

    def show_comparison(self, source, output):
        """Open (or re-target) the single shared comparison window for a pair."""
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.show(source, output)
        else:
            self.comparison_window = ComparisonWindow(self, source, output, app=self)

    # ── Updates ──────────────────────────────────────────────────────────────

    def _startup_worker(self):
        """
        Launch-time background task (off the UI thread): check GitHub once, then
        prompt for a not-skipped update (if the auto-check is on) and/or publish
        the version snapshot to MQTT (if enabled).
        """
        status, payload = updater.check_for_update(APP_VERSION)
        update_available = (status == "update")
        latest = payload.version if update_available else APP_VERSION

        if self.mqtt is not None:
            self.mqtt.publish_many({
                mqtt_publisher.VERSION_TOPIC:        APP_VERSION,
                mqtt_publisher.UPDATE_TOPIC:         "yes" if update_available else "no",
                mqtt_publisher.LATEST_VERSION_TOPIC: latest,
            })

        if (update_auto_check_enabled() and update_available
                and payload.version != update_skipped_version()):
            self.after(0, lambda: self.show_update_dialog(payload))

    def _startup_bench_sync(self):
        """Launch-time background task (off the UI thread): refresh the local benchmark cache
        from the community corpus on GitHub, falling back to the shipped CSV when offline.
        Silent and fail-safe; never touches the UI. Local results take precedence on import."""
        try:
            import video_benchmark as vb
            vb.auto_update(local_csv=os.path.join(APP_ROOT, "docs", "video-benchmarks.csv"))
        except Exception as exc:                           # noqa: BLE001 (fail-safe)
            debug_log("App._startup_bench_sync", exc=exc)

    # ── MQTT (Home Assistant) ────────────────────────────────────────────────

    def start_mqtt(self):
        """Start the persistent MQTT client from the saved config and verify the
        connection (the result is reported via _on_mqtt_state). Best-effort."""
        if not mqtt_enabled():
            return
        if not mqtt_publisher.mqtt_available():
            self._on_mqtt_state(False, "paho-mqtt is not installed.")
            return
        client = mqtt_publisher.MqttClient(mqtt_config(), status_cb=self._on_mqtt_state)
        ok, msg = client.start()
        if ok:
            self.mqtt = client
            self._set_mqtt_status("Connecting…", None)
        else:
            self.mqtt = None
            self._set_mqtt_status(msg, False)

    def _set_mqtt_status(self, text, connected):
        st = getattr(self, "settings_tab", None)
        if st is not None and hasattr(st, "mqtt_status"):
            color = "#666" if connected is None else ("#1a7f37" if connected else "#b3261e")
            st.mqtt_status.configure(text=f"MQTT: {text}", foreground=color)

    def _on_mqtt_state(self, connected, message):
        """Connection-state callback (runs on the MQTT thread) → update the UI."""
        try:
            self.after(0, lambda: self._set_mqtt_status(message, connected))
        except Exception:
            pass

    def stop_mqtt(self, last_used=None):
        if self.mqtt is not None:
            self.mqtt.stop(last_used=last_used)
            self.mqtt = None

    def restart_mqtt(self):
        """Apply changed MQTT settings: drop any existing client and re-start."""
        self.stop_mqtt()
        if mqtt_enabled():
            self.start_mqtt()

    def flash_attention(self):
        """Flash the taskbar button until the window is brought to the foreground,
        so a notification catches the eye while the user is working in another app
        (e.g. an overnight upscale finishing or degrading). Windows-only, ctypes
        only (matches the dependency-light telemetry/crash-logger pattern) and
        fail-safe — never let a missing API break the GUI. No-op if already focused
        (FLASHW_TIMERNOFG) or not on Windows."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            # winfo_id() is Tk's client HWND; its parent is the top-level window
            # that owns the taskbar button.
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize",    ctypes.c_uint),
                            ("hwnd",      wintypes.HWND),
                            ("dwFlags",   ctypes.c_uint),
                            ("uCount",    ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]

            FLASHW_TRAY      = 0x00000002   # flash the taskbar button
            FLASHW_TIMERNOFG = 0x0000000C   # keep flashing until the window is focused
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              FLASHW_TRAY | FLASHW_TIMERNOFG, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _taskbar(self):
        """Lazily build the taskbar progress controller for this window (the
        button only exists once the window is shown). Fail-safe → None."""
        tb = getattr(self, "_taskbar_obj", "unset")
        if tb == "unset":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
                self._taskbar_obj = taskbar_progress.TaskbarProgress(hwnd)
            except Exception:
                self._taskbar_obj = None
        return self._taskbar_obj

    def taskbar_progress(self, done, total):
        """Paint a green done/total fill on the taskbar button (UI thread)."""
        tb = self._taskbar()
        if tb is not None:
            tb.set_progress(done, total)

    def taskbar_state(self, state):
        """Set the taskbar bar state ('indeterminate'/'error'/'none'/…)."""
        tb = self._taskbar()
        if tb is not None:
            tb.set_state(state)

    def taskbar_clear(self):
        """Remove the taskbar progress bar (run finished or idle)."""
        tb = self._taskbar()
        if tb is not None:
            tb.clear()

    def mqtt_publish(self, values):
        """Publish a {topic: payload} mapping if the client is running (no-op
        otherwise). Called from the tool tabs as task state changes. Uses getattr
        because the tabs are constructed before self.mqtt is assigned."""
        client = getattr(self, "mqtt", None)
        if client is not None:
            client.publish_many(values)

    # ── System telemetry (Feature #3a) ───────────────────────────────────────

    IDLE_TELEMETRY_MS = 60000   # idle sampling cadence (no task running)

    def _any_task_running(self):
        return any(t.running for t in
                   (self.upscale_tab, self.tag_tab, self.conciliate_tab,
                    self.video_tab))

    def _idle_telemetry_tick(self):
        """Sample while idle so the readout stays live between runs. Skips when a
        task is running — that path samples on its own (faster) cadence."""
        if not self._any_task_running():
            self.sample_telemetry()
        self._idle_telemetry_job = self.after(self.IDLE_TELEMETRY_MS,
                                              self._idle_telemetry_tick)

    def sample_telemetry(self):
        """Take one telemetry sample off the UI thread, then push the result to
        the in-app rows and MQTT. Skips if a previous sample is still in flight
        (nvidia-smi can take a moment) so overlapping ticks never pile up."""
        if not self._telemetry_lock.acquire(blocking=False):
            return
        threading.Thread(target=self._telemetry_worker, daemon=True).start()

    def _telemetry_worker(self):
        try:
            cpu = self._cpu_sampler.sample()
            ram = system_telemetry.sample_ram()
            gpu = system_telemetry.sample_gpu()
        finally:
            self._telemetry_lock.release()
        sample = {"cpu": cpu}
        if ram is not None:
            sample.update(ram_used_mb=ram[0], ram_total_mb=ram[1])
        else:
            sample.update(ram_used_mb=None, ram_total_mb=None)
        # sample_gpu returns a dict of the widened GPU fields (any value may be
        # None where the card doesn't report it); keep every key present so the
        # sample shape is stable whether or not a GPU is found.
        gpu_keys = ("gpu_used_mb", "gpu_total_mb", "gpu_temp_c", "gpu_util_pct",
                    "gpu_power_w", "gpu_power_limit_w", "gpu_clock_mhz")
        if gpu is not None:
            for k in gpu_keys:
                sample[k] = gpu.get(k)
        else:
            for k in gpu_keys:
                sample[k] = None
        try:
            self.after(0, lambda: self._apply_telemetry(sample))
        except Exception:
            pass

    def _apply_telemetry(self, sample):
        """On the UI thread: render the sample in every readout row and mirror
        it to the MQTT system topics."""
        for row in self.telemetry_rows:
            try:
                if getattr(row, "_on_click", None) is None:
                    row.set_on_click(lambda: self.open_telemetry_graph("local"))
                row.show(sample)
            except Exception:
                pass
        # Feed the local run history (no-op unless a local run is live, so idle
        # sampling never enters a graph).
        self.telemetry_history_append("local", sample)
        values = {}
        if sample.get("cpu") is not None:
            values[mqtt_publisher.SYS_CPU_TOPIC] = f"{sample['cpu']:.0f}"
        if sample.get("ram_used_mb") is not None:
            values[mqtt_publisher.SYS_RAM_TOPIC]       = str(sample["ram_used_mb"])
            values[mqtt_publisher.SYS_RAM_TOTAL_TOPIC] = str(sample["ram_total_mb"])
        if sample.get("gpu_used_mb") is not None:
            values[mqtt_publisher.SYS_GPU_VRAM_TOPIC]       = str(sample["gpu_used_mb"])
            values[mqtt_publisher.SYS_GPU_VRAM_TOTAL_TOPIC] = str(sample["gpu_total_mb"])
        if sample.get("gpu_temp_c") is not None:
            values[mqtt_publisher.SYS_GPU_TEMP_TOPIC] = str(sample["gpu_temp_c"])
        if sample.get("gpu_util_pct") is not None:
            values[mqtt_publisher.SYS_GPU_UTIL_TOPIC] = str(sample["gpu_util_pct"])
        if sample.get("gpu_power_w") is not None:
            values[mqtt_publisher.SYS_GPU_POWER_TOPIC] = str(sample["gpu_power_w"])
        if sample.get("gpu_power_limit_w") is not None:
            values[mqtt_publisher.SYS_GPU_POWER_LIMIT_TOPIC] = str(sample["gpu_power_limit_w"])
        if sample.get("gpu_clock_mhz") is not None:
            values[mqtt_publisher.SYS_GPU_CLOCK_TOPIC] = str(sample["gpu_clock_mhz"])
        if values:
            self.mqtt_publish(values)

    def apply_remote_telemetry(self, tab, sample):
        """Render a remote-pod telemetry sample (remote #1, Feature #4) in the
        tab's dedicated remote row — revealing it on the first sample — and
        mirror it to the MQTT system/remote/* topics."""
        row = getattr(tab, "remote_telemetry_row", None)
        src = f"remote:{id(tab)}"
        if row is not None:
            try:
                if getattr(row, "_on_click", None) is None:
                    row.set_on_click(lambda s=src: self.open_telemetry_graph(s))
                if not row.winfo_manager():     # hidden via grid_remove → reveal
                    row.grid()
                row.show(sample)
            except Exception:
                pass
        # Start this pod's run history on its first sample; append thereafter.
        h = self._tel_history.get(src)
        if h is None or h.is_sealed:
            title = f"Remote pod — {getattr(tab, 'mqtt_task_name', 'run')}"
            self.telemetry_history_start(src, title=title)
        self.telemetry_history_append(src, sample)
        values = {}
        if sample.get("cpu") is not None:
            values[mqtt_publisher.SYS_REMOTE_CPU_TOPIC] = f"{sample['cpu']:.0f}"
        if sample.get("ram_used_mb") is not None:
            values[mqtt_publisher.SYS_REMOTE_RAM_TOPIC]       = str(sample["ram_used_mb"])
            values[mqtt_publisher.SYS_REMOTE_RAM_TOTAL_TOPIC] = str(sample["ram_total_mb"])
        if sample.get("gpu_used_mb") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOPIC]       = str(sample["gpu_used_mb"])
            values[mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOTAL_TOPIC] = str(sample["gpu_total_mb"])
        if sample.get("gpu_temp_c") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_TEMP_TOPIC] = str(sample["gpu_temp_c"])
        if sample.get("gpu_util_pct") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_UTIL_TOPIC] = str(sample["gpu_util_pct"])
        if sample.get("gpu_power_w") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_POWER_TOPIC] = str(sample["gpu_power_w"])
        if sample.get("gpu_power_limit_w") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_POWER_LIMIT_TOPIC] = str(sample["gpu_power_limit_w"])
        if sample.get("gpu_clock_mhz") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_CLOCK_TOPIC] = str(sample["gpu_clock_mhz"])
        if values:
            self.mqtt_publish(values)

    def clear_remote_telemetry(self, tab):
        """Run ended / pod gone: hide the tab's remote-pod telemetry row and zero the
        retained MQTT system/remote/* topics, so a terminated pod doesn't leave stale
        non-zero CPU/RAM/VRAM/temp readings showing in Home Assistant."""
        row = getattr(tab, "remote_telemetry_row", None)
        if row is not None:
            try:
                if row.winfo_manager():
                    row.grid_remove()
            except Exception:
                pass
        # Seal this pod's run history: its graph freezes but stays viewable.
        self.telemetry_history_seal(f"remote:{id(tab)}")
        self.mqtt_publish({
            mqtt_publisher.SYS_REMOTE_CPU_TOPIC:            "0",
            mqtt_publisher.SYS_REMOTE_RAM_TOPIC:            "0",
            mqtt_publisher.SYS_REMOTE_RAM_TOTAL_TOPIC:      "0",
            mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOPIC:       "0",
            mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOTAL_TOPIC: "0",
            mqtt_publisher.SYS_REMOTE_GPU_TEMP_TOPIC:       "0",
            mqtt_publisher.SYS_REMOTE_GPU_UTIL_TOPIC:        "0",
            mqtt_publisher.SYS_REMOTE_GPU_POWER_TOPIC:       "0",
            mqtt_publisher.SYS_REMOTE_GPU_POWER_LIMIT_TOPIC: "0",
            mqtt_publisher.SYS_REMOTE_GPU_CLOCK_TOPIC:       "0",
        })

    # ── telemetry history + graph windows (usage graphs, #9) ─────────────────

    def telemetry_history(self, source):
        """The TelemetryHistory for a source ("local" / "remote:<id>"), or None."""
        return self._tel_history.get(source)

    def telemetry_history_start(self, source, title=None):
        """Open a fresh run buffer for a source (resets any previous run). Called
        at a local run's start (source "local") and on a pod's first sample."""
        import time
        h = self._tel_history.get(source)
        if h is None:
            h = system_telemetry.TelemetryHistory()
            self._tel_history[source] = h
        h.start(time.time())
        if title:
            self._tel_titles[source] = title

    def telemetry_history_seal(self, source):
        """End a source's run: its graph freezes but stays viewable. Safe no-op if
        the source never started (e.g. sealing 'local' after a remote-only run)."""
        h = self._tel_history.get(source)
        if h is not None:
            h.seal()

    def telemetry_history_append(self, source, sample):
        """Record a sample into a source's run buffer (no-op unless it is live)."""
        h = self._tel_history.get(source)
        if h is not None:
            import time
            h.append(time.time(), sample)

    def open_telemetry_graph(self, source):
        """Open (or focus) the graph window for a source. No-op if the source has
        no run this session (design 4.5). matplotlib is imported lazily and
        fail-safe: absent, only the graph is unavailable (the row + MQTT are not)."""
        h = self._tel_history.get(source)
        if h is None or len(h) == 0:
            return
        win = self._tel_windows.get(source)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift(); win.focus_set()
                    return
            except Exception:
                pass
            self._tel_windows.pop(source, None)
        title = self._tel_titles.get(source,
                                     "System telemetry — local"
                                     if source == "local" else "Remote pod")
        # Import AND construct inside the guard: telemetry_graph imports cleanly
        # without matplotlib, but the window needs it (imported in __init__), so a
        # missing-matplotlib install fails here, not at module import.
        try:
            from gui.telemetry_graph import TelemetryGraphWindow
            win = TelemetryGraphWindow(self, self, source, title)
        except Exception as exc:
            from tkinter import messagebox
            messagebox.showinfo(
                APP_TITLE,
                "Telemetry graphs need matplotlib, which isn't installed in this "
                "app's environment.\n\nThe live readout row and Home Assistant "
                "topics work without it.\n\n(" + str(exc) + ")")
            return
        self._tel_windows[source] = win

    def forget_telemetry_graph(self, source):
        """A graph window closed: drop the reference so a reopen makes a new one."""
        self._tel_windows.pop(source, None)

    def show_update_dialog(self, info):
        """Open (or focus) the single update dialog for the given UpdateInfo."""
        if self._update_dialog is not None and self._update_dialog.winfo_exists():
            self._update_dialog.lift()
            self._update_dialog.focus_set()
            return
        self._update_dialog = UpdateDialog(self, info)

    def _on_close(self):
        # Don't let unsaved Settings / RunPod edits vanish on exit.
        for tab, name in ((self.settings_tab, "Settings"), (self.runpod_tab, "RunPod")):
            if tab.is_dirty() and self._confirm_unsaved("closing", tab, name) == "cancel":
                return
        # Time each teardown step. If the close is slow (it once flagged "Not
        # Responding"), the per-step breakdown is written to logs/close_timing.log
        # so the real culprit is recorded instead of inferred.
        t0 = time.perf_counter()
        marks = []

        def mark(label):
            marks.append((label, time.perf_counter() - t0))

        busy = [t for t in (self.upscale_tab, self.video_tab, self.tag_tab,
                            self.conciliate_tab) if t.running]
        if busy:
            if not messagebox.askyesno(
                    APP_TITLE, "A task is still running.\nStop it and close the app?"):
                return
            for t in busy:
                t.send("q")
            # Brief grace period — a kill mid-EXIF-write could damage a photo.
            for t in busy:
                if t.proc is not None:
                    try:
                        t.proc.wait(timeout=5)
                    except Exception:
                        t.terminate()
            mark("stop-tasks")
        # Persist the main window layout and the shared log window's layout.
        self._save_geometry()
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.save_geometry()
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.save_geometry()
        if self.video_comparison_window is not None and self.video_comparison_window.winfo_exists():
            self.video_comparison_window.save_geometry()
        seg = getattr(self.video_tab, "_segments_win", None)
        if seg is not None and seg.winfo_exists():
            seg.save_geometry()
        if self.video_playback_window is not None and self.video_playback_window.winfo_exists():
            self.video_playback_window.save_geometry()
            # Release the libVLC players before root.destroy() cascades into their
            # surfaces (else the vout thread faults on the destroyed HWND at exit).
            try:
                self.video_playback_window.teardown_players()
            except Exception:
                pass
        for _tel_win in list(self._tel_windows.values()):
            try:
                if _tel_win.winfo_exists():
                    _tel_win.save_geometry()
            except Exception:
                pass
        mark("save-geometry")
        # Record last-used time and announce going offline before we exit.
        self.stop_mqtt(last_used=datetime.datetime.now().isoformat(timespec="seconds"))
        mark("stop-mqtt")
        self._log_close_timing(marks)
        self.destroy()

    def _log_close_timing(self, marks, slow_threshold=1.5):
        """Append the close-path step timings to logs/close_timing.log, but only
        when the close was slow — so a clean exit stays silent and a stall is
        captured with its real breakdown. Best-effort, never raises."""
        try:
            total = marks[-1][1] if marks else 0.0
            if total < slow_threshold:
                return
            log_dir = os.path.join(APP_ROOT, "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            steps = "  ".join(f"{label}={secs:.2f}s" for label, secs in marks)
            with open(os.path.join(log_dir, "close_timing.log"), "a", encoding="utf-8") as f:
                f.write(f"{stamp}  total={total:.2f}s  {steps}\n")
        except Exception:                       # noqa: BLE001 (diagnostics must never block exit)
            pass


def main():
    # Refuse to start a second copy — two instances share the SQLite cache and the
    # resume/log folders (double file access, DB contention). Done first, before
    # anything touches config/DB. The existing window is brought to the front.
    if single_instance and not single_instance.acquire(
            window_title=f"{APP_TITLE} {APP_VERSION}", app_title=APP_TITLE):
        sys.exit(0)
    # Crisp text on high-DPI displays
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # Give the process its own taskbar identity (AppUserModelID) so the taskbar
    # button shows app.ico and groups under Image Toolbox instead of inheriting
    # pythonw.exe's icon/grouping. Must be set before the first window appears.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "war4peace.ImageToolbox")
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
