"""
gui/dialogs.py
--------------
Standalone dialogs (the in-app update dialog).
"""

import os
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
import updater
from gui.common import (APP_ROOT, APP_TITLE, APP_VERSION, set_update_skipped_version,
                       ollama_pull)


class UpdateDialog(tk.Toplevel):
    """
    Shows that a newer release is available, displays its patch notes, and (on
    confirmation) downloads the installer and launches it, then quits the app so
    Inno Setup can replace the running scripts. See updater.py.
    """

    def __init__(self, app, info):
        super().__init__(app)
        self.app  = app
        self.info = info
        self._downloading = False

        self.title("Update available")
        # Own the app icon explicitly. iconbitmap(default=...) on the root already
        # registers it for Toplevels (0.4.7), but be explicit here: this is the one
        # window a user sees while updating FROM an older build whose root never set
        # the default. Fail-safe (a missing icon just keeps the tk feather).
        try:
            self.iconbitmap(os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        self.transient(app)
        self.resizable(True, True)
        self.minsize(800, 420)          # min width 800px (requested)
        self.protocol("WM_DELETE_WINDOW", self._later)
        self._build()

        # Center over the main window at a minimum 800px width.
        self.update_idletasks()
        try:
            w = max(self.winfo_width(), 800)
            h = self.winfo_height()
            x = app.winfo_rootx() + (app.winfo_width()  - w) // 2
            y = app.winfo_rooty() + (app.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        # Modal: block the main window until dismissed. wait_visibility first, since
        # grab_set can silently fail on a not-yet-mapped window.
        try:
            self.wait_visibility()
            self.grab_set()
        except Exception:
            pass
        self.focus_set()

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text=f"Image Toolbox {self.info.version} is available",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=f"You're running {APP_VERSION}.",
                  foreground="#666").grid(row=1, column=0, sticky="w", pady=(2, 8))

        notes_frame = ttk.LabelFrame(outer, text="  What's new  ", padding=6)
        notes_frame.grid(row=2, column=0, sticky="nsew")
        notes_frame.rowconfigure(0, weight=1)
        notes_frame.columnconfigure(0, weight=1)

        txt = tk.Text(notes_frame, wrap="word", height=12, relief="flat",
                      background=self.cget("background"))
        scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        txt.insert("1.0", self.info.notes or "(No release notes were provided.)")
        txt.configure(state="disabled")

        # Progress (hidden until a download starts)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(outer, mode="determinate",
                                        maximum=100, variable=self.progress_var)
        self.status_lbl = ttk.Label(outer, text="", foreground="#666")

        # Buttons
        btns = ttk.Frame(outer)
        btns.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.update_btn = ttk.Button(btns, text="Update now", command=self._update_now)
        self.skip_btn   = ttk.Button(btns, text="Skip this version", command=self._skip)
        self.later_btn  = ttk.Button(btns, text="Remind me later", command=self._later)
        self.update_btn.pack(side="left")
        self.later_btn.pack(side="right")
        self.skip_btn.pack(side="right", padx=(0, 6))

    # ── actions ────────────────────────────────────────────────────────────────

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.update_btn, self.skip_btn, self.later_btn):
            b.configure(state=state)

    def _update_now(self):
        if self._downloading:
            return
        if not self.info.asset_url:
            if messagebox.askyesno(
                    APP_TITLE,
                    "This release has no installer attached.\n\n"
                    "Open the releases page in your browser instead?"):
                webbrowser.open(updater.RELEASES_PAGE)
            return
        # Refuse to self-update while a tool is mid-run — replacing files then
        # would be unsafe. (The tabs lock each other, so checking all is enough.)
        busy = [t for t in (self.app.upscale_tab, self.app.video_tab, self.app.tag_tab,
                            self.app.conciliate_tab) if t.running]
        if busy:
            messagebox.showwarning(
                APP_TITLE, "Please let the current task finish before updating.")
            return

        self._downloading = True
        self._set_busy(True)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(10, 2))
        self.status_lbl.grid(row=4, column=0, sticky="w")
        self.status_lbl.configure(text="Downloading the installer …")

        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        def on_progress(done, total):
            pct = (done / total * 100) if total else 0
            self.after(0, lambda: self._on_progress(done, total, pct))
        try:
            path = updater.download_installer(
                self.info.asset_url, expected_size=self.info.asset_size,
                sha256_url=self.info.sha256_url, progress_cb=on_progress)
        except Exception as exc:
            # Bind the message now: `exc` is unbound once this except block
            # exits, and the after() lambda runs later (it would NameError).
            msg = str(exc)
            self.after(0, lambda: self._on_download_error(msg))
            return
        self.after(0, lambda: self._on_download_done(path))

    def _on_progress(self, done, total, pct):
        self.progress_var.set(pct)
        if total:
            self.status_lbl.configure(
                text=f"Downloading … {done // (1024*1024)} / {total // (1024*1024)} MB")
        else:
            self.status_lbl.configure(text=f"Downloading … {done // (1024*1024)} MB")

    def _on_download_error(self, msg):
        self._downloading = False
        self._set_busy(False)
        self.progress.grid_remove()
        self.status_lbl.configure(text="Download failed.", foreground="#b3261e")
        messagebox.showerror(APP_TITLE, f"The update could not be downloaded:\n\n{msg}")

    def _on_download_done(self, path):
        self.status_lbl.configure(
            text="Starting the installer — the app will now close.", foreground="#1a7f37")
        try:
            updater.launch_installer(path)
        except Exception as exc:
            self._on_download_error(f"Could not start the installer: {exc}")
            return
        # Quit so Inno can overwrite the running scripts; the installer will
        # offer to relaunch the app when it finishes.
        self.app._save_geometry()
        if self.app.log_window is not None and self.app.log_window.winfo_exists():
            self.app.log_window.save_geometry()
        self.app.destroy()

    def _skip(self):
        set_update_skipped_version(self.info.version)
        self.destroy()

    def _later(self):
        if self._downloading:
            return
        self.destroy()


