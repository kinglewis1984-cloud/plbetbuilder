@echo off
REM Regenerate the weekend coupon dashboard for the upcoming Fri-Mon.
cd /d "%~dp0"
python build.py
echo.
echo Done. Open dashboard.html
