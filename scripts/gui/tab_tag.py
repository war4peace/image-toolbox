"""
gui/tab_tag.py
--------------
The Tag & Rename tab.
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import ssh_setup
from gui.common import (APP_TITLE, CFG, get_default_folder, set_default_folder,
                       get_install_mode, _ollama_release_vram,
                       ollama_list_models, ollama_model_present)
from gui.widgets import Tooltip
from gui.tooltab import ToolTab
from gui.dialogs import OllamaPullDialog


LANGUAGES = [
    "English", "Romanian", "French", "German", "Spanish", "Italian",
    "Portuguese", "Dutch", "Polish", "Hungarian", "Czech", "Greek",
    "Russian", "Ukrainian", "Turkish", "Swedish", "Norwegian", "Danish",
    "Finnish",
]

# Undo scopes for Tag & Rename, as (menu label, backend code) pairs.
# The first entry is the default selection.
UNDO_SCOPES = [
    ("Undo everything",   "all"),
    ("File names only",   "names"),
    ("Descriptions only", "exif"),
]


class TagTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Tag & Rename"
        self.mqtt_task_name = "tagging"
        self.telemetry_interval_ms = 30000      # sample every 30 s while running
        self.dir_var    = tk.StringVar()
        self.lang_var   = tk.StringVar(value="English")
        self.ftag_var   = tk.BooleanVar(value=False)
        self.fren_var   = tk.BooleanVar(value=False)
        # Remote toggle defaults ON for a Remote-only install (the only way to tag
        # there — no local Ollama/torch); OFF for Local/Both.
        self.remote_var = tk.BooleanVar(value=(get_install_mode() == "remote"))
        # Live GPU picker: the vision model needs only ~6.6 GB, so a 16 GB floor
        # is plenty; the persisted preference is runpod.tag_gpu_type_id.
        self._gpu_min_vram = 16
        self._gpu_pref_key = "tag_gpu_type_id"
        self._bench_task   = "tag"       # $/100 readout: tag benchmark rows
        self.scope_var  = tk.StringVar(value=UNDO_SCOPES[0][0])
        self._mode      = "tag"          # "tag" | "undo" — for the exit message
        self._build()

        # Restore the pinned default folder from config.json
        self.restore_defaults_if_empty()
        self.dir_var.trace_add("write", lambda *_: self._refresh_dir_buttons())
        self._refresh_dir_buttons()

    def restore_defaults_if_empty(self):
        if not self.dir_var.get().strip():
            tag_default = get_default_folder("tag_folder")
            if tag_default and os.path.isdir(tag_default):
                self.dir_var.set(tag_default)

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_dir).grid(row=0, column=2, pady=3)
        self.save_dir_btn = ttk.Button(self, text="Save as Default", command=self._save_default)
        self.save_dir_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        opts = ttk.Frame(self)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Description language:").pack(side="left")
        ttk.Combobox(opts, textvariable=self.lang_var, values=LANGUAGES,
                     width=14).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(opts, text="Force Tag all images",
                        variable=self.ftag_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="Force Rename all images",
                        variable=self.fren_var).pack(side="left")

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.start_btn  = ttk.Button(btns, text="Start tagging", command=self._start)
        self.resume_btn = ttk.Button(btns, text="Resume after error", command=self._resume, state="disabled")
        self.stop_btn   = ttk.Button(btns, text="Stop after current image", command=self._stop, state="disabled")
        self.open_btn    = ttk.Button(btns, text="Open photo folder", command=self._open_dir)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log, state="disabled")
        for b in (self.start_btn, self.resume_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))

        # "Run on remote pod" + the live GPU picker share a dedicated row. The
        # checkbox tooltip is set here (overriding the generic one) because remote
        # tagging is the unusual case: tagging runs locally, only the model calls
        # go over the tunnel.
        self._build_remote_row(row=3)
        Tooltip(self.remote_chk,
                "Run the vision model (and the auto-straighten CNN) on a rented "
                "RunPod GPU instead of this PC (roadmap #1, experimental). Tagging "
                "itself runs locally; only the model calls go over an SSH tunnel. "
                "Creates a billed pod and terminates it when done. Needs a RunPod "
                "API key + model volume in the RunPod tab.")

        undo = ttk.LabelFrame(self, text=" Undo previous runs ", padding=(8, 4))
        undo.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(undo, text="Scope:").pack(side="left", padx=(0, 4))
        ttk.Combobox(undo, textvariable=self.scope_var, state="readonly", width=18,
                     values=[label for label, _ in UNDO_SCOPES]).pack(side="left", padx=(0, 16))
        self.undo_btn = ttk.Button(undo, text="Undo this folder…", command=self._undo)
        self.undo_btn.pack(side="left")

        self._build_output_area(row=5)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _pick_dir(self):
        folder = filedialog.askdirectory(title="Choose the folder with photos to tag")
        if folder:
            self.dir_var.set(os.path.normpath(folder))

    # ── Default-folder button ────────────────────────────────────────────────

    def _dir_valid(self):
        p = self.dir_var.get().strip()
        return bool(p) and os.path.isdir(p)

    def _refresh_dir_buttons(self):
        has_dir = bool(self.dir_var.get().strip())
        self.save_dir_btn.configure(state="normal" if self._dir_valid() else "disabled")
        # "Open photo folder" and "Undo this folder…" are meaningless with no
        # folder entered. Undo additionally stays locked while a run is active.
        self.open_btn.configure(state="normal" if has_dir else "disabled")
        self.undo_btn.configure(
            state="normal" if (has_dir and not self.running) else "disabled")
        # No photo folder, nothing to tag — but never re-enable mid-run.
        if not self.running:
            self.start_btn.configure(state="normal" if has_dir else "disabled")

    def _save_default(self):
        if not self._dir_valid():
            return
        set_default_folder("tag_folder", self.dir_var.get().strip())
        self.app.sync_settings_defaults()   # mirror into the Settings tab
        self.save_dir_btn.configure(text="Saved ✓")
        self.after(1200, lambda: self.save_dir_btn.configure(text="Save as Default"))

    def _open_dir(self):
        folder = self.dir_var.get().strip()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_TITLE, "Please choose a valid photo folder first.")

    def _valid_dir(self):
        folder = self.dir_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, "Please choose a valid photo folder first.")
            return None
        return folder

    def _start(self):
        folder = self._valid_dir()
        if not folder:
            return
        remote = bool(self.remote_var.get())
        extra_env = None
        if remote:
            rp = CFG.get("runpod", {})
            if not rp.get("api_key") or not rp.get("network_volume_id"):
                messagebox.showwarning(
                    APP_TITLE,
                    "Remote tagging needs a RunPod API key and a model volume.\n"
                    "Set them in the RunPod tab.")
                return
            ok_ssh, ssh_info = ssh_setup.setup(
                os.path.expandvars(rp.get("ssh_key_path", "")) or None)
            if not ok_ssh:
                messagebox.showwarning(APP_TITLE, ssh_info.get("message", "SSH setup failed."))
                return
            gpu_chain = self._selected_gpu_chain()
            if gpu_chain == []:
                messagebox.showwarning(
                    APP_TITLE,
                    "No RunPod GPU with at least 16 GB of VRAM is available in your "
                    "volume's region right now.\n\nTry again in a while, or choose a "
                    "different region for your model volume in Settings. (Press ↻ to "
                    "re-check availability.)")
                return
            if not self.confirm_deadman_safety():
                return
            gpu_note = self._gpu_confirm_note(gpu_chain)
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Run Tag & Rename on a rented RunPod GPU?\n\n"
                    "This creates a BILLED pod running the vision model, tags each "
                    "image against it (the files are still read/written here), and "
                    "terminates the pod when the run ends. The pod also "
                    f"self-terminates on an idle / max-runtime deadline.{gpu_note}\n\n"
                    "Proceed?"):
                return
            extra_env = {"IMGTBX_TAG_REMOTE": "1"}
            if gpu_chain:
                extra_env["IMGTBX_GPU_OVERRIDE"] = ",".join(gpu_chain)
        else:
            # A Remote-only install has no local Ollama or torch — refuse a local
            # tag run with a clear message instead of crashing on a missing model.
            if get_install_mode() == "remote":
                messagebox.showwarning(
                    APP_TITLE,
                    "This is a Remote-only install — the local vision model "
                    "(Ollama) and PyTorch aren't installed.\n\n"
                    "Tick 'Run on remote pod (RunPod)' to tag on a rented GPU, or "
                    "reinstall and choose Local or Both to tag on this PC.")
                return
            if not self._ensure_ollama_model():
                return
            if not self.confirm_gpu_overlap():
                return
        args = [folder, "--no-prompt"]
        if self.ftag_var.get():
            args.append("-ftag")
        if self.fren_var.get():
            args.append("-frename")
        lang = self.lang_var.get().strip()
        if lang and lang.lower() != "english":
            args.append(f"--language:{lang}")

        self._mode = "tag"
        self.progress.set(0)
        self._reset_stream_state()
        self._snapshot_cost_run(remote)
        self.eta_var.set("calculating…")
        self.status_var.set(
            "Starting the remote pod (first run takes a few minutes) …" if remote
            else "Starting — checking Ollama and scanning the folder …")
        if self.launch("tag_and_rename.py", args, extra_env=extra_env):
            self._remote_run = remote
            self._set_running(True)

    def _ensure_ollama_model(self):
        """Local tag runs need the configured Ollama vision model present, and
        Ollama does NOT auto-pull on first use (a generate against a missing model
        just fails). So before starting, check availability and, if the model is
        missing, offer to download it (the first-start Wizard normally does this,
        but it may have been skipped or its pull may have failed).

        Returns True to proceed, False to abort the run. Fail-open when Ollama can't
        be reached to check: the runner reports Ollama problems clearly on its own,
        so don't block a run on a check we couldn't complete."""
        o = CFG.get("ollama", {})
        url = o.get("url", "http://127.0.0.1:11434")
        model = o.get("model", "qwen2.5vl:7b")
        if not model:
            return True

        ok, names = ollama_list_models(url)
        if not ok:
            return True                       # server unreachable — let the runner report
        if ollama_model_present(names, model):
            return True

        if not messagebox.askyesno(
                APP_TITLE,
                f"The vision model '{model}' isn't installed yet.\n\n"
                "Tag & Rename needs it. Download it now? This can be several GB and "
                "may take a while."):
            messagebox.showinfo(
                APP_TITLE,
                "Tagging was not started.\n\n"
                f"You can download the model later with:  ollama pull {model}\n"
                "or from Settings → Re-run first-start wizard.")
            return False

        dlg = OllamaPullDialog(self, url, model)
        self.wait_window(dlg)
        if not dlg.ok:
            messagebox.showerror(
                APP_TITLE,
                f"The model '{model}' could not be downloaded:\n\n"
                f"{dlg.error or 'unknown error'}\n\nTagging was not started.")
            return False
        return True

    def _undo(self):
        folder = self._valid_dir()
        if not folder:
            return
        scope = dict(UNDO_SCOPES).get(self.scope_var.get(), "all")
        scope_text = {
            "all":   "file names, embedded descriptions, and auto-straighten rotations",
            "names": "file names only",
            "exif":  "embedded descriptions and auto-straighten rotations",
        }[scope]
        if not messagebox.askyesno(
                APP_TITLE,
                f"Undo the changes made by previous runs in:\n{folder}\n\n"
                f"This restores: {scope_text}.\n\nContinue?"):
            return
        args = [folder, "--undo-all"]
        if scope == "names":
            args.append("--names-only")
        elif scope == "exif":
            args.append("--exif-only")

        self._mode = "undo"
        self._remote_run = False    # undo is always local file I/O
        self.progress.set(0)
        self._reset_stream_state()
        self.status_var.set("Undoing previous changes …")
        if self.launch("tag_and_rename.py", args):
            self._set_running(True)

    def _resume(self):
        self.send("r")
        self.status_var.set("Resuming …")

    def _stop(self):
        if getattr(self, "_remote_run", False):
            idle = int(CFG.get("runpod", {}).get("idle_timeout_minutes", 15))
            # Yes → stop the pod now · No → leave it running · Cancel → keep going.
            ans = messagebox.askyesnocancel(
                APP_TITLE,
                "The tagging run will stop after the current image.\n\n"
                "Also STOP the remote pod now?\n\n"
                "• Yes — stop the pod immediately (billing stops now).\n"
                f"• No — leave it running; the dead-man's switch stops it "
                f"automatically after {idle} min of inactivity.\n\n"
                "Cancel — keep the run going.")
            if ans is None:
                return                              # cancelled — keep running
            self.send("qstop" if ans else "qkeep")
        else:
            self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping — finishes the current image first …")

    def _set_running(self, running):
        self.resume_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"     if running else "disabled")
        # Start and Undo are locked while running; otherwise they follow the
        # folder state (no folder entered → nothing to act on).
        has_dir = bool(self.dir_var.get().strip())
        self.start_btn.configure(
            state="normal" if (not running and has_dir) else "disabled")
        self.undo_btn.configure(
            state="normal" if (not running and has_dir) else "disabled")
        self.app.refresh_conciliate_lock()

    def on_exit(self, code):
        self._set_running(False)
        self.eta_var.set("—")
        # Backup VRAM release — the script unloads the model itself on a
        # graceful exit, but not if it was killed (e.g. app closed mid-image).
        threading.Thread(target=_ollama_release_vram, daemon=True).start()
        # Drain any strip thumbnail decodes still in flight after exit
        for delay in (250, 1000, 3000):
            self.after(delay, self._tick)
        if code == 0:
            self.progress.set(100)
            if self._mode == "undo":
                self.status_var.set("Undo finished — see the summary above.")
            else:
                self.status_var.set("Done. Descriptions written and files renamed where applicable.")
        else:
            self.status_var.set(self._failure_status(code))
        super().on_exit(code)


# ─────────────────────────────────────────────
#  SETTINGS TAB
# ─────────────────────────────────────────────

# Resolution Target presets → (max_resolution, resolution)
