"""
Windows taskbar progress bar (0.3.0).

Paints a progress bar onto the app's own taskbar button — the same fill Explorer
shows while copying files — so a long upscale/tag run is glanceable from the
taskbar without alt-tabbing back to the window. It pairs with the taskbar flash
(``App.flash_attention``): green fill while running, **red** when the performance
watchdog reports a degradation episode.

Driven straight from ``ctypes`` against the shell ``ITaskbarList3`` COM interface
so NO new dependency is added (no comtypes / pywin32), in keeping with the
dependency-light telemetry/crash-logger modules. Windows-only and **fail-safe**:
every COM call is guarded so a missing API, an STA hiccup or a non-Windows host
can never break the GUI — on any failure the instance disables itself and all
further calls are no-ops.

All calls must come from the UI thread (COM is initialised on whatever thread
constructs the object, and the GUI owns that thread).

    tb = TaskbarProgress(hwnd)        # hwnd = the top-level window handle
    tb.set_progress(done, total)      # green fill, done/total
    tb.set_state("indeterminate")     # marquee (e.g. during the initial scan)
    tb.set_state("error")             # red (watchdog degradation)
    tb.clear()                        # remove the bar (idle / finished)
"""

import ctypes
from ctypes import wintypes

# TBPFLAG — taskbar progress-bar states.
TBPF_NOPROGRESS    = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL        = 0x2
TBPF_ERROR         = 0x4
TBPF_PAUSED        = 0x8

_STATE_FLAGS = {
    "none":          TBPF_NOPROGRESS,
    "normal":        TBPF_NORMAL,
    "indeterminate": TBPF_INDETERMINATE,
    "error":         TBPF_ERROR,
    "paused":        TBPF_PAUSED,
}

# Shell CLSID/IID for the taskbar list object and the v3 interface.
_CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITaskbarList3 = "{ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf}"

_CLSCTX_INPROC_SERVER = 0x1
_S_OK = 0

# ITaskbarList3 vtable slots (IUnknown 0-2, ITaskbarList 3-7, ITaskbarList2 8,
# ITaskbarList3 9+). We only need HrInit, SetProgressValue and SetProgressState.
_SLOT_HRINIT             = 3
_SLOT_SET_PROGRESS_VALUE = 9
_SLOT_SET_PROGRESS_STATE = 10


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    g = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


class TaskbarProgress:
    """Thin, fail-safe wrapper over ITaskbarList3 for one top-level window."""

    def __init__(self, hwnd):
        self.hwnd     = wintypes.HWND(hwnd)
        self._ptr     = None
        self._ok      = False
        self._errored = False
        try:
            self._init()
        except Exception:
            self._ok = False

    # ── COM plumbing ─────────────────────────────────────────────────────────

    def _init(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        clsid = _guid(_CLSID_TaskbarList)
        iid   = _guid(_IID_ITaskbarList3)
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None,
                                    _CLSCTX_INPROC_SERVER, ctypes.byref(iid),
                                    ctypes.byref(ptr))
        if hr != _S_OK or not ptr:
            return
        self._ptr = ptr
        # ITaskbarList::HrInit must be called before any other method.
        if self._method(_SLOT_HRINIT, ctypes.HRESULT)(self._ptr) != _S_OK:
            self._ptr = None
            return
        self._ok = True

    def _method(self, slot, restype, *argtypes):
        """Build a callable for vtable entry `slot`. The COM 'this' pointer is
        the implicit first argument."""
        vtable   = ctypes.cast(self._ptr, ctypes.POINTER(ctypes.c_void_p)).contents
        func_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
        proto    = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(func_ptr)

    # ── Public API (all no-ops once disabled) ────────────────────────────────

    def set_progress(self, done, total):
        """Green determinate fill at done/total. Ignored while in the error
        state so a late progress tick can't overwrite the red warning."""
        if not self._ok or self._errored:
            return
        try:
            total = int(total)
            if total <= 0:
                return
            done = max(0, min(int(done), total))
            self._method(_SLOT_SET_PROGRESS_STATE, ctypes.HRESULT,
                         wintypes.HWND, ctypes.c_int)(
                self._ptr, self.hwnd, TBPF_NORMAL)
            self._method(_SLOT_SET_PROGRESS_VALUE, ctypes.HRESULT,
                         wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(
                self._ptr, self.hwnd,
                ctypes.c_ulonglong(done), ctypes.c_ulonglong(total))
        except Exception:
            self._ok = False

    def set_state(self, state):
        """Set a bar state: 'normal', 'indeterminate', 'error', 'paused' or
        'none' (clears it)."""
        if not self._ok:
            return
        flag = _STATE_FLAGS.get(state, TBPF_NORMAL)
        self._errored = (state == "error")
        try:
            self._method(_SLOT_SET_PROGRESS_STATE, ctypes.HRESULT,
                         wintypes.HWND, ctypes.c_int)(
                self._ptr, self.hwnd, flag)
        except Exception:
            self._ok = False

    def clear(self):
        """Remove the progress bar from the taskbar button (idle/finished)."""
        self._errored = False
        self.set_state("none")
