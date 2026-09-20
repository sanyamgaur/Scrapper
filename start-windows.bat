@echo off
cd /d "%~dp0"
echo Installing dependencies (first run only)...
python -m pip install -r requirements-web.txt
echo.
echo Starting Sourced at http://127.0.0.1:8000/console
echo Close this window to stop.
echo.
python run_console.py
pause
