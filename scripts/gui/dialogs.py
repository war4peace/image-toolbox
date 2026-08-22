"""
gui/dialogs.py
--------------
Standalone dialogs: the in-app update dialog, the one-model Ollama pull, and the
diagnostics review shown before a bug report leaves the machine (#24).

This module may import both `gui.common` and `gui.widgets`; `gui.common` may import
neither, which is why the log-collapse pattern is injected into `diagnostics` from
here rather than read there.
"""

import io
import os
import tempfile
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
import updater
import diagnostics
from gui.common import (APP_ROOT, APP_TITLE, APP_VERSION, CFG,
                        set_update_skipped_version, ollama_pull,
                        open_in_explorer, report_issue)
from gui.widgets import Tooltip, COLLAPSE_PROCESSING_RE

try:                                                    # optional, never required
    from debug_log import debug_log
except Exception:                                       # pragma: no cover
    def debug_log(*_a, **_k):
        pass


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


def prompt_install_triton(parent, on_done=None):
    """Offer to download + install the verified triton-windows wheel on demand, so the local
    video engine can use torch.compile (feature #7). If Triton is already present, calls
    on_done(True) at once. Otherwise asks, runs the install off the UI thread behind a small
    progress dialog, and calls on_done(bool installed). Mirrors prompt_install_libvlc;
    fail-safe (never raises into the GUI)."""
    import triton_setup

    if triton_setup.triton_installed():
        if on_done:
            on_done(True)
        return
    spec = triton_setup.wheel_for_env()
    if spec is None:
        messagebox.showinfo(
            APP_TITLE, "No matching Triton build is pinned for this Python/PyTorch, so the "
                       "compile speedup can't be enabled automatically. Local runs still work "
                       "without it.")
        if on_done:
            on_done(False)
        return
    if not messagebox.askyesno(
            APP_TITLE,
            f"Enable the local torch.compile speedup?\n\nThis downloads and installs Triton "
            f"(triton-windows {spec['version']}, a ~50 MB verified download) into the app's "
            f"environment. Local video runs then compile like the rented-pod runs do.\n\n"
            f"Install now?"):
        if on_done:
            on_done(False)
        return

    dlg = tk.Toplevel(parent)
    dlg.title("Installing Triton")
    dlg.transient(parent)
    dlg.resizable(False, False)
    ttk.Label(dlg, text="Downloading and installing Triton…",
              padding=(14, 12, 14, 4)).pack()
    pb = ttk.Progressbar(dlg, mode="determinate", length=320, maximum=100)
    pb.pack(padx=14, pady=(0, 6))
    status = tk.StringVar(value="Starting…")
    ttk.Label(dlg, textvariable=status, foreground="#7f8a99",
              padding=(14, 0, 14, 12)).pack()
    try:
        dlg.grab_set()
    except tk.TclError:
        pass

    def _set(msg):
        try:
            status.set(msg)
            # Reflect the "Downloading … NN%" lines on the bar; ignore non-percent messages.
            if "%" in msg:
                pct = msg.rsplit(" ", 1)[-1].rstrip("%")
                if pct.isdigit():
                    pb.configure(mode="determinate")
                    pb["value"] = int(pct)
            else:
                pb.configure(mode="indeterminate")
                pb.start(12)
        except (tk.TclError, ValueError):
            pass

    def _done(ok, msg):
        try:
            pb.stop()
        except tk.TclError:
            pass
        try:
            dlg.grab_release()
            dlg.destroy()
        except tk.TclError:
            pass
        if not ok:
            messagebox.showerror(APP_TITLE, f"Could not install Triton:\n{msg}\n\n"
                                            "Local runs still work without the compile speedup.")
        if on_done:
            on_done(bool(ok))

    def work():
        ok, msg = triton_setup.install(progress=lambda m: parent.after(0, _set, m))
        parent.after(0, _done, ok, msg)

    threading.Thread(target=work, daemon=True).start()

