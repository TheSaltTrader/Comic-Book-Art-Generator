@echo off
REM Comic Book Art Creator - double-click to launch.
REM The app starts the local engine automatically (headless) if it
REM is not already running, so this is the only thing you need to run.
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0app\comic_art_creator.py"
