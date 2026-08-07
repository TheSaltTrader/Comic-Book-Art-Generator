@echo off
REM Comic Book Art Creator - first-time setup (engine + models, ~36 GB).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