class OllamaPullDialog(tk.Toplevel):
    """
    Modal progress dialog that pulls one Ollama model (common.ollama_pull over
    HTTP), for the Tag & Rename tab's "download the model before tagging" safety
    net. Blocks the caller via wait_window; read `.ok` (and `.error`) afterwards.
    Shared with the first-start Wizard's pull path only in spirit — both call the
    same common.ollama_pull, but this one is a standalone modal.
    """

    def __init__(self, parent, url, model):
        super().__init__(parent)
        self.ok = False
        self.error = None
        self.model = model

        self.title("Downloading model")
        self.transient(parent)
        self.resizable(False, False)
        # No close button mid-pull: a stalled pull ends itself via ollama_pull's
        # per-read timeout (surfaced as an error), so there is no way to get stuck.
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"Downloading {model} …",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self._var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(outer, mode="determinate", maximum=100,
                        variable=self._var, length=380).pack(fill="x", pady=(10, 4))
        self._status = ttk.Label(outer, text="Starting …", foreground="#666")
        self._status.pack(anchor="w")

        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        self.grab_set()

        threading.Thread(target=self._worker, args=(url, model), daemon=True).start()

    def _worker(self, url, model):
        def prog(status, completed, total):
            self.after(0, lambda: self._on_progress(status, completed, total))
        ok, err = ollama_pull(url, model, progress_cb=prog)
        self.after(0, lambda: self._done(ok, err))

    def _on_progress(self, status, completed, total):
        if not self.winfo_exists():
            return
        if total:
            self._var.set((completed or 0) / total * 100)
            gb = 1024 ** 3
            self._status.configure(
                text=f"{status}  {(completed or 0) / gb:.1f} / {total / gb:.1f} GB".strip())
        else:
            self._status.configure(text=status or "Downloading …")

    def _done(self, ok, err):
        self.ok = ok
        self.error = None if ok else str(err)
        self.destroy()


# Progress-bar debug (TEMPORARY, being validated): log every bar value change with the
# inputs, so the bar can be checked over a long run without eyeballing it. Modes:
#   "window" -> the log window (and backlog) — what you watch live;
#   "file"   -> logs/video_progress_debug.log only, kept OFF the window (flip to this
#               once the bar is trusted, so the window stays clean);
#   None     -> off (remove the instrumentation).
