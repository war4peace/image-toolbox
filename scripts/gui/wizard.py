"""
gui/wizard.py
-------------
First-start Wizard (0.4.6): a one-time onboarding dialog shown the first time a
new user launches the app (guarded by the `wizard_done` flag in gui_settings.json).

It detects the local GPU and pre-selects the SeedVR2 upscale model + Ollama vision
model that fit the card's VRAM (see gui/wizard_recommend.py for the pure tier
logic and docs/first-start-wizard.md for the design). The recommendation is a
suggestion, not a gate: every model stays selectable.

SKELETON (step 1 of the build): the navigation scaffold, GPU detection and the
recommendation step are functional and Finish writes the chosen models to config.
The Ollama one-click pull step and the remote-setup step are placeholders wired
into the flow but implemented in later steps of the build.
"""
import threading
import tkinter as tk
from tkinter import ttk

import system_telemetry
from gui.common import (APP_TITLE, CFG, save_config, get_install_mode,
                        mark_wizard_completed, save_settings, ollama_installed,
                        ollama_list_models, ollama_model_present, ollama_pull)
import gui.wizard_recommend as wr


def _fmt_gb(completed, total):
    """'1.2 / 6.0 GB' for a pull's byte counts, or '' when sizes aren't known yet."""
    if not total:
        return ""
    gb = 1024 ** 3
    return f"{(completed or 0) / gb:.1f} / {total / gb:.1f} GB"


def should_show(app=None):
    """Whether the wizard should appear on this launch: only when it has not run
    before. Kept as a function so app.py has a single, testable gate."""
    from gui.common import wizard_completed
    return not wizard_completed()


