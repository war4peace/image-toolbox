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


def vlc_error():
    """The exception (if any) that prevented libVLC from loading, for a UI hint."""
    return _VLC_LOAD_ERROR


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
            self._instance = _vlc.Instance(
                "--intf", "dummy", "--no-video-title-show", "--quiet")
            self._player = self._instance.media_player_new()
            self.update_idletasks()                 # realise the surface -> valid winfo_id
            handle = self.surface.winfo_id()
            if os.name == "nt":
                self._player.set_hwnd(handle)
            else:                                   # not shipped, but keep it correct
                self._player.set_xwindow(handle)
            self.ok = True
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
        self._stop_poll()
        try:
            if self._player is not None:
                self._player.stop()
                self._player.release()
        except Exception:                            # noqa: BLE001
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
