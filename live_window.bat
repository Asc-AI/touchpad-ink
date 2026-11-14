@echo off
setlocal
cd /d "%~dp0"
title Touchpad Motion Window
python -u tools\live_window.py
if errorlevel 1 (
  echo.
  echo Startup failed. Mouse capture is not active.
  pause
)
endlocal