class FirstStartWizard(tk.Toplevel):
    """Modal onboarding wizard. Built as an ordered list of step builders chosen by
    install mode, with a shared Back / Next / Skip / Finish nav bar."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.install_mode = get_install_mode()

        # Detected GPU, filled asynchronously (sample_gpu spawns nvidia-smi, which
        # blocks; never on the UI thread). None until detection returns.
        self.gpu_name = None
        self.vram_total_mb = None
        self._gpu_done = False
        self.recommendation = wr.recommend_models(None)   # provisional until detected

        # The user's current picks (default to the recommendation; overridable).
        self.dit_choice = tk.StringVar()
        self.ollama_choice = tk.StringVar()

        # Ollama-pull step state (a small state machine; see _step_ollama_pull):
        #   None -> checking -> present | missing | no_ollama | unreachable
        #   missing --Download--> pulling -> pulled | error
        self._oll_state = None
        self._oll_error = None
        self._oll_progress_var = None
        self._oll_status_lbl = None

        self.title("Welcome to Image Toolbox")
        self.transient(app)
        self.resizable(True, True)
        self.minsize(560, 420)
        self.protocol("WM_DELETE_WINDOW", self._skip)

        # Step order depends on install mode:
        #   remote-only  : welcome -> remote setup -> finish  (no local GPU)
        #   local/both   : welcome -> GPU + models -> Ollama pull -> [remote] -> finish
        if self.install_mode == "remote":
            self._steps = [self._step_welcome, self._step_remote, self._step_finish]
        else:
            self._steps = [self._step_welcome, self._step_gpu, self._step_ollama_pull]
            if self.install_mode == "both":
                self._steps.append(self._step_remote)
            self._steps.append(self._step_finish)
        self._i = 0

        self._build_shell()
        self._render()

        # Center on the parent and grab focus (same pattern as UpdateDialog).
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        self.grab_set()
        self.focus_set()

        # Kick off GPU detection for the local/both flow.
        if self.install_mode != "remote":
            threading.Thread(target=self._detect_gpu, daemon=True).start()

    # ── shell / navigation ───────────────────────────────────────────────────

    def _build_shell(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        self._content = ttk.Frame(outer)
        self._content.grid(row=0, column=0, sticky="nsew")
        self._content.columnconfigure(0, weight=1)

        nav = ttk.Frame(outer)
        nav.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self._back_btn = ttk.Button(nav, text="Back", command=self._back)
        self._next_btn = ttk.Button(nav, text="Next", command=self._next)
        self._skip_btn = ttk.Button(nav, text="Skip", command=self._skip)
        self._skip_btn.pack(side="left")
        self._next_btn.pack(side="right")
        self._back_btn.pack(side="right", padx=(0, 6))

    def _render(self):
        for w in self._content.winfo_children():
            w.destroy()
        self._steps[self._i](self._content)
        self._back_btn.configure(state=("disabled" if self._i == 0 else "normal"))
        last = self._i == len(self._steps) - 1
        self._next_btn.configure(text=("Finish" if last else "Next"))

    def _showing(self, builder):
        """True if the step currently on screen IS `builder`. Compares the
        underlying functions, not the bound methods: `self._step_x` creates a fresh
        bound-method object on every access, so `self._steps[self._i] is
        self._step_x` is ALWAYS False (that bug left the async 'Checking …' state
        stuck until the user navigated away and back)."""
        def fn(x):
            return getattr(x, "__func__", x)
        try:
            return fn(self._steps[self._i]) is fn(builder)
        except Exception:
            return False

    def _next(self):
        if self._i == len(self._steps) - 1:
            self._finish()
        else:
            self._i += 1
            self._render()

    def _back(self):
        if self._i > 0:
            self._i -= 1
            self._render()

    def _skip(self):
        # Skipping still marks the wizard done (it must never reappear) and leaves
        # config untouched at its defaults.
        self._mark_done()
        self.destroy()

    def _finish(self):
        self._apply_config()
        self._mark_done()
        self.destroy()

    def _mark_done(self):
        """Record wizard_done so it never reappears. Critically, write it through
        the App's SHARED gui_settings dict (`app.settings`) that every geometry save
        also rewrites: writing a separate disk copy (mark_wizard_completed) would be
        clobbered the next time the app saved its window geometry, and the wizard
        would return on every launch. Falls back to the disk helper when the parent
        is not a full App (e.g. tests pass a bare Tk root)."""
        try:
            st = getattr(self.app, "settings", None)
            if isinstance(st, dict):
                st["wizard_done"] = True
                save_settings(st)
                return
        except Exception:
            pass
        mark_wizard_completed()

    # ── config write ─────────────────────────────────────────────────────────

    def _apply_config(self):
        """Write the chosen models to CFG and persist (config_store keeps secrets
        in the overlay). Only touches the three model keys; everything else is left
        as-is. Fail-safe: a bad save must not crash the app on first launch.

        Remote-only installs skip the GPU/model steps entirely, so there is no
        user choice to write: leave the shipped defaults (7B FP16 + qwen2.5vl:7b),
        which suit the big pod GPU the user will pick per-run, rather than seeding
        the no-GPU lowest tier."""
        if self.install_mode == "remote":
            return
        dit = self._id_for(wr.SEEDVR_OPTIONS, self.dit_choice.get()) or \
            self.recommendation.dit_model
        oll = self._id_for(wr.OLLAMA_OPTIONS, self.ollama_choice.get()) or \
            self.recommendation.ollama_model
        try:
            CFG.setdefault("upscale", {})["dit_model"] = dit
            CFG.setdefault("video", {})["dit_model"] = dit
            CFG.setdefault("ollama", {})["model"] = oll
            save_config(CFG)
        except Exception:
            pass

    @staticmethod
    def _id_for(options, label):
        for lbl, mid in options:
            if lbl == label:
                return mid
        return None

    # ── GPU detection (worker thread) ────────────────────────────────────────

    def _detect_gpu(self):
        name = system_telemetry.gpu_name()
        gpu = system_telemetry.sample_gpu()
        vram = gpu[1] if gpu else None
        self.after(0, lambda: self._on_gpu_detected(name, vram))

    def _on_gpu_detected(self, name, vram_total_mb):
        self.gpu_name = name
        self.vram_total_mb = vram_total_mb
        self.recommendation = wr.recommend_models(vram_total_mb)
        self._gpu_done = True
        # If the GPU step is on screen right now, re-render it with real data.
        if self._showing(self._step_gpu):
            self._render()

    # ── steps ────────────────────────────────────────────────────────────────

    def _step_welcome(self, parent):
        ttk.Label(parent, text="Welcome to Image Toolbox",
                  font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        msg = ("This quick setup detects your graphics card and picks the upscaling "
               "and tagging models that best suit it.\n\n"
               "You can change any choice now or later in Settings. Press Skip to "
               "keep the defaults.")
        if self.install_mode == "remote":
            msg = ("This install is remote-only (the GPU work runs on a rented pod). "
                   "This quick setup helps you get the remote side ready.\n\n"
                   "Press Skip to configure it later in Settings.")
        ttk.Label(parent, text=msg, wraplength=500, justify="left",
                  foreground="#666").grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _step_gpu(self, parent):
        ttk.Label(parent, text="Your graphics card",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")

        if not self._gpu_done:
            ttk.Label(parent, text="Detecting GPU …", foreground="#666").grid(
                row=1, column=0, sticky="w", pady=(8, 0))
            return

        if self.vram_total_mb:
            head = f"{self.gpu_name or 'NVIDIA GPU'}  |  {wr.vram_mb_to_gb(self.vram_total_mb)} GB VRAM"
        else:
            head = ("No NVIDIA GPU detected. You can still pick models below, but "
                    "local upscaling needs an NVIDIA card.")
        ttk.Label(parent, text=head, wraplength=500, justify="left").grid(
            row=1, column=0, sticky="w", pady=(8, 8))

        rec = self.recommendation
        box = ttk.LabelFrame(parent, text="  Recommended models  ", padding=10)
        box.grid(row=2, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Upscaling (SeedVR2):").grid(row=0, column=0, sticky="w")
        self.dit_choice.set(wr.label_for(wr.SEEDVR_OPTIONS, rec.dit_model))
        ttk.Combobox(box, textvariable=self.dit_choice, state="readonly",
                     values=[lbl for lbl, _ in wr.SEEDVR_OPTIONS], width=44).grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(box, text="Tagging (Ollama):").grid(row=1, column=0, sticky="w")
        self.ollama_choice.set(wr.label_for(wr.OLLAMA_OPTIONS, rec.ollama_model))
        ttk.Combobox(box, textvariable=self.ollama_choice, state="readonly",
                     values=[lbl for lbl, _ in wr.OLLAMA_OPTIONS], width=44).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(parent, wraplength=500, justify="left", foreground="#888",
                  text=("These are pre-selected for your VRAM. A heavier model gives "
                        "more detail but runs slower on a smaller card.")).grid(
            row=3, column=0, sticky="w", pady=(10, 0))

    def _ollama_selected_model(self):
        """The Ollama model id the user has chosen (or the recommendation)."""
        return self._id_for(wr.OLLAMA_OPTIONS, self.ollama_choice.get()) or \
            self.recommendation.ollama_model

    def _ollama_url(self):
        return CFG.get("ollama", {}).get("url", "http://127.0.0.1:11434")

    def _step_ollama_pull(self, parent):
        model = self._ollama_selected_model()
        ttk.Label(parent, text="Tagging model",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")

        # First time this step is shown, kick off the presence check off-thread.
        if self._oll_state is None:
            self._oll_state = "checking"
            threading.Thread(target=self._check_ollama_present,
                             args=(model,), daemon=True).start()

        state = self._oll_state
        if state == "checking":
            ttk.Label(parent, foreground="#666",
                      text=f"Checking whether {model} is installed …").grid(
                row=1, column=0, sticky="w", pady=(8, 0))

        elif state == "no_ollama":
            ttk.Label(parent, wraplength=500, justify="left", foreground="#666",
                      text=("Ollama is not installed, so the tagging model can't be "
                            "downloaded here. You can install Ollama and pull the "
                            f"model ({model}) later; Tag & Rename will remind you.")).grid(
                row=1, column=0, sticky="w", pady=(8, 0))

        elif state == "unreachable":
            ttk.Label(parent, wraplength=500, justify="left", foreground="#666",
                      text=(f"Could not reach Ollama at {self._ollama_url()}.\n"
                            "Start Ollama, then Retry. You can also skip this and "
                            "pull the model later.")).grid(
                row=1, column=0, sticky="w", pady=(8, 0))
            if self._oll_error:
                ttk.Label(parent, text=self._oll_error, foreground="#888",
                          wraplength=500).grid(row=2, column=0, sticky="w", pady=(4, 0))
            ttk.Button(parent, text="Retry", command=self._retry_ollama_check).grid(
                row=3, column=0, sticky="w", pady=(8, 0))

        elif state == "present":
            ttk.Label(parent, foreground="#1a7f37",
                      text=f"✓ {model} is already installed.").grid(
                row=1, column=0, sticky="w", pady=(8, 0))

        elif state == "pulled":
            ttk.Label(parent, foreground="#1a7f37",
                      text=f"✓ {model} downloaded and ready.").grid(
                row=1, column=0, sticky="w", pady=(8, 0))

        elif state in ("missing", "error", "pulling"):
            head = f"{model} is not installed yet."
            if state == "error":
                head = f"The download of {model} did not finish."
            ttk.Label(parent, wraplength=500, justify="left", text=head).grid(
                row=1, column=0, sticky="w", pady=(8, 4))
            if state == "error" and self._oll_error:
                ttk.Label(parent, text=self._oll_error, foreground="#b3261e",
                          wraplength=500).grid(row=2, column=0, sticky="w")

            self._oll_progress_var = tk.DoubleVar(value=0.0)
            prog = ttk.Progressbar(parent, mode="determinate", maximum=100,
                                   variable=self._oll_progress_var)
            self._oll_status_lbl = ttk.Label(parent, text="", foreground="#666")
            btn = ttk.Button(parent, text="Download model",
                             command=lambda: self._start_ollama_pull(model))

            if state == "pulling":
                # Resume the visible progress UI (nav is disabled while pulling).
                prog.grid(row=3, column=0, sticky="ew", pady=(6, 2))
                self._oll_status_lbl.grid(row=4, column=0, sticky="w")
                self._oll_status_lbl.configure(text="Starting download …")
            else:
                btn.grid(row=3, column=0, sticky="w", pady=(6, 0))
                ttk.Label(parent, foreground="#888", wraplength=500,
                          text="You can also skip this and pull the model later.").grid(
                    row=4, column=0, sticky="w", pady=(6, 0))

    # ── Ollama check / pull ──────────────────────────────────────────────────

    def _retry_ollama_check(self):
        self._oll_state = None
        self._oll_error = None
        self._render()

    def _check_ollama_present(self, model):
        if not ollama_installed():
            self._ui(lambda: self._set_oll_state("no_ollama"))
            return
        ok, value = ollama_list_models(self._ollama_url())
        if not ok:
            self._oll_error = str(value)
            self._ui(lambda: self._set_oll_state("unreachable"))
            return
        present = ollama_model_present(value, model)
        self._ui(lambda: self._set_oll_state("present" if present else "missing"))

    def _start_ollama_pull(self, model):
        # Navigation stays enabled: a multi-GB pull can take minutes, the download
        # continues server-side even if the user moves on, and the _ui() guard keeps
        # a late callback from touching a closed wizard.
        self._set_oll_state("pulling")
        threading.Thread(target=self._pull_worker, args=(model,), daemon=True).start()

    def _pull_worker(self, model):
        def on_progress(status, completed, total):
            self._ui(lambda: self._on_pull_progress(status, completed, total))
        ok, err = ollama_pull(self._ollama_url(), model, progress_cb=on_progress)
        self._oll_error = None if ok else str(err)
        self._ui(lambda: self._on_pull_done(ok))

    def _on_pull_progress(self, status, completed, total):
        if self._oll_status_lbl is None or not self._oll_status_lbl.winfo_exists():
            return
        size = _fmt_gb(completed, total)
        self._oll_status_lbl.configure(text=f"{status} {size}".strip())
        if total and self._oll_progress_var is not None:
            self._oll_progress_var.set((completed or 0) / total * 100)

    def _on_pull_done(self, ok):
        self._set_oll_state("pulled" if ok else "error")

    def _set_oll_state(self, state):
        self._oll_state = state
        if self._showing(self._step_ollama_pull):
            self._render()

    def _ui(self, fn):
        """Run fn on the UI thread iff the wizard still exists (a worker may finish
        after the user skipped/closed the wizard)."""
        try:
            if self.winfo_exists():
                self.after(0, fn)
        except Exception:
            pass

    def _step_remote(self, parent):
        ttk.Label(parent, text="Remote upscaling",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        optional = self.install_mode == "both"
        if optional:
            note = ("You can also run upscaling and tagging on a rented GPU pod, "
                    "handy if your own card struggles. Set it up now on the RunPod "
                    "tab (SSH key + model volume), or skip and do it later.")
        else:
            note = ("This install is remote-only, so the GPU work runs on a rented "
                    "pod. Finish the setup on the RunPod tab: set up the SSH key and "
                    "provision a model volume.")
        ttk.Label(parent, wraplength=500, justify="left", foreground="#666",
                  text=note).grid(row=1, column=0, sticky="w", pady=(8, 6))
        ttk.Button(parent, text="Open the RunPod tab",
                   command=self._go_to_runpod).grid(row=2, column=0, sticky="w",
                                                    pady=(4, 0))

    def _go_to_runpod(self):
        """Route to where the remote controls actually live (the RunPod tab), then
        close the wizard. Fail-safe: if the tab can't be selected, just close."""
        try:
            self.app.nb.select(self.app.runpod_tab)
        except Exception:
            pass
        self._finish()

    def _step_finish(self, parent):
        ttk.Label(parent, text="You're all set",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        dit = self.dit_choice.get() or wr.label_for(wr.SEEDVR_OPTIONS,
                                                    self.recommendation.dit_model)
        oll = self.ollama_choice.get() or wr.label_for(wr.OLLAMA_OPTIONS,
                                                       self.recommendation.ollama_model)
        summary = "Press Finish to save your choices."
        if self.install_mode != "remote":
            summary = (f"Upscaling: {dit}\nTagging: {oll}\n\n"
                       "Press Finish to save these to Settings.")
        ttk.Label(parent, text=summary, wraplength=500, justify="left",
                  foreground="#666").grid(row=1, column=0, sticky="w", pady=(8, 0))
