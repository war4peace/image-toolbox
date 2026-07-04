"""
toolbox_gui.py
--------------
Entry point for the Image Toolbox GUI. The GUI itself now lives in the gui/
package (split out of this former ~8k-line module in 0.4.3). This thin shim keeps
the historical launch path working (Image Toolbox.cmd, bootstrap.ps1 and the
installer all run scripts\toolbox_gui.py) and, crucially, arms crash logging
BEFORE importing the package, so even an import-time failure (a module the
installer forgot to ship) still writes a crash log and shows a dialog instead of
a silent split-second window.

Run (no console window):
    .venv\Scripts\pythonw.exe scripts\toolbox_gui.py
or double-click "Image Toolbox.cmd".
"""

import os
import sys

# Arm crash logging before importing the gui package (see module docstring). The
# try/except means a missing crash_logger.py can't itself reintroduce a silent
# crash.
try:
    import crash_logger
    crash_logger.install()
except Exception:
    crash_logger = None
    import traceback as _traceback
    import datetime as _datetime

    def _emergency_excepthook(exc_type, exc, tb):
        try:
            _d = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            os.makedirs(_d, exist_ok=True)
            _p = os.path.join(
                _d, "crash_" + _datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            with open(_p, "w", encoding="utf-8") as _f:
                _traceback.print_exception(exc_type, exc, tb, file=_f)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _emergency_excepthook

# Importing the package pulls in every gui.* module (and the app feature modules)
# - the point past which crash_logger is guaranteed armed.
from gui.app import App, main
from gui.common import (
    APP_VERSION, APP_TITLE, GUI_MARKER, CFG, funds_color, fmt_funds,
    config_funds_floor, report_issue, save_config, load_settings, save_settings,
)
from gui.tooltab import ToolTab

if crash_logger:
    crash_logger.set_version(APP_VERSION)

# Public API preserved for `import toolbox_gui` callers (and the test suite).
__all__ = ["App", "main", "APP_VERSION", "APP_TITLE", "GUI_MARKER", "ToolTab",
           "CFG", "funds_color", "fmt_funds", "config_funds_floor",
           "report_issue", "save_config", "load_settings", "save_settings"]


if __name__ == "__main__":
    main()
