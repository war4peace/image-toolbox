"""
gui/video_player.py
-------------------
Shared libVLC-backed video player widget (section 16.2), used by BOTH the segment
picker (section 16.4) and the reworked comparison window (section 16.3). It is the
one place the app plays video with audio.

Dependency-light contract: `python-vlc` + libVLC are **bootstrap-downloaded** (both
install modes), so they are ABSENT on a fresh box or an old install. The import is
therefore guarded — this module always imports (the GUI must still launch), and
`VLC_AVAILABLE` tells a consumer whether real playback is possible. When it is
False, the picker falls back to a silent ffmpeg frame-scrub and the comparison
window keeps its decode-on-seek wipe, so the feature degrades, never breaks.

Everything VLC-specific is wrapped in try/except (a driver/codec hiccup must never
crash the GUI). The pure time<->frame + keyframe-nav helpers at the bottom carry no
tkinter/VLC dependency and are unit-tested.
"""

import os
import bisect
import tkinter as tk
from tkinter import ttk


# ── guarded libVLC load ──────────────────────────────────────────────────────

VLC_AVAILABLE = False
_vlc = None
_VLC_LOAD_ERROR = None
_LOAD_TRIED = False


def _bundled_vlc_dir():
    """<APP_ROOT>/vlc, where bootstrap.ps1 unpacks libvlc.dll + libvlccore.dll +
    the plugins/ dir. Anchored off APP_ROOT (never the cwd)."""
    try:
        from gui.common import APP_ROOT
    except Exception:
        return None
    return os.path.join(APP_ROOT, "vlc")


def load_vlc():
    """Import python-vlc against the bundled libVLC (once). Returns VLC_AVAILABLE.
    Safe to call repeatedly and from any consumer; fail-safe (never raises)."""
    global VLC_AVAILABLE, _vlc, _VLC_LOAD_ERROR, _LOAD_TRIED
    if _LOAD_TRIED:
        return VLC_AVAILABLE
    _LOAD_TRIED = True
    try:
        d = _bundled_vlc_dir()
        if d and os.path.isdir(d):
            # Point python-vlc at the bundled libVLC + its plugins so we never
            # depend on a system-wide VLC install.
            os.environ.setdefault("PYTHON_VLC_MODULE_PATH", d)
            dll = os.path.join(d, "libvlc.dll")
            if os.path.isfile(dll):
                os.environ.setdefault("PYTHON_VLC_LIB_PATH", dll)
            os.environ.setdefault("VLC_PLUGIN_PATH", os.path.join(d, "plugins"))
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass
        import vlc as _mod          # noqa: WPS433 (guarded, optional dependency)
        _vlc = _mod
        VLC_AVAILABLE = True
    except Exception as exc:         # noqa: BLE001 — optional dependency; fall back
        _VLC_LOAD_ERROR = exc
        VLC_AVAILABLE = False
    return VLC_AVAILABLE


def reload_vlc():
    """Re-attempt the guarded import (after an in-app install). Clears the one-shot
    cache so a fresh `import vlc` runs against the just-downloaded libVLC."""
    global _LOAD_TRIED, _VLC_LOAD_ERROR, VLC_AVAILABLE
    _LOAD_TRIED = False
    _VLC_LOAD_ERROR = None
    VLC_AVAILABLE = False
    return load_vlc()


def vlc_error():
    """The exception (if any) that prevented libVLC from loading, for a UI hint."""
    return _VLC_LOAD_ERROR


# ── GPU-overlay hook detection (RivaTuner / MSI Afterburner) ──────────────────
# RivaTuner Statistics Server injects an overlay hook DLL into processes it thinks
# are rendering and crashes libVLC with a native access violation (0xc0000005 in an
# "unknown"/injected module) at a random point during playback. This is an EXTERNAL
# incompatibility, not an app bug, and it's uncatchable from Python. We can at least
# DETECT the injected hook and warn, so a user doesn't lose hours to a mystery crash.
_OVERLAY_HOOK_DLLS = {
    "RTSSHooks64.dll": "RivaTuner Statistics Server (MSI Afterburner)",
    "RTSSHooks.dll":   "RivaTuner Statistics Server (MSI Afterburner)",
}
_overlay_warned = False


