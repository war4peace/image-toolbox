"""
gui/ - the Image Toolbox tkinter front-end, split out of the former single
8k-line toolbox_gui.py (recommendations item 6).

Layout (built bottom-up so imports never cycle):
  common.py      - constants + config/settings/funds/mqtt/ollama helpers (no
                   sibling-gui imports; imported by everything else)
  widgets.py     - generic widgets (Tooltip, ProgressBar, TelemetryRow, LogPane,
                   ConsoleBuffer, LogViewer) + small text/format helpers
  comparison.py  - the floating before/after windows (ComparisonWindow,
                   VideoComparisonWindow)
  ... (further tabs/app extractions land here in later stages)

`scripts/toolbox_gui.py` stays as the entry point and re-exports the public
names, so `Image Toolbox.cmd`, the installer, and existing importers keep working.
"""
