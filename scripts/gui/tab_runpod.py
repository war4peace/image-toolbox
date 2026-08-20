"""
gui/tab_runpod.py
-----------------
The Remote (RunPod) settings tab.
"""

import os
import re
import json
import time
import queue
import codecs
import threading
import subprocess
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import runpod_client
import ssh_setup
# runpod_provision is deliberately NOT imported: this tab RUNS it, as a subprocess
# (_stream_provision), and importing it would only add a module-level sys.path
# insert at GUI startup plus one more file whose absence could stop this tab from
# building. LogPane likewise: _stream_provision hand-rolls its Text widget because
# a model download's carriage-return progress needs terminal emulation LogPane has
# no reason to have.
from gui.common import SCRIPT_DIR, APP_ROOT, APP_TITLE, CREATE_NO_WINDOW, CFG, save_config, PYTHON_EXE
from gui.widgets import Tooltip, _ScrollFrame, use_window_button_style


def _fmt_spend(spend):
    """The recent-spend label, or "" when there is nothing to show (no key, an
    unreachable API, or the v1 escape hatch, which has no billing route). Pure,
    so the wording is tested without a window."""
    if not spend or not spend.get("days"):
        return ""
    return (f"spent in the last {spend['days']} days: ${spend['total']:.2f} "
            f"(${spend['gpu']:.2f} pods, ${spend['storage']:.2f} storage)")