def overlay_hook_present():
    """Name of a known GPU-overlay tool whose hook DLL is injected into THIS process,
    or None. Detected via GetModuleHandleW (the DLL is loaded in our address space
    only when the overlay has hooked us). Fail-safe: None on non-Windows / any error."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        for dll, name in _OVERLAY_HOOK_DLLS.items():
            if k32.GetModuleHandleW(dll):
                return name
    except Exception:                                # noqa: BLE001
        return None
    return None


def warn_overlay_once(parent):
    """Once per session, if a GPU-overlay hook (RTSS/Afterburner) is injected, warn
    that it can crash the video player and how to avoid it. Fail-safe."""
    global _overlay_warned
    if _overlay_warned:
        return
    name = overlay_hook_present()
    if not name:
        return
    _overlay_warned = True
    try:
        from tkinter import messagebox
        from gui.common import APP_TITLE
        messagebox.showwarning(
            APP_TITLE,
            f"{name} is running and its overlay is injected into this app.\n\n"
            "That overlay is known to crash the in-app video player (a driver-level "
            "access violation that Windows reports against pythonw.exe). If the "
            "player crashes, close it or add pythonw.exe to its exclusion / "
            "\"Application detection level = None\" list, then try again.\n\n"
            "The frame comparison (Compare frames) is unaffected.")
    except Exception:                                # noqa: BLE001
        pass


def prompt_install_libvlc(parent, on_done=None):
    """Offer to download + install libVLC (+ python-vlc) in-app, for an install made
    before bootstrap fetched it (section 16.2). If already available, calls
    on_done(True) at once. Otherwise asks, runs the install off the UI thread behind a
    small progress dialog, reloads libVLC, and calls on_done(bool available). Fail-safe."""
    import threading
    import tkinter as tk
    from tkinter import ttk, messagebox
    from gui.common import APP_TITLE

    if load_vlc():
        if on_done:
            on_done(True)
        return
    if not messagebox.askyesno(
            APP_TITLE,
            "In-app video playback needs libVLC (a ~40 MB download) plus the "
            "python-vlc package.\n\nDownload and install it now?"):
        if on_done:
            on_done(False)
        return

    dlg = tk.Toplevel(parent)
    dlg.title("Installing libVLC")
    dlg.transient(parent)
    dlg.resizable(False, False)
    ttk.Label(dlg, text="Downloading and installing libVLC…",
              padding=(14, 12, 14, 4)).pack()
    pb = ttk.Progressbar(dlg, mode="indeterminate", length=300)
    pb.pack(padx=14, pady=(0, 6))
    pb.start(12)
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
        except tk.TclError:
            pass

    def _done(ok, msg):
        try:
            pb.stop()
            dlg.grab_release()
            dlg.destroy()
        except tk.TclError:
            pass
        available = reload_vlc() if ok else False
        if not ok:
            messagebox.showerror(APP_TITLE, f"Could not install libVLC:\n{msg}\n\n"
                                            "You can still scrub frame by frame.")
        elif not available:
            messagebox.showwarning(
                APP_TITLE, "libVLC was installed but could not be loaded. A restart "
                           "of the app may be needed.")
        if on_done:
            on_done(bool(available))

    def work():
        import vlc_setup
        ok, msg = vlc_setup.install(progress=lambda m: parent.after(0, _set, m))
        parent.after(0, _done, ok, msg)

    threading.Thread(target=work, daemon=True).start()


# ── the widget ───────────────────────────────────────────────────────────────

class VideoPlayer(ttk.Frame):
    """A tkinter video surface driven by libVLC. Create it, pack/grid it, then call
    `load(path)`; drive it with play/pause/seek/step. Times in the public API are
    **seconds**. `on_time(seconds)` (optional) is called ~10x/s while playing and
    once after a seek, so a picker can track the playhead. Windows-only embedding
    (set_hwnd); on any failure `ok` is False and the caller should fall back."""

    POLL_MS = 90

    def __init__(self, master, on_time=None, on_end=None, fps=30.0):
        super().__init__(master)
        self._on_time = on_time
        self._on_end = on_end
        self.fps = float(fps) or 30.0
        self.ok = False
        self._player = None
        self._instance = None
        self._length = 0.0            # seconds (0 until known)
        self._poll_job = None
        self._path = None

        # The actual video output surface: a black frame libVLC renders into.
        self.surface = tk.Frame(self, background="black", highlightthickness=0)
        self.surface.pack(fill="both", expand=True)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _ensure_player(self):
        if self._player is not None:
            return self.ok
        if not load_vlc():
            self.ok = False
            return False
        try:
            # Use a PURE-SOFTWARE pipeline (GDI video output + software decode). Any
            # GPU video output (Direct3D 11 -> dxgi.dll, or Direct3D 9 -> the driver)
            # crashes with a native access violation (0xc0000005) across the embedded-
            # HWND resize/pause lifecycle: the swapchain is recreated on resize and a
            # following state change (pause) faults in it. This is a C-level segfault
            # Python cannot catch, so it kills the whole app with no log. The `wingdi`
            # vout has NO swapchain (it blits frames to the window DC), so resize is
            # just a new blit rect -- nothing to mismanage. `--avcodec-hw=none` keeps
            # decode off the GPU too (no DXVA2/D3D11VA surfaces). Software is plenty
            # for the modest clips this window compares; stability wins here. See the
            # crash note in this module's docstring / docs/video-upscaler.md 16.3.
            self._instance = _vlc.Instance(
                "--intf", "dummy", "--no-video-title-show", "--quiet",
                "--vout=wingdi", "--avcodec-hw=none")
            self._player = self._instance.media_player_new()
            self.update_idletasks()                 # realise the surface -> valid winfo_id
            handle = self.surface.winfo_id()
            if os.name == "nt":
                self._player.set_hwnd(handle)
            else:                                   # not shipped, but keep it correct
                self._player.set_xwindow(handle)
            self.ok = True
            warn_overlay_once(self)                 # RTSS/overlay-hook crash warning
        except Exception:                            # noqa: BLE001
            self.ok = False
            self._player = None
        return self.ok

    def load(self, path):
        """Point the player at `path` (paused at 0). Returns True on success."""
        if not self._ensure_player():
            return False
        try:
            media = self._instance.media_new(path)
            self._player.set_media(media)
            self._path = path
            self._length = 0.0
            # Play->pause primes the pipeline so the first frame shows and length
            # becomes known, without actually starting playback for the user.
            self._player.play()
            self._player.set_pause(1)
            self._player.set_time(0)
            self._start_poll()
            return True
        except Exception:                            # noqa: BLE001
            return False

    def close(self):
        """Tear the player down in the order that avoids the close-while-playing
        crash: stop playback, DETACH the HWND (set_hwnd(0)) so libVLC's vout stops
        drawing into a window that is about to be destroyed, drop the media, then
        release. Destroying the Tk surface before this detaches is what faulted
        (a native access violation from the vout thread). Callers should also defer
        the window destroy() a tick (see the windows' _close) to let the native
        threads unwind."""
        self._stop_poll()
        p = self._player
        if p is not None:
            for step in (
                    lambda: p.stop(),
                    lambda: (p.set_hwnd(0) if os.name == "nt" else p.set_xwindow(0)),
                    lambda: p.set_media(None),
                    lambda: p.release()):
                try:
                    step()
                except Exception:                    # noqa: BLE001
                    pass
        self._player = None
        try:
            if self._instance is not None:
                self._instance.release()
        except Exception:                            # noqa: BLE001
            pass
        self._instance = None
        self.ok = False

    # ── transport ────────────────────────────────────────────────────────────

    def play(self):
        if self._player is not None:
            try:
                self._player.play()
            except Exception:                        # noqa: BLE001
                pass

    def pause(self):
        if self._player is not None:
            try:
                self._player.set_pause(1)
            except Exception:                        # noqa: BLE001
                pass

    def toggle(self):
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def is_playing(self):
        try:
            return bool(self._player and self._player.is_playing())
        except Exception:                            # noqa: BLE001
            return False

    def is_ended(self):
        """True once the media reached its end. In this state libVLC will NOT play()
        or seek() again until the media is reset (see restart), so callers must
        detect it rather than silently no-op."""
        try:
            return bool(self._player is not None and _vlc is not None
                        and self._player.get_state() == _vlc.State.Ended)
        except Exception:                            # noqa: BLE001
            return False

    def restart(self):
        """Re-prime to a paused frame-0 state. Reloads a FRESH media (load), which is
        the reliable way to revive a player out of the Ended/Stopped state — play()
        alone does not restart an ended media in libVLC 3.x."""
        if self._path:
            self.load(self._path)

    def seek(self, seconds):
        """Seek to `seconds` (clamped to [0, length])."""
        if self._player is None:
            return
        t = max(0.0, float(seconds))
        if self._length:
            t = min(t, self._length)
        try:
            self._player.set_time(int(t * 1000))
        except Exception:                            # noqa: BLE001
            return
        if self._on_time:
            self._on_time(t)

    def step(self, frames):
        """Step by whole frames. Forward one uses libVLC's frame-accurate
        next_frame(); other deltas seek by frame duration off the current time."""
        if self._player is None:
            return
        if frames == 1:
            try:
                self._player.next_frame()
                if self._on_time:
                    self._on_time(self.get_time())
                return
            except Exception:                        # noqa: BLE001
                pass
        self.seek(self.get_time() + frames / self.fps)

    def get_time(self):
        """Current playhead in seconds (0.0 on failure)."""
        try:
            ms = self._player.get_time() if self._player else 0
            return max(0.0, (ms or 0) / 1000.0)
        except Exception:                            # noqa: BLE001
            return 0.0

    def get_length(self):
        """Media length in seconds (0.0 until known)."""
        try:
            ms = self._player.get_length() if self._player else 0
            if ms and ms > 0:
                self._length = ms / 1000.0
        except Exception:                            # noqa: BLE001
            pass
        return self._length

    def set_rate(self, rate):
        if self._player is not None:
            try:
                self._player.set_rate(float(rate))
            except Exception:                        # noqa: BLE001
                pass

    def set_volume(self, vol):
        if self._player is not None:
            try:
                self._player.audio_set_volume(int(max(0, min(100, vol))))
            except Exception:                        # noqa: BLE001
                pass

    def set_mute(self, muted):
        if self._player is not None:
            try:
                self._player.audio_set_mute(bool(muted))
            except Exception:                        # noqa: BLE001
                pass

    # ── time polling ─────────────────────────────────────────────────────────

    def _start_poll(self):
        if self._poll_job is None:
            self._poll_job = self.after(self.POLL_MS, self._poll)

    def _stop_poll(self):
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:                        # noqa: BLE001
                pass
            self._poll_job = None

    def _poll(self):
        self._poll_job = None
        if self._player is None:
            return
        self.get_length()                            # refresh length once it's known
        if self.is_playing() and self._on_time:
            self._on_time(self.get_time())
        # libVLC reports "ended" via state; surface it so a picker can reset to start.
        try:
            if self._on_end and _vlc and self._player.get_state() == _vlc.State.Ended:
                self._on_end()
        except Exception:                            # noqa: BLE001
            pass
        self._start_poll()


# ── pure helpers (unit-tested; no tkinter / VLC) ─────────────────────────────

def time_to_frame(t, fps):
    """Frame index (0-based) at time `t` seconds for a `fps` stream."""
    if not fps or fps <= 0:
        return 0
    return int(round(max(0.0, float(t)) * float(fps)))


def frame_to_time(frame, fps):
    """Start time (seconds) of frame index `frame`."""
    if not fps or fps <= 0:
        return 0.0
    return max(0, int(frame)) / float(fps)


def nearest_keyframe(times, t, direction, eps=1e-3):
    """The previous (direction < 0) or next (direction > 0) keyframe time relative
    to `t`, or None if there isn't one that way. `times` must be sorted ascending.
    `eps` keeps a keyframe exactly at `t` from being returned as its own neighbour."""
    if not times:
        return None
    if direction > 0:
        i = bisect.bisect_right(times, t + eps)
        return times[i] if i < len(times) else None
    i = bisect.bisect_left(times, t - eps)
    return times[i - 1] if i > 0 else None
