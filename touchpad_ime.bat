@echo off
setlocal
cd /d "%~dp0"
title Touchpad IME Overlay
echo Touchpad handwriting overlay is running.
echo   Hold Alt     show the pad at the caret and start writing
echo   Release Alt  stop and recognize; click a candidate or press 1-5
echo   Alt again    without a choice, clear the ink and rewrite
echo   Esc          cancel
echo   Ctrl+F8      quit (Ctrl+Shift+F8 if Ctrl+F8 is taken)
echo Keep this window open (it can stay behind other apps).
python -u tools\ime_window.py
if errorlevel 1 (
  echo.
  echo Startup failed. No mouse capture is active.
  pause
)
endlocal
