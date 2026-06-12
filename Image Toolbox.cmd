@echo off
rem Image Toolbox launcher.
rem First launch: runs bootstrap.ps1 (one-time component download/setup).
rem After that:   starts the GUI directly, without a console window.
if exist "%~dp0.setup_complete" (
    start "Image Toolbox" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0toolbox_gui.py"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
)
