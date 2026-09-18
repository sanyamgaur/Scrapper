@echo off
REM ==========================================================================
REM  Sourced - one-click launcher (Windows)
REM  Double-click this file. It installs what the website needs, then starts
REM  the control tower and opens it in your browser. No commands to type.
REM  Leave this window open while you present; close it to stop the server.
REM ==========================================================================
cd /d "%~dp0"

echo Installing website dependencies (first run only, ~1 min)...
python -m pip install -r requirements-web.txt --quiet
if errorlevel 1 (
  echo.
  echo pip failed. If this is a Python 3.14 wheel problem, install Python 3.12
  echo from python.org and run this again.
  pause
  exit /b 1
)

REM Optional extras: image caching and the LLM tail classifier. Ignore failures.
python -m pip install httpx anthropic --quiet 2>nul

echo.
echo Starting Sourced control tower...
echo   Control tower : http://127.0.0.1:8000/console
echo   Storefront    : http://127.0.0.1:8000/
echo Close this window to stop the server.
echo.
python run_console.py
pause
