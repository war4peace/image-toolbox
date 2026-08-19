@echo off
rem Image Toolbox launcher.
rem First launch: runs bootstrap.ps1 (one-time component download/setup).
rem After that:   checks the environment still RUNS, then starts the GUI without
rem               a console window.
rem
rem That check is not paranoia, it is defect D1. A venv is not self-contained:
rem .venv\Scripts\python.exe is a stub that reads .venv\pyvenv.cfg and executes the
rem base Python named there. Uninstalling or moving that base Python (for some
rem unrelated tool) leaves every file in this folder looking perfectly healthy while
rem the stub exits 103 - and under pythonw.exe there is no console for it to say so
rem in, so the app "does not start" with no window, no error and no crash log,
rem because none of our code ever runs. Asking it to import sys costs about a tenth
rem of a second and turns that into a repair. Do NOT replace it with an existence
rem check: the file is PRESENT in exactly the case that breaks.
if not exist "%~dp0.setup_complete" goto bootstrap
"%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo The Python environment needs repairing - running setup ...
    goto bootstrap
)
start "Image Toolbox" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\toolbox_gui.py"
goto :eof

:bootstrap
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