class RunPodTab(ttk.Frame):
    """Remote-pod (RunPod) settings, split out of SettingsTab (0.3.7) into its own
    tab because the section grew large and complex. Self-contained: it owns its
    save bar, unsaved-changes detection and revert, all scoped to the `runpod`
    block of config.json only (SettingsTab writes every other section)."""

    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self._all_volumes = None
        self._pods_data = []      # last-fetched pod dicts (for the pods list)
        self._pod_rows = {}       # tree row id -> {"id", "active"}
        self._build()

    def _section(self, parent, title):
        lf = ttk.LabelFrame(parent, text=f"  {title}  ", padding=(10, 8))
        lf.pack(fill="x", padx=10, pady=(10, 0))
        return lf

    def _build(self):
        sf = _ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        body = sf.body

        # ── Remote upscaling (RunPod) ───────────────────────────────────────────
        # No LabelFrame: this is now its own tab, so the content sits directly on
        # the page (a plain padded frame, packed so it coexists with the save bar).
        rp = CFG.get("runpod", {})
        sec = ttk.Frame(body, padding=(10, 8))
        sec.pack(fill="x", padx=10, pady=(10, 0))

        desc = ttk.Frame(sec)
        desc.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        ttk.Label(desc, wraplength=560, foreground="#666",
                  text=("Process images on a rented remote pod (RunPod.io). Set "
                        "'Run on' to 'Remote: RunPod' in the appropriate tab. The API "
                        "key authenticates the pod control plane; the auto-stop / "
                        "runtime limits below are the safety net that keeps a "
                        "billed pod from being left running.")
                  ).pack(anchor="w")
        key_link = tk.Label(desc, text="Get a RunPod API key →", fg="#3a86ff",
                            cursor="hand2", font=("Segoe UI", 9, "underline"))
        key_link.pack(anchor="w", pady=(2, 0))
        key_link.bind("<Button-1>",
                      lambda _e: webbrowser.open(runpod_client.CONSOLE_API_KEYS_URL))
        key_link.bind("<Enter>", lambda _e: key_link.configure(fg="#1a5fd0"))
        key_link.bind("<Leave>", lambda _e: key_link.configure(fg="#3a86ff"))
        Tooltip(key_link,
                "Opens the RunPod console (Settings → API Keys → Create API Key). "
                f"Docs: {runpod_client.DOCS_API_KEYS_URL}")

        # First row, all inline: API key + Test + Set up SSH key + SSH-ready status.
        keyrow = ttk.Frame(sec)
        keyrow.grid(row=1, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(keyrow, text="API key:").pack(side="left")
        self.runpod_key_var = tk.StringVar(value=rp.get("api_key", ""))
        key_entry = ttk.Entry(keyrow, textvariable=self.runpod_key_var, show="•", width=44)
        key_entry.pack(side="left", padx=(4, 0))
        Tooltip(key_entry, "RunPod API key (rest.runpod.io). Stored locally in "
                           "config.json; never committed.")
        W = Tooltip.WRAP_NARROW
        self.runpod_test_btn = ttk.Button(keyrow, text="Test", command=self._test_runpod)
        Tooltip(self.runpod_test_btn,
                "Check that this key works by contacting RunPod. Nothing is rented "
                "and nothing is charged; it only confirms the key is valid.",
                wraplength=W)
        self.runpod_test_btn.pack(side="left", padx=(6, 0))
        # Zero-config SSH: the app owns a dedicated key and hands its public half to
        # every pod via PUBLIC_KEY, so the user never runs ssh-keygen or pastes a key
        # into the RunPod website. A run also auto-ensures it; this button is a
        # convenience, not a prerequisite.
        self.runpod_ssh_btn = ttk.Button(keyrow, text="Set up SSH key",
                                         command=self._setup_ssh)
        self.runpod_ssh_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.runpod_ssh_btn,
                "Generates the dedicated SSH key the app uses to reach rented "
                "pods (one-time). Its public half is sent to each pod "
                "automatically — you never paste a key into the RunPod website.")
        self.runpod_ssh_status = ttk.Label(keyrow, text="", foreground="#666")
        self.runpod_ssh_status.pack(side="left", padx=(8, 0))
        self._refresh_ssh_status()

        # Region + data center (FIRST, so the Refresh next to it clearly drives the
        # GPU lists and volume below). A model volume is region-locked and can only
        # live where network storage is supported, so the picker is grouped by region
        # and offers storage-capable data centers only. Pods follow the volume's region.
        dcsel = ttk.Frame(sec)
        dcsel.grid(row=3, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(dcsel, text="Region:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.runpod_region_var = tk.StringVar()
        self.runpod_region_cmb = ttk.Combobox(dcsel, textvariable=self.runpod_region_var,
                                              state="readonly", values=runpod_client.REGIONS,
                                              width=16)
        self.runpod_region_cmb.grid(row=0, column=1, sticky="w")
        self.runpod_region_cmb.bind("<<ComboboxSelected>>", self._on_region_change)
        Tooltip(self.runpod_region_cmb,
                "Pick the part of the world to host your model volume and run pods. "
                "Only regions with a storage-capable data center are listed. Choose "
                "the one nearest you for the best throughput (volumes are region-locked).")
        ttk.Label(dcsel, text="Data center:").grid(row=0, column=2, sticky="w", padx=(18, 4))
        self.runpod_dc_var = tk.StringVar()
        self.runpod_dc_cmb = ttk.Combobox(dcsel, textvariable=self.runpod_dc_var,
                                          state="readonly", values=[], width=26)
        self.runpod_dc_cmb.grid(row=0, column=3, sticky="w")
        self.runpod_dc_cmb.bind("<<ComboboxSelected>>", self._on_dc_change)
        Tooltip(self.runpod_dc_cmb,
                "Only data centers that support network volumes are listed. The "
                "GPU lists and model volume below apply to this data center. Refresh "
                "to pull the live list (data centers, GPUs and volumes) from RunPod.")
        dc_refresh = ttk.Button(dcsel, text="Refresh", command=self._refresh_datacenters)
        Tooltip(dc_refresh,
                "Ask RunPod for the current list of regions and data centers, and "
                "reload the GPUs and volumes that go with them.", wraplength=W)
        dc_refresh.grid(row=0, column=4, sticky="w", padx=(8, 0))
        # The account balance is shown in the shared bottom-bar "Funds" readout
        # (always visible on this tab), so it isn't duplicated here; Refresh still
        # updates it (see _refresh_datacenters).

        # Upscale GPU, Tag GPU and Model volume share ONE grid (column 0 = labels,
        # column 1 = comboboxes) so the three comboboxes line up under each other,
        # directly below the Region/Data center row whose Refresh drives them. The GPU
        # combos start as the curated name lists; Refresh repopulates them with the
        # GPUs offered in the selected DC plus live price (see _populate_settings_gpus).
        # `_gpu_id_by_label` maps the shown label back to the gpuTypeId (identity for
        # the curated names, label->id for live entries) so resolution works either way.
        gv = ttk.Frame(sec)
        gv.grid(row=4, column=0, columnspan=4, sticky="w", pady=3)
        _CMB_W = 50      # shared width so all three comboboxes align

        ttk.Label(gv, text="Upscale GPU:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 2))
        self.runpod_gpu_var = tk.StringVar(
            value=rp.get("gpu_type_id", runpod_client.GPU_TYPES[0]))
        self._gpu_id_by_label = {name: name for name in runpod_client.GPU_TYPES}
        self.runpod_gpu_cmb = ttk.Combobox(gv, textvariable=self.runpod_gpu_var, state="readonly",
                                           values=runpod_client.GPU_TYPES, width=_CMB_W)
        self.runpod_gpu_cmb.grid(row=0, column=1, sticky="w", pady=(0, 2))
        Tooltip(self.runpod_gpu_cmb,
                "RunPod GPU for upscaling (the heavy SeedVR2 work). The persisted "
                "preference; the Refresh above fills this with the GPUs offered in the "
                "selected data center and their live price. Each tab's live picker "
                "still overrides it per run.")

        # Tag & Rename GPU. The vision model needs only ~6.6 GB, so a cheap 16-20 GB
        # card is ideal; the chosen card is tried first, then the rest as a fallback.
        ttk.Label(gv, text="Tag GPU:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(0, 2))
        self._tag_gpu_label_by_id = {gid: lbl for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self._tag_gpu_id_by_label = {lbl: gid for lbl, gid in runpod_client.TAG_GPU_TYPES}
        cur_tg = rp.get("tag_gpu_type_id", runpod_client.TAG_GPU_TYPES[0][1])
        self.runpod_tag_gpu_var = tk.StringVar(
            value=self._tag_gpu_label_by_id.get(cur_tg, runpod_client.TAG_GPU_TYPES[0][0]))
        self.runpod_tag_gpu_cmb = ttk.Combobox(gv, textvariable=self.runpod_tag_gpu_var,
                                               state="readonly",
                                               values=[lbl for lbl, _ in runpod_client.TAG_GPU_TYPES],
                                               width=_CMB_W)
        self.runpod_tag_gpu_cmb.grid(row=1, column=1, sticky="w", pady=(0, 2))
        Tooltip(self.runpod_tag_gpu_cmb,
                "GPU for remote Tag & Rename. The vision model needs only ~6.6 GB, so "
                "a cheap card is plenty. The Refresh above fills this with the GPUs "
                "offered in the selected data center and their live price.")

        # Model volume (the persistent model store). Saved WITH its full display label
        # (network_volume_label) so it reads in full on restart, not just the bare id;
        # the bare id (network_volume_id) is what the run/provision code consumes.
        ttk.Label(gv, text="Model volume:").grid(row=2, column=0, sticky="w", padx=(0, 6))
        saved_vid = rp.get("network_volume_id", "")
        saved_vlabel = rp.get("network_volume_label", "")
        vol_initial = (saved_vlabel
                       if saved_vlabel and saved_vlabel.split("|", 1)[0].strip() == saved_vid
                       else saved_vid)
        self.runpod_vol_var = tk.StringVar(value=vol_initial)
        self.runpod_vol_cmb = ttk.Combobox(gv, textvariable=self.runpod_vol_var,
                                           state="readonly", width=_CMB_W)
        self.runpod_vol_cmb.grid(row=2, column=1, sticky="w")
        self.runpod_vol_cmb.bind("<<ComboboxSelected>>", self._on_volume_selected)
        Tooltip(self.runpod_vol_cmb,
                "Persistent RunPod network volume that holds the models (SeedVR2 + "
                "Ollama) so disposable pods don't re-download them. Format: "
                "'id | name | size | dc'. The list shows ALL volumes on your "
                "account; the one in the selected data center (or 'None | <data "
                "center>') is pre-selected. Picking a volume from another region "
                "switches the Region / Data center / GPU pickers to match it. "
                "Refresh lists them; Create makes one.")
        # The four volume action buttons on their own row, aligned under the combo.
        volbtns = ttk.Frame(gv)
        volbtns.grid(row=3, column=1, sticky="w", pady=(4, 0))
        vol_refresh = ttk.Button(volbtns, text="Refresh", command=self._refresh_volumes)
        vol_refresh.pack(side="left")
        create_btn = ttk.Button(volbtns, text="Create…", command=self._create_volume)
        create_btn.pack(side="left", padx=(6, 0))
        Tooltip(vol_refresh,
                "List the model volumes on your RunPod account again, in case one "
                "was added or removed elsewhere.", wraplength=W)
        Tooltip(create_btn,
                "Make a new model volume in the data center shown below. A volume "
                "is storage you keep, so it adds a small monthly charge even when "
                "nothing is running; you are asked to confirm first.", wraplength=W)
        del_btn = tk.Button(volbtns, text="Delete…", fg="#b3261e", activeforeground="#b3261e",
                            cursor="hand2", command=self._delete_volume)
        del_btn.pack(side="left", padx=(6, 0))
        Tooltip(del_btn, "Permanently delete the selected network volume AND all "
                         "models stored on it. Asks for confirmation first.")
        prov_btn = ttk.Button(volbtns, text="Provision…", command=self._provision_models)
        prov_btn.pack(side="left", padx=(6, 0))
        use_window_button_style(prov_btn)             # opens its own progress window
        Tooltip(prov_btn, "One-time: fill the selected volume with the models "
                          "(SeedVR2 + Ollama) by briefly renting a pod. ~10-20 min; "
                          "the pod is terminated automatically when finished.")

        # Where the volume actions act (rendered by the seed below via _update_dc_target).
        self.runpod_dc_target = ttk.Label(sec, text="", foreground="#444")
        self.runpod_dc_target.grid(row=5, column=0, columnspan=4, sticky="w", padx=2, pady=(2, 0))

        # Picker state: the last-fetched volumes (None until a Refresh, so the
        # filter leaves the saved id alone on first open). Regions/DCs without
        # network-volume storage are simply never populated, so there's no
        # special-case to carry (a compute-only DC like OC-AU-1 just doesn't appear).
        self._all_volumes = None

        # Seed the picker from the curated list, then point it at the saved DC.
        dc_ids = rp.get("data_center_ids") or []
        cur_dc = dc_ids[0] if dc_ids else "EU-RO-1"
        self._set_dc_entries(
            [{"id": dcid, "label": lbl, "region": runpod_client.region_of(dcid)}
             for lbl, dcid in runpod_client.DATACENTERS],
            preserve_id=cur_dc)

        safety = ttk.Frame(sec)
        safety.grid(row=7, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(safety, text="Auto-stop after:").pack(side="left", padx=(0, 4))
        self.runpod_maxrun_var = tk.StringVar(value=str(rp.get("max_runtime_minutes", 0)))
        maxrun_spin = ttk.Spinbox(safety, from_=0, to=10080, increment=30, width=7,
                                  textvariable=self.runpod_maxrun_var)
        maxrun_spin.pack(side="left")
        ttk.Label(safety, text="min max runtime,").pack(side="left", padx=(4, 12))
        Tooltip(maxrun_spin, "Hard ceiling enforced on the pod itself: it stops "
                             "after this long no matter what. Defaults to 0 (no "
                             "limit) so a long batch of many images is never cut off "
                             "mid-run — the idle timeout below is the dead-man's "
                             "switch that still ends a billed pod if the connection "
                             "drops. Set a value only if you want a hard cap.")
        self.runpod_idle_var = tk.StringVar(value=str(rp.get("idle_timeout_minutes", 15)))
        idle_spin = ttk.Spinbox(safety, from_=0, to=1440, increment=5, width=6,
                                textvariable=self.runpod_idle_var)
        idle_spin.pack(side="left")
        ttk.Label(safety, text="min idle timeout").pack(side="left", padx=(4, 12))
        Tooltip(idle_spin, "Stop the pod after this many minutes with no work "
                           "(0 = no idle limit).")

        ttk.Label(safety, text="·  Provision:").pack(side="left", padx=(0, 4))
        self.runpod_provrun_var = tk.StringVar(value=str(rp.get("provision_max_runtime_minutes", 60)))
        provrun_spin = ttk.Spinbox(safety, from_=30, to=240, increment=15, width=5,
                                   textvariable=self.runpod_provrun_var)
        provrun_spin.pack(side="left")
        ttk.Label(safety, text="min ceiling").pack(side="left", padx=(4, 0))
        Tooltip(provrun_spin, "Hard ceiling for the temporary model-provisioning "
                              "pod's dead-man's switch: it self-terminates after "
                              "this long even if the app is closed or the "
                              "provisioning window is force-killed, so a stuck "
                              "download can't leave a pod billing. The download "
                              "normally takes 10-20 min; 60 leaves headroom. (Idle "
                              "timeout doesn't apply here — no heartbeat is written "
                              "during a download.)")

        # ── Money safety-net (funds_guard, roadmap #1) ──────────────────────────
        money = ttk.Frame(sec)
        money.grid(row=8, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(money, text="Money safety:  stop the run at $").pack(side="left", padx=(0, 4))
        self.runpod_cap_var = tk.StringVar(value=str(rp.get("session_cost_cap_usd", 0)))
        cap_spin = ttk.Spinbox(money, from_=0, to=10000, increment=5, width=7,
                               textvariable=self.runpod_cap_var)
        cap_spin.pack(side="left")
        ttk.Label(money, text="/run cost,").pack(side="left", padx=(4, 12))
        Tooltip(cap_spin, "Auto-stop the pod once THIS run's accrued cost (its real "
                          "billed $/h times how long it has run) reaches this many "
                          "dollars. 0 = no cap. The resume cache is saved, so you "
                          "continue later. A money backstop alongside the time/idle "
                          "dead-man's switch.")
        ttk.Label(money, text="or below $").pack(side="left", padx=(0, 4))
        self.runpod_floor_var = tk.StringVar(value=str(rp.get("balance_floor_usd", 0)))
        floor_spin = ttk.Spinbox(money, from_=0, to=100000, increment=5, width=7,
                                 textvariable=self.runpod_floor_var)
        floor_spin.pack(side="left")
        ttk.Label(money, text="balance").pack(side="left", padx=(4, 0))
        Tooltip(floor_spin, "Keep at least this much in your RunPod account: the app "
                            "refuses to START a run that would drop below it, and "
                            "auto-stops a running pod if the live balance falls to "
                            "it. 0 = no floor. Needs the RunPod API key set. "
                            "Unreadable balance never blocks a run (fail-safe).")

        # Recent SPEND, beside the two money limits (#25 P4). It is not a balance
        # and cannot become one, but it is the figure that says where the money
        # actually went — and a network volume bills around the clock whether or
        # not anything is running, which is invisible in every other readout.
        self.runpod_spend_var = tk.StringVar(value="")
        spend_lbl = ttk.Label(money, textvariable=self.runpod_spend_var,
                              foreground="#666")
        spend_lbl.pack(side="left", padx=(18, 0))
        Tooltip(spend_lbl,
                "What this RunPod account has been charged over the last 30 days, "
                "split into pod GPU time and network-volume storage. Filled in by "
                "the Refresh button above. Storage is billed continuously for as "
                "long as the model volume exists, even with no pod running.",
                wraplength=W)

        self.runpod_terminate_var = tk.BooleanVar(value=bool(rp.get("terminate_when_done", True)))
        term_chk = ttk.Checkbutton(
            sec, text="Terminate (delete) the pod when done, not just stop it",
            variable=self.runpod_terminate_var)
        term_chk.grid(row=9, column=0, columnspan=4, sticky="w", pady=3)
        Tooltip(term_chk, "ON (recommended): the disposable pod is deleted when a "
                          "run ends, freeing ALL billing. This NEVER deletes your "
                          "model network volume — that's a separate resource. OFF "
                          "only stops the pod (it lingers as EXITED and keeps "
                          "billing for its disk); the app never reuses a stopped "
                          "pod, so OFF just leaves billing cruft.")

        self.runpod_status = ttk.Label(sec, text="", foreground="#666")
        self.runpod_status.grid(row=10, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # ── Your pods ───────────────────────────────────────────────────────────
        # List every pod on the account (running or exited) with a Terminate
        # control, so the user can clean up billing without visiting the RunPod
        # website. A pod a live remote run depends on is marked '(in use)' and
        # can't be terminated here.
        pods = ttk.Frame(body, padding=(10, 8))
        pods.pack(fill="x", padx=10, pady=(10, 0))
        hdr = ttk.Frame(pods)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Your pods", font=("Segoe UI", 9, "bold")).pack(side="left")
        pods_refresh = ttk.Button(hdr, text="Refresh", command=self._refresh_pods)
        pods_refresh.pack(side="left", padx=(10, 0))
        Tooltip(pods_refresh,
                "List every pod on your account, running or stopped, with what it "
                "costs per hour. Worth a look if you want to be sure nothing is "
                "still billing.", wraplength=W)
        self.runpod_pods_term_btn = tk.Button(
            hdr, text="Terminate selected…", fg="#b3261e", activeforeground="#b3261e",
            cursor="hand2", state="disabled", command=self._terminate_pod)
        self.runpod_pods_term_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.runpod_pods_term_btn,
                "Permanently delete the selected pod, freeing all its billing. This "
                "never touches your model network volume. A pod a remote run is "
                "using right now is marked '(in use)' and can't be terminated here "
                "— stop that run first.")

        tree = ttk.Treeview(pods,
                            columns=("name", "status", "gpu", "region", "dc", "cost"),
                            show="headings", height=5, selectmode="browse")
        for col, txt, w, anchor in (("name", "Name / id", 170, "w"),
                                    ("status", "Status", 85, "w"),
                                    ("gpu", "GPU", 150, "w"),
                                    ("region", "Region", 105, "w"),
                                    ("dc", "Data center", 95, "w"),
                                    ("cost", "$/hr", 55, "e")):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor=anchor, stretch=(col == "gpu"))
        tree.pack(fill="x", pady=(6, 0))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_terminate_state())
        self.runpod_pods_tree = tree
        self.runpod_pods_status = ttk.Label(pods, text="", foreground="#666")
        self.runpod_pods_status.pack(anchor="w", pady=(4, 0))

        # ── Save bar ────────────────────────────────────────────
        bar = ttk.Frame(body, padding=(8, 12))
        bar.pack(fill="x")
        rp_save = ttk.Button(bar, text="Save settings", command=self._save)
        rp_save.pack(side="left")
        Tooltip(rp_save,
                "Write the RunPod settings on this page to disk. Runs read the "
                "saved values, so an unsaved key or volume is not used yet.",
                wraplength=W)
        self.save_status = ttk.Label(bar, text="", foreground="#666")
        self.save_status.pack(side="left", padx=12)

        # Baseline + live "Not saved" indicator, same pattern as SettingsTab.
        self._baseline = self._snapshot()
        self._save_status_base = ""
        self._save_status_hold = 0.0
        self._refresh_save_indicator()

    # ── save / unsaved-changes machinery (runpod-scoped) ───────────────

    def _collect(self):
        """The runpod section the form currently describes, as ({"runpod": {...}},
        errors). Kept in SettingsTab's (sections, errors) shape so the save/dirty
        helpers below mirror it exactly."""
        return {"runpod": self._runpod_fields()}, []

    def _snapshot(self):
        sections, errors = self._collect()
        return json.dumps(sections, sort_keys=True), bool(errors)

    def is_dirty(self):
        try:
            return self._snapshot() != self._baseline
        except Exception:
            return False

    def _save(self):
        sections, errors = self._collect()
        if errors:
            messagebox.showwarning(
                APP_TITLE, "These fields need a whole number:\n  • " + "\n  • ".join(errors))
            return False
        for name, values in sections.items():
            target = CFG.setdefault(name, {})
            if name == "runpod":
                target.pop("max_price_per_hour", None)          # 0.3.4: split per task
                target.pop("max_price_per_hour_upscale", None)  # 0.4.0: no auto-fallback
                target.pop("max_price_per_hour_tag", None)      # GPU is never substituted
            target.update(values)
        if save_config():
            self._baseline = self._snapshot()
            self._save_status_base = "Saved."
            self.save_status.configure(text="Saved.", foreground="#1a7f37")
            return True
        self._save_status_hold = time.time() + 6
        self.save_status.configure(
            text="Could not write config.json (check file permissions).",
            foreground="#b3261e")
        return False

    def _refresh_save_indicator(self):
        """Light timer mirroring SettingsTab's: 'Not saved' (red) when the form
        differs from the saved state, 'Saved.' (green) right after a save."""
        try:
            if not self.save_status.winfo_exists():
                return
            if time.time() >= self._save_status_hold:
                if self.is_dirty():
                    self.save_status.configure(text="Not saved", foreground="#b3261e")
                else:
                    base = self._save_status_base
                    self.save_status.configure(
                        text=base, foreground="#1a7f37" if base == "Saved." else "#666")
        except Exception:                       # noqa: BLE001
            pass
        self.after(400, self._refresh_save_indicator)

    def revert(self):
        """Discard unsaved edits: reset every runpod field to the values in CFG."""
        rp = CFG.get("runpod", {})
        self.runpod_key_var.set(rp.get("api_key", ""))
        self.runpod_maxrun_var.set(str(rp.get("max_runtime_minutes", 0)))
        self.runpod_idle_var.set(str(rp.get("idle_timeout_minutes", 15)))
        self.runpod_provrun_var.set(str(rp.get("provision_max_runtime_minutes", 60)))
        self.runpod_cap_var.set(str(rp.get("session_cost_cap_usd", 0)))
        self.runpod_floor_var.set(str(rp.get("balance_floor_usd", 0)))
        self.runpod_terminate_var.set(bool(rp.get("terminate_when_done", True)))
        # Reset the GPU combos to the curated lists (discard any live-refresh state).
        self.runpod_gpu_cmb.configure(values=runpod_client.GPU_TYPES)
        self._gpu_id_by_label = {name: name for name in runpod_client.GPU_TYPES}
        self.runpod_gpu_var.set(rp.get("gpu_type_id", runpod_client.GPU_TYPES[0]))
        self._tag_gpu_label_by_id = {gid: lbl for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self._tag_gpu_id_by_label = {lbl: gid for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self.runpod_tag_gpu_cmb.configure(values=[lbl for lbl, _ in runpod_client.TAG_GPU_TYPES])
        self.runpod_tag_gpu_var.set(self._tag_gpu_label_by_id.get(
            rp.get("tag_gpu_type_id", runpod_client.TAG_GPU_TYPES[0][1]),
            runpod_client.TAG_GPU_TYPES[0][0]))
        dc_ids = rp.get("data_center_ids") or []
        cur_dc = dc_ids[0] if dc_ids else "EU-RO-1"
        self._sync_region_dc_to(cur_dc)
        saved_vid = rp.get("network_volume_id", "")
        saved_vlabel = rp.get("network_volume_label", "")
        self.runpod_vol_var.set(
            saved_vlabel if saved_vlabel and saved_vlabel.split("|", 1)[0].strip() == saved_vid
            else saved_vid)
        self._baseline = self._snapshot()
        self._save_status_base = ""

    def _runpod_fields(self):
        """The RunPod settings currently in the form (so Test works pre-Save).
        Numeric fields fall back to their defaults rather than erroring — the
        save path re-validates and reports."""
        def _num(var, default, cast):
            try:
                return cast(str(var.get()).strip())
            except (ValueError, tk.TclError):
                return default
        rp = CFG.get("runpod", {})
        dc_id = self._selected_dc_id()
        return {
            "api_key":              self.runpod_key_var.get().strip(),
            "max_runtime_minutes":  _num(self.runpod_maxrun_var, 0, int),
            "idle_timeout_minutes": _num(self.runpod_idle_var, 15, int),
            "provision_max_runtime_minutes": _num(self.runpod_provrun_var, 60, int),
            "session_cost_cap_usd": _num(self.runpod_cap_var, 0, float),
            "balance_floor_usd":    _num(self.runpod_floor_var, 0, float),
            "terminate_when_done":  bool(self.runpod_terminate_var.get()),
            "gpu_type_id":      self._gpu_id_by_label.get(
                self.runpod_gpu_var.get(),
                self.runpod_gpu_var.get().strip() or runpod_client.GPU_TYPES[0]),
            "tag_gpu_type_id":  self._tag_gpu_id_by_label.get(
                self.runpod_tag_gpu_var.get(), runpod_client.TAG_GPU_TYPES[0][1]),
            "data_center_ids":  [dc_id] if dc_id else [],
            "network_volume_id": self._selected_volume_id(),
            # The full combobox label ('id | name | size | dc') so it reloads in full
            # next launch instead of just the bare id; blank when no real volume.
            "network_volume_label": (self.runpod_vol_var.get()
                                     if self._selected_volume_id() else ""),
            # Carried through unchanged (no UI) so a save never drops them.
            # hourly_rate has no UI control (live GPU prices + the per-task ceilings
            # replaced it); only the `status` dev CLI still reads it for a cost estimate.
            "hourly_rate":      rp.get("hourly_rate", 0.90),
            "image_name":       rp.get("image_name", ""),
            "template_id":      rp.get("template_id", ""),
            "container_disk_gb": rp.get("container_disk_gb", 30),
            "ssh_key_path":     rp.get("ssh_key_path", ""),
            "worker_port":      rp.get("worker_port", 8200),
            "stop_pod_when_done": rp.get("stop_pod_when_done", True),
        }

    def _selected_volume_id(self):
        """Resolve the network-volume field to a bare id ('' if none). The combobox
        shows 'id | name | size | dc', or a 'None | <data center>' placeholder when
        the selected DC has no volume — both resolve to no id."""
        tok = (self.runpod_vol_var.get().split("|", 1)[0]).strip()
        return "" if tok == "None" else tok

    # ── Region / data-center picker ──────────────────────────────────────────
    def _set_dc_entries(self, entries, preserve_id=None):
        """Adopt a list of data-center entries ({id,label,region}) as the picker's
        source, rebuild the lookup maps, and point the Region/Data center combos at
        `preserve_id` (kept available even if absent from the list)."""
        entries = list(entries)
        if preserve_id and preserve_id not in {e["id"] for e in entries}:
            entries.append({
                "id": preserve_id, "label": preserve_id,
                "region": runpod_client.region_of(preserve_id) or runpod_client.REGIONS[0]})
        self._dc_entries     = entries
        self._dc_label_by_id = {e["id"]: e["label"] for e in entries}
        self._dc_id_by_label = {e["label"]: e["id"] for e in entries}
        # Only offer regions that actually have a storage-capable DC (so e.g.
        # Oceania, with only the compute-only OC-AU-1, simply doesn't appear).
        avail_regions = [r for r in runpod_client.REGIONS
                         if any(e["region"] == r for e in entries)]
        self.runpod_region_cmb.configure(values=avail_regions)
        target = preserve_id or self._selected_dc_id()
        region = runpod_client.region_of(target)
        if region not in avail_regions:
            region = self.runpod_region_var.get() if self.runpod_region_var.get() in avail_regions \
                else (avail_regions[0] if avail_regions else "")
        self.runpod_region_var.set(region)
        self._populate_dc_for_region(select_id=target)

    def _populate_dc_for_region(self, select_id=None):
        """Fill the Data center combo with the DCs in the chosen region, selecting
        `select_id` if it lives there else the first one."""
        region = self.runpod_region_var.get()
        labels = [e["label"] for e in self._dc_entries if e["region"] == region]
        self.runpod_dc_cmb.configure(values=labels)
        if labels:
            lab = self._dc_label_by_id.get(select_id) if select_id else None
            self.runpod_dc_var.set(lab if lab in labels else labels[0])
        else:
            self.runpod_dc_var.set("")
        self._update_dc_target()
        self._apply_volume_filter()

    def _on_region_change(self, *_):
        self._populate_dc_for_region()

    def _on_dc_change(self, *_):
        self._update_dc_target()
        self._apply_volume_filter()

    def _selected_dc_id(self):
        """The data-center id currently chosen in the picker ('' if none)."""
        return self._dc_id_by_label.get(self.runpod_dc_var.get(), "")

    def _update_dc_target(self):
        """Spell out, in plain language, where the volume buttons will act. Regions
        without a storage-capable DC are never offered, so a missing selection just
        means 'nothing picked yet'."""
        dc = self._selected_dc_id()
        region = self.runpod_region_var.get()
        if dc:
            self.runpod_dc_target.configure(
                text=f"Volume actions (Create / Provision) act in:  {region}  ·  {dc}",
                foreground="#444")
        else:
            self.runpod_dc_target.configure(
                text="Pick a region and data center for the model volume.",
                foreground="#666")

    def _sync_region_dc_to(self, dc_id):
        """Point the Region/Data center pickers at `dc_id` (e.g. a selected
        volume's region) so the displayed target matches where actions run. Adds an
        ad-hoc entry if the id isn't already in the list."""
        if not dc_id:
            return
        if dc_id not in self._dc_label_by_id:
            self._dc_entries.append({
                "id": dc_id, "label": dc_id,
                "region": runpod_client.region_of(dc_id) or runpod_client.REGIONS[0]})
            self._dc_label_by_id[dc_id] = dc_id
            self._dc_id_by_label[dc_id] = dc_id
        self.runpod_region_var.set(
            runpod_client.region_of(dc_id) or self.runpod_region_var.get())
        self._populate_dc_for_region(select_id=dc_id)

    def _on_volume_selected(self, *_):
        """When the user picks an existing volume, follow it: a volume is
        region-locked, so the Region/Data center should reflect where it lives, and
        the Upscale/Tag GPU lists should refresh for that data center. Picking a
        volume from another region is thus the reverse of changing the DC by hand."""
        parts = self.runpod_vol_var.get().split("|")
        if len(parts) >= 4:
            dc = parts[-1].strip()
            if dc and dc != "?":
                self._sync_region_dc_to(dc)
                self._refresh_settings_gpus()

    def _refresh_datacenters(self):
        """Pull the live storage-capable data-center list from RunPod (GraphQL) and
        populate the Region/Data center pickers. Also refreshes the model volumes
        and the Upscale/Tag GPU lists for the selected data center."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_status.configure(text="Listing data centers…", foreground="#666")
        keep = self._selected_dc_id()

        def work():
            try:
                dcs = runpod_client.data_centers(key)      # storage-capable only
                err = None
            except runpod_client.RunPodError as exc:
                dcs, err = [], str(exc)
            funds = runpod_client.account_balance_detail(key)   # never raises
            spend = runpod_client.account_spend(key, days=30)   # never raises; None on v1

            def apply():
                self.runpod_spend_var.set(_fmt_spend(spend))
                # Push the fetched balance to the shared bottom-bar Funds readout.
                try:
                    self.app.set_funds_cache(funds)
                except Exception:
                    pass
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                entries = [{
                    "id": d["id"],
                    "label": (f'{d["location"]} ({d["id"]})' if d["location"] else d["id"]),
                    "region": d["region"] or runpod_client.region_of(d["id"]),
                } for d in dcs]
                self._set_dc_entries(entries, preserve_id=keep)
                self.runpod_status.configure(
                    text=f"{len(entries)} storage-capable data center(s) across "
                         f"{len({e['region'] for e in entries})} region(s).",
                    foreground="#1a7f37")
                # Also refresh volumes + GPU lists for the (now-selected) DC.
                self._refresh_volumes()
                self._refresh_settings_gpus()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _fmt_gpu(self, g):
        """Label for a Settings GPU combo entry: name, VRAM, live price, stock."""
        price = f"${g['price']:.2f}/h" if g.get("price") is not None else "n/a"
        tail = "" if g.get("stock") else " | no stock"
        return f"{g.get('name', g.get('id'))} | {g.get('memory_gb', 0)} GB | {price}{tail}"

    def _refresh_settings_gpus(self):
        """Populate the Upscale/Tag GPU comboboxes with the GPUs the selected data
        center offers, each with its live price. Out-of-stock cards are included
        (these are a stored PREFERENCE, not a now-deployable pick) so the defaults
        (RTX 5090 / RTX 2000 Ada) are offered even when momentarily sold out."""
        key = self.runpod_key_var.get().strip()
        if not key:
            return
        dc = self._selected_dc_id() or None

        def work():
            try:
                gpus = runpod_client.available_gpus(key, dc, min_memory_gb=0,
                                                    include_out_of_stock=True)
                err = None
            except runpod_client.RunPodError as exc:
                gpus, err = [], str(exc)

            def apply():
                if err or not gpus:
                    return                  # keep the curated lists on failure
                self._populate_settings_gpus(gpus)
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _populate_settings_gpus(self, gpus):
        """Fill both GPU combos from a live availability list, partitioned by the
        VRAM floor (≥32 GB upscale, ≥16 GB tag), keeping the current pick if it is
        still offered else defaulting to RTX 5090 / RTX 2000 Ada else cheapest."""
        ups = [g for g in gpus if (g.get("memory_gb") or 0) >= 32]
        tag = [g for g in gpus if (g.get("memory_gb") or 0) >= 16]
        self._fill_gpu_combo(self.runpod_gpu_cmb, self.runpod_gpu_var, ups,
                             "_gpu_id_by_label", "NVIDIA GeForce RTX 5090")
        self._fill_gpu_combo(self.runpod_tag_gpu_cmb, self.runpod_tag_gpu_var, tag,
                             "_tag_gpu_id_by_label", "NVIDIA RTX 2000 Ada Generation")

    def _fill_gpu_combo(self, cmb, var, gpus, id_map_attr, default_id):
        """Set a GPU combo's values to live entries and select the current pick (by
        resolved id) if still present, else default_id, else the first (cheapest)."""
        if not gpus:
            return
        labels  = [self._fmt_gpu(g) for g in gpus]
        id_by_label = {lbl: g["id"] for lbl, g in zip(labels, gpus)}
        cur_id = getattr(self, id_map_attr, {}).get(var.get())
        setattr(self, id_map_attr, id_by_label)
        cmb.configure(values=labels)
        want = cur_id if any(g["id"] == cur_id for g in gpus) else default_id
        sel = next((lbl for lbl, g in zip(labels, gpus) if g["id"] == want), labels[0])
        var.set(sel)

    def _refresh_volumes(self, select_id=None):
        """Fetch the account's network volumes (free call), cache them, and show the
        ones in the currently-selected data center. `select_id` pre-selects a volume
        (e.g. one just created)."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_status.configure(text="Listing network volumes…", foreground="#666")

        def work():
            try:
                vols = runpod_client.list_network_volumes(key)
                err = None
            except runpod_client.RunPodError as exc:
                vols, err = [], str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                self._all_volumes = [v for v in vols if isinstance(v, dict)]
                # select_id forces a pick (e.g. a just-created volume); a plain
                # Refresh (None) lets the filter follow the selected data center.
                total, n = self._apply_volume_filter(select_id=select_id)
                dc = self._selected_dc_id() or "the selected data center"
                if total:
                    self.runpod_status.configure(
                        text=(f"{total} volume(s) on your account · {n} in {dc}."
                              if n else
                              f"{total} volume(s) on your account · none in {dc} "
                              "(use Create…)."),
                        foreground="#1a7f37")
                else:
                    self.runpod_status.configure(
                        text="No network volumes on your account yet — use Create…",
                        foreground="#666")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _volume_label(self, v):
        # The data center goes through runpod_client.volume_data_center: v1 spells it
        # `dataCenterId`, v2 `dataCenter`. Reading it raw would print '?' for
        # every volume and, worse, match none of them to the picked DC below.
        return (f"{v.get('id','')} | {v.get('name','?')} | "
                f"{v.get('size','?')} GB | {runpod_client.volume_data_center(v) or '?'}")

    def _apply_volume_filter(self, select_id=None):
        """Populate the Model volume combobox with ALL of the account's volumes (so
        the user can see and pick any volume without hunting region-by-region), and
        select the one in the currently-selected data center — or a 'None | <dc>'
        placeholder when that DC has none. Returns (total, in_dc) counts; (0, 0)
        before the first fetch, so the saved id stays visible on first open."""
        if self._all_volumes is None:
            return (0, 0)
        dc = self._selected_dc_id()
        all_labels = [self._volume_label(v) for v in self._all_volumes]
        in_dc = [v for v in self._all_volumes
                 if runpod_client.volume_data_center(v) == dc] if dc else []
        # A DC with no volume still needs a readable selection: prepend a
        # 'None | <dc>' placeholder (kept first so it's easy to spot in the list).
        values = list(all_labels)
        placeholder = None
        if not in_dc:
            dc_label = self._dc_label_by_id.get(dc, dc) if dc else "(no data center)"
            placeholder = f"None | {dc_label}"
            values = [placeholder] + values
        self.runpod_vol_cmb.configure(values=values)
        # Selection: an explicit target wins; else keep the current pick when it
        # belongs to this DC; else the DC's own volume; else the placeholder. So a
        # DC change follows the DC, while a Refresh keeps a still-valid selection.
        cur = self._selected_volume_id()
        want = select_id or (cur if any(v.get("id") == cur
                                        and runpod_client.volume_data_center(v) == dc
                                        for v in self._all_volumes) else None)
        sel = (next((l for l in all_labels if l.split("|", 1)[0].strip() == want), None)
               if want else None)
        if sel is None and in_dc:
            sel = self._volume_label(in_dc[0])
        if sel is None:
            sel = placeholder or (values[0] if values else "")
        self.runpod_vol_var.set(sel)
        return (len(self._all_volumes), len(in_dc))

    def _create_volume(self):
        """Create a network volume in the selected region's data center (this
        starts a small monthly storage charge — confirmed first)."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        dc_id = self._selected_dc_id()
        if not dc_id:
            self.runpod_status.configure(
                text=f"No storage data center selected for {self.runpod_region_var.get()} "
                     "— pick another region first.", foreground="#b3261e")
            return
        name = simpledialog.askstring(
            "Create network volume",
            "Name for the model volume:", parent=self, initialvalue="image-toolbox-models")
        if not name:
            return
        size = simpledialog.askinteger(
            "Create network volume",
            "Size in GB (SeedVR2 ~16 GB + Ollama runtime & model ~10 GB + video "
            "scratch; 50 leaves headroom, 40 has run 94% full):",
            parent=self, initialvalue=50, minvalue=1, maxvalue=4000)
        if not size:
            return
        est = size * 0.07
        if not messagebox.askyesno(
                APP_TITLE,
                f"Create a {size} GB network volume '{name}' in {dc_id}?\n\n"
                f"This starts a storage charge of about ${est:.2f}/month "
                f"(at $0.07/GB/mo) until you delete it."):
            return
        self.runpod_status.configure(text="Creating network volume…", foreground="#666")

        def work():
            try:
                vol = runpod_client.create_network_volume(key, name, size, dc_id)
                err = None
            except runpod_client.RunPodError as exc:
                vol, err = None, str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                vid = (vol or {}).get("id", "")
                self.runpod_status.configure(
                    text=f"Created volume {vid} in {dc_id}.", foreground="#1a7f37")
                self._refresh_volumes(select_id=vid)
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _delete_volume(self):
        """Permanently delete the selected network volume (and the models on it),
        behind an explicit warning confirmation."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        vid = self._selected_volume_id()
        if not vid:
            self.runpod_status.configure(
                text="Select a volume to delete first (Refresh, then pick one).",
                foreground="#b3261e")
            return
        label = self.runpod_vol_var.get().strip() or vid
        if not messagebox.askyesno(
                APP_TITLE,
                f"Delete this network volume?\n\n  {label}\n\n"
                "This PERMANENTLY destroys the volume and ALL MODELS stored on it "
                "(SeedVR2, Ollama). Any disposable pod will have to re-download "
                "~40 GB the next time you run. This cannot be undone.",
                icon="warning", default="no"):
            return
        self.runpod_status.configure(text="Deleting network volume…", foreground="#666")

        def work():
            try:
                runpod_client.delete_network_volume(key, vid)
                err = None
            except runpod_client.RunPodError as exc:
                err = str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                # Clear the selection if it was the deleted volume, then re-list.
                if self._selected_volume_id() == vid:
                    self.runpod_vol_var.set("")
                self.runpod_status.configure(
                    text=f"Deleted volume {vid}.", foreground="#1a7f37")
                self._refresh_volumes()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    # ── Your pods (list + terminate) ──────────────────────────────────────────
    def _active_pod_ids(self):
        app = getattr(self, "app", None)
        return app.active_remote_pod_ids() if app is not None else set()

    def _refresh_pods(self):
        """Fetch every pod on the account (running or exited) and show it."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_pods_status.configure(text="Enter a RunPod API key first.",
                                              foreground="#b3261e")
            return
        self.runpod_pods_status.configure(text="Listing pods…", foreground="#666")

        def work():
            try:
                pods = runpod_client.list_pods_detailed(key)
                err = None
            except runpod_client.RunPodError as exc:
                pods, err = [], str(exc)

            def apply():
                if err:
                    self.runpod_pods_status.configure(text=err, foreground="#b3261e")
                    return
                self._pods_data = [p for p in pods if isinstance(p, dict)]
                self._render_pods()
                n = len(self._pods_data)
                self.runpod_pods_status.configure(
                    text=(f"{n} pod(s) on your account." if n
                          else "No pods on your account."),
                    foreground="#1a7f37" if n else "#666")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _pod_fields(self, p):
        """Display tuple for one pod from a normalized runpod_client record
        ({id, name, status, gpu, gpu_count, region, data_center, cost})."""
        pid = p.get("id", "")
        name = p.get("name") or pid
        status = p.get("status") or "?"
        gpu = p.get("gpu") or "?"
        cnt = p.get("gpu_count")
        if cnt and gpu != "?":
            gpu = f"{cnt}× {gpu}"
        region = p.get("region") or "?"
        dc = p.get("data_center") or "?"
        cost = p.get("cost")
        cost = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        return pid, name, status, gpu, region, dc, cost

    def _render_pods(self):
        """Rebuild the pods tree from the cached data, marking the live pod(s)."""
        tree = self.runpod_pods_tree
        if not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        self._pod_rows = {}
        active = self._active_pod_ids()
        for p in self._pods_data:
            pid, name, status, gpu, region, dc, cost = self._pod_fields(p)
            is_active = pid in active
            shown = f"{status} · in use" if is_active else status
            row = tree.insert("", "end", values=(name, shown, gpu, region, dc, cost))
            self._pod_rows[row] = {"id": pid, "active": is_active}
        self._refresh_terminate_state()

    def _refresh_terminate_state(self):
        """Enable Terminate only when a selected pod is not in use by a live run."""
        if not self.runpod_pods_term_btn.winfo_exists():
            return
        sel = self.runpod_pods_tree.selection()
        info = self._pod_rows.get(sel[0]) if sel else None
        ok = bool(info) and not info["active"] and info["id"] not in self._active_pod_ids()
        self.runpod_pods_term_btn.configure(state="normal" if ok else "disabled")

    def on_active_pods_changed(self):
        """A remote run started/ended: re-mark the list and the Terminate button."""
        if self._pods_data:
            self._render_pods()
        else:
            self._refresh_terminate_state()

    def _terminate_pod(self):
        sel = self.runpod_pods_tree.selection()
        info = self._pod_rows.get(sel[0]) if sel else None
        if not info:
            return
        pid = info["id"]
        # Re-check liveness at click time (a run may have started since render).
        if info["active"] or pid in self._active_pod_ids():
            self.runpod_pods_status.configure(
                text="That pod is in use by a running remote task — stop it first.",
                foreground="#b3261e")
            self._refresh_terminate_state()
            return
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_pods_status.configure(text="Enter a RunPod API key first.",
                                              foreground="#b3261e")
            return
        vals = self.runpod_pods_tree.item(sel[0], "values")
        label = f"{vals[0]} ({pid})" if vals else pid
        if not messagebox.askyesno(
                APP_TITLE,
                f"Terminate this pod?\n\n  {label}\n\n"
                "This permanently deletes the pod and frees its billing. It does "
                "NOT touch your model network volume. This cannot be undone.",
                icon="warning", default="no"):
            return
        self.runpod_pods_status.configure(text="Terminating pod…", foreground="#666")

        def work():
            try:
                runpod_client.terminate_pod(key, pid)
                err = None
            except runpod_client.RunPodError as exc:
                err = str(exc)

            def apply():
                if err:
                    self.runpod_pods_status.configure(text=err, foreground="#b3261e")
                    return
                self.runpod_pods_status.configure(
                    text=f"Terminated pod {pid}.", foreground="#1a7f37")
                self._refresh_pods()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _test_runpod(self):
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_test_btn.configure(state="disabled")
        self.runpod_status.configure(text="Testing connection…", foreground="#666")

        def work():
            ok, msg = runpod_client.test_connection(key)
            def apply():
                self.runpod_test_btn.configure(state="normal")
                self.runpod_status.configure(
                    text=msg, foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _effective_ssh_key(self):
        """The key path a run will use: the configured one, else the app default."""
        return (os.path.expandvars(CFG.get("runpod", {}).get("ssh_key_path", ""))
                or ssh_setup.default_key_path())

    def _refresh_ssh_status(self):
        """Reflect the current SSH-key state without generating anything."""
        if ssh_setup.read_public_key(self._effective_ssh_key()):
            self.runpod_ssh_status.configure(text="SSH key ready ✓", foreground="#1a7f37")
            return
        ok, _ssh, _kg, _msg = ssh_setup.ssh_available()
        if ok:
            self.runpod_ssh_status.configure(text="No key yet — click to set up.",
                                             foreground="#666")
        else:
            self.runpod_ssh_status.configure(
                text="OpenSSH not found — enable it in Windows Optional features.",
                foreground="#b3261e")

    def _setup_ssh(self):
        """Generate (or locate) the app's dedicated SSH key off the UI thread."""
        self.runpod_ssh_btn.configure(state="disabled")
        self.runpod_ssh_status.configure(text="Setting up SSH…", foreground="#666")
        # Use the configured path if any; ensure_keypair falls back to the default.
        key_path = os.path.expandvars(CFG.get("runpod", {}).get("ssh_key_path", "")) or None

        def work():
            ok, info = ssh_setup.setup(key_path)
            def apply():
                self.runpod_ssh_btn.configure(state="normal")
                self.runpod_ssh_status.configure(
                    text=info.get("message", ""),
                    foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _provision_models(self):
        """One-time model-volume provisioning: launch runpod_provision.py
        setup-volume (create pod → fill the volume → auto-terminate) and stream
        its progress in a window. Reads config.json, so settings must be saved."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        if not self._selected_volume_id():
            self.runpod_status.configure(text="Select or create a model volume first.",
                                         foreground="#b3261e")
            return
        # setup-volume reads config.json, not the live form — persist edits first.
        if self.is_dirty():
            if not messagebox.askokcancel(
                    APP_TITLE, "Provisioning reads your saved settings. Save the "
                               "current changes now and continue?"):
                return
            if not self._save():
                return
        if not messagebox.askyesno(
                APP_TITLE,
                "Provision the model volume now?\n\n"
                "This briefly rents a BILLED pod, downloads ~40 GB of models "
                "(all three SeedVR2 tiers + all three Ollama vision tiers) onto the "
                "selected volume, and terminates the pod automatically when done — usually 10-20 "
                "minutes the first time. Re-running later is safe and incremental: it "
                "keeps what is already there, prunes obsolete models, and downloads "
                "only what changed (much faster).\n\nProceed?"):
            return
        self._stream_provision()

    def _stream_provision(self):
        """Run the setup-volume subprocess and stream its output into a window."""
        win = tk.Toplevel(self)
        win.title("Provisioning the model volume")
        win.geometry("780x460")
        # Match the Batch Upscaler / Tag & Rename log console palette (LogPane):
        # dark background, light text, flat relief.
        win.configure(bg="#15181d")
        body = tk.Frame(win, bg="#15181d")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        txt = tk.Text(body, wrap="word", font=("Consolas", 9), state="disabled",
                      background="#15181d", foreground="#d7dde4",
                      insertbackground="#d7dde4", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        # A model download streams a tqdm/curl progress bar: thousands of updates
        # for a 15 GB file. Render it like a terminal — a carriage return rewrites
        # the current line instead of appending a fresh one — so the Text widget
        # stays small. Without this it grew unbounded until each insert/see got so
        # slow the Tk main loop starved and Windows flagged the window "Not
        # Responding". A hard line cap is a defensive backstop for tools that emit
        # a newline per update.
        MAX_LINES = 600
        # A "still working" heartbeat for the silent stretches: pip building/
        # installing ~30 packages, a big weight download, `ollama pull` — all emit
        # nothing for minutes, which looks identical to a hang. When the pod has
        # been quiet this long, show a single self-updating line so the user knows
        # it's alive (and isn't tempted to force-kill, which would orphan/strand
        # the provisioning pod).
        HB_AFTER = 10.0          # seconds of silence before the heartbeat appears
        HB_EVERY = 5.0           # refresh the heartbeat (its counter) this often
        last_activity = time.monotonic()
        last_hb = 0.0
        hb_active = False        # a heartbeat line is currently the last line

        def _clear_heartbeat():
            nonlocal hb_active
            if hb_active:
                txt.delete("end-1c linestart", "end-1c")
                hb_active = False

        def append(s):
            nonlocal last_activity
            txt.configure(state="normal")
            _clear_heartbeat()   # real output supersedes the transient heartbeat
            for token in re.split(r"(\r\n|\r|\n)", s):
                if not token:
                    continue
                if token in ("\n", "\r\n"):
                    txt.insert("end", "\n")
                elif token == "\r":
                    # Carriage return: drop the current (in-progress) line so the
                    # next text overwrites it — collapses a progress bar to one line.
                    txt.delete("end-1c linestart", "end-1c")
                else:
                    txt.insert("end", token)
            excess = int(txt.index("end-1c").split(".")[0]) - MAX_LINES
            if excess > 0:
                txt.delete("1.0", f"{excess + 1}.0")
            txt.see("end")
            txt.configure(state="disabled")
            last_activity = time.monotonic()

        def heartbeat():
            nonlocal hb_active, last_hb
            txt.configure(state="normal")
            if hb_active:
                txt.delete("end-1c linestart", "end-1c")   # overwrite the previous
            elif txt.get("end-1c linestart", "end-1c"):     # last line has content
                txt.insert("end", "\n")                     # give the heartbeat its own line
            secs = int(time.monotonic() - last_activity)
            txt.insert("end", f"  … still working on the pod — no new output for "
                              f"{secs}s. Large downloads and installs run silent; "
                              f"please wait …")
            hb_active = True
            last_hb = time.monotonic()
            txt.see("end")
            txt.configure(state="disabled")

        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, "runpod_provision.py"),
               "setup-volume"]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd, cwd=APP_ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, env=env)
        except Exception as exc:
            append(f"Could not start provisioning: {exc}\n")
            return

        q = queue.Queue()

        def reader():
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                q.put(dec.decode(chunk))
            q.put(None)

        def pump():
            done = False
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    done = True
                    break
                append(item)
            if done:
                code = proc.wait()
                append(f"\n--- finished (exit {code}) ---\n")
                if code == 0:
                    self.runpod_status.configure(
                        text="Model volume provisioned — remote upscaling is ready.",
                        foreground="#1a7f37")
                else:
                    self.runpod_status.configure(
                        text="Provisioning failed — see the window for details.",
                        foreground="#b3261e")
            else:
                # No output for a while but the process is alive → reassure the user.
                now = time.monotonic()
                if (now - last_activity) >= HB_AFTER and (now - last_hb) >= HB_EVERY:
                    heartbeat()
                win.after(80, pump)

        threading.Thread(target=reader, daemon=True).start()
        win.after(80, pump)

# ─────────────────────────────────────────────
#  TAB 3 — CONCILIATION
# ─────────────────────────────────────────────

# (menu label, backend mode). The first entry is the default selection.
