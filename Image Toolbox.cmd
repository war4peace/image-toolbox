@echo off
rem Launches the Image Toolbox GUI without a console window.
start "Image Toolbox" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0toolbox_gui.py"