class DiagnosticsDialog(tk.Toplevel):
    """
    Future feature #24: the review step before a bug report leaves the machine.

    The flow is deliberately one thing at a time. This dialog opens FIRST, with
    nothing else on screen; only when the user presses the report button does the
    browser open and Explorer come up behind it with the zip selected. Opening
    three surfaces at once is disorienting, and the user needs to have read what is
    in the zip before they are looking at a form.

    Opening Explorer with the file selected is NOT inspection: nobody unzips twelve
    files to audit them. So the dialog does that work: it lists what the zip holds
    with sizes, states in one line what was removed, and says plainly that
    attaching the file to a public issue publishes it permanently. That disclosure
    is the reason the redaction defaults are the aggressive ones, and the reason
    there is no "include real names" opt-out anywhere in this feature.

    Gathering runs OFF the UI thread: it reads the cache DB, several megabytes of
    log, and shells out to nvidia-smi for the card's VRAM.
    """

    def __init__(self, app, extra_logs=()):
        super().__init__(app)
        self.app = app
        self._extra_logs = list(extra_logs or ())
        self._report = None
        self._zip_path = None
        self._map_path = None

        self.title("Report an issue")
        try:
            self.iconbitmap(os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        self.transient(app)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self.update_idletasks()
        self._centre_on(app)
        threading.Thread(target=self._gather, daemon=True).start()

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer, wraplength=560, justify="left",
            text=("The app can attach a diagnostics file to your report, so you do "
                  "not have to describe your setup or find any logs.\n\n"
                  "Folder names and file names are replaced with short codes, and "
                  "anything the app wrote about what is IN your pictures (the "
                  "descriptions Tag & Rename generates) is removed outright. So the "
                  "file does not reveal what your photos are or where they live.")
        ).grid(row=0, column=0, sticky="we")

        self.status = ttk.Label(outer, text="Collecting diagnostics ...",
                                foreground="#7f8a99")
        self.status.grid(row=1, column=0, sticky="w", pady=(10, 4))

        box = ttk.LabelFrame(outer, text="What the file contains", padding=(8, 6))
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listing = tk.Text(box, height=8, width=68, wrap="none",
                               relief="flat", background=self.cget("background"))
        self.listing.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.listing.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listing.configure(yscrollcommand=scroll.set, state="disabled")

        # These two lines were inside the scrolling list in the first cut, and a
        # screenshot showed them sitting below the fold behind a scrollbar nobody
        # is going to drag. What was redacted, and where the private key to it went,
        # are the two things the user most needs to read, so they are fixed labels
        # under the box and the box holds nothing but the file table.
        self.redaction_note = ttk.Label(outer, wraplength=560, justify="left",
                                        foreground="#7f8a99", text="")
        self.redaction_note.grid(row=3, column=0, sticky="we", pady=(6, 0))
        self.map_note = ttk.Label(outer, wraplength=560, justify="left",
                                  foreground="#7f8a99", text="")
        self.map_note.grid(row=4, column=0, sticky="we", pady=(2, 0))

        # The disclosure. One line, always visible, never behind a "details" toggle:
        # a viewer of a public issue can download an attachment the moment it
        # uploads, and it stays there even if the issue is never submitted.
        warn = ttk.Label(
            outer, wraplength=560, justify="left", foreground="#b06000",
            text=("Note: once you drag the file into a public issue it uploads "
                  "immediately and can be downloaded by anyone, even if you never "
                  "submit the issue."))
        warn.grid(row=5, column=0, sticky="we", pady=(8, 0))

        row = ttk.Frame(outer)
        row.grid(row=6, column=0, sticky="we", pady=(12, 0))
        self.open_btn = ttk.Button(row, text="Open folder", width=14,
                                   command=self._open_folder, state="disabled")
        self.open_btn.pack(side="left")
        self.read_btn = ttk.Button(row, text="Open report.md", width=16,
                                   command=self._open_report, state="disabled")
        self.read_btn.pack(side="left", padx=(8, 0))
        self.copy_btn = ttk.Button(row, text="Copy diagnostics", width=17,
                                   command=self._copy, state="disabled")
        self.copy_btn.pack(side="left", padx=(8, 0))

        self.go_btn = ttk.Button(row, text="Report with this file", width=20,
                                 command=self._report_with_file, state="disabled")
        self.go_btn.pack(side="right")
        self.plain_btn = ttk.Button(row, text="Report without it",
                                    command=self._report_plain, state="disabled")
        self.plain_btn.pack(side="right", padx=(0, 8))

        for widget, hint in (
            (self.open_btn, "Open the folder holding the diagnostics file, with the "
                            "file selected so you can drag it into your browser."),
            (self.read_btn, "Open the readable summary that goes at the top of your "
                            "report, so you can see exactly what is being sent."),
            (self.copy_btn, "Copy the summary to the clipboard, for a forum post, a "
                            "chat message or an email instead of a GitHub issue."),
            (self.go_btn,   "Open a pre-filled GitHub issue in your browser and show "
                            "the diagnostics file ready to drag in."),
            (self.plain_btn, "Open the pre-filled issue without the file. The "
                             "summary above is still included in the report."),
        ):
            Tooltip(widget, hint)

    def _centre_on(self, app):
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:
            pass

    # ── gathering (background thread) ────────────────────────────────────────

    def _post(self, fn, *args):
        """Hand a result back to the UI thread, tolerating a dialog that is already
        gone. Gathering takes about half a second, and closing the window inside
        that window is an ordinary thing for a user to do; `after` on a destroyed
        widget raises, and a traceback out of a daemon thread helps nobody."""
        try:
            self.after(0, fn, *args)
        except Exception:
            pass

    def _gather(self):
        """Build the report, write the zip and the private map. Off the UI thread:
        it reads the cache DB, several megabytes of log, and shells out to
        nvidia-smi for the card's VRAM."""
        try:
            name = diagnostics.report_name()
            report = diagnostics.build_report(
                CFG, app_root=APP_ROOT, collapse_re=COLLAPSE_PROCESSING_RE,
                zip_name=name, extra_logs=self._extra_logs)
            zip_path = diagnostics.write_zip(report, name=name)
            map_path = diagnostics.write_mapping(report)
            diagnostics.prune_reports()
        except Exception as exc:
            debug_log("diagnostics: report could not be built", exc, tb=True)
            self._post(self._gather_failed)
            return
        self._post(self._gather_done, report, zip_path, map_path)

    def _gather_failed(self):
        """A report that cannot be gathered must not block reporting the bug. The
        plain path still carries the app version, OS, Python and GPU name."""
        self._report = None
        self.status.configure(
            text="Diagnostics could not be collected. You can still report the "
                 "issue without them.", foreground="#c04040")
        self.plain_btn.configure(state="normal")
        self.plain_btn.focus_set()

    def _gather_done(self, report, zip_path, map_path):
        self._report, self._zip_path, self._map_path = report, zip_path, map_path
        try:
            size = os.path.getsize(zip_path) / 1024.0
        except OSError:
            size = 0.0
        self.status.configure(
            text="%s  (%.0f KB)" % (os.path.basename(zip_path), size),
            foreground="#2e7d32")

        lines = []
        for arcname, text in report.files:
            kb = len((text or "").encode("utf-8", "replace")) / 1024.0
            lines.append("%-34s %8.0f KB" % (arcname, kb))
        self.listing.configure(state="normal")
        self.listing.delete("1.0", "end")
        self.listing.insert("1.0", "\n".join(lines))
        self.listing.configure(state="disabled")

        # Says what happened to THIS file, without repeating the explanation the
        # paragraph at the top already gives. The description count leads: it is the
        # one people ask about, because it is their photos being described.
        bits = []
        if report.withheld:
            bits.append("%d line(s) holding the description of a photo, and the name "
                        "made from it, were removed." % report.withheld)
        if report.dropped:
            bits.append("%d line(s) were removed for naming a folder this app did "
                        "not recognise." % report.dropped)
        self.redaction_note.configure(
            text=" ".join(bits) or "Nothing had to be removed outright.")
        if map_path:
            self.map_note.configure(
                text=("A private list of what each code means was saved for you "
                      "alone, next to your logs, as %s. It is not inside the file "
                      "above and must not be attached to anything."
                      % os.path.basename(map_path)))

        for btn in (self.open_btn, self.read_btn, self.copy_btn,
                    self.go_btn, self.plain_btn):
            btn.configure(state="normal")
        self.go_btn.focus_set()

    # ── actions ──────────────────────────────────────────────────────────────

    def _report_with_file(self):
        """Browser first, then Explorer behind it. The body already names the file
        and asks for the drag, so the instruction is where the user is looking.

        NOT named `_report`: `self._report` is the gathered report, and an attribute
        that shadows a method is silent here in a way it is not elsewhere. `_build`
        runs before the gather thread finishes, so `command=self._report` read the
        `None` set in `__init__`, and tkinter accepts `command=None` without a word:
        the button drew normally, enabled itself normally and did nothing at all.
        `tests/test_gui_command_bindings.py` now fails on any such collision."""
        body = self._report.body if self._report else None
        report_issue(body=body)
        if self._zip_path:
            open_in_explorer(self._zip_path)
        self._close()

    def _report_plain(self):
        report_issue(body=self._report.body if self._report else None)
        self._close()

    def _open_folder(self):
        open_in_explorer(self._zip_path)

    def _open_report(self):
        """Write report.md beside the zip's own name and open it.

        It is written to the TEMP folder, not to ./issues: that folder holds
        redacted zips and nothing else, ever, because it is what the user drags
        from and a loose file beside the zip is an accident waiting to happen.
        """
        if not self._report:
            return
        try:
            path = os.path.join(tempfile.gettempdir(), "imgtbx-report-preview.md")
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self._report.full)
            os.startfile(path)
        except Exception as exc:
            debug_log("diagnostics: could not open the report preview", exc)

    def _copy(self):
        """The other half of the feature, and often the better one: no cap at all,
        and it works for a forum, a chat or an email that never becomes an issue."""
        if not self._report:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._report.full)
            self.status.configure(text="Diagnostics copied to the clipboard.",
                                  foreground="#2e7d32")
        except Exception as exc:
            debug_log("diagnostics: clipboard copy failed", exc)

    def _close(self):
        try:
            self.destroy()
        except Exception:
            pass


def open_issue_reporter(parent, extra_logs=()):
    """Open the review dialog. Fail-safe: if the dialog itself cannot be built, fall
    straight through to the plain pre-filled issue, because the one thing this
    feature must never do is stop somebody reporting a bug."""
    try:
        return DiagnosticsDialog(parent, extra_logs=extra_logs)
    except Exception as exc:
        debug_log("diagnostics: review dialog failed to open", exc, tb=True)
        report_issue()
        return None
