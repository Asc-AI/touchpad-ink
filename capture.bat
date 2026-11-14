@echo off
cd /d "%~dp0"
python -u tools\ptp_capture.py --delay 5 --seconds 30
echo.
pause
