#!/usr/bin/env bash
# Sourced — one-click launcher (macOS / Linux).
# Double-click on macOS (may need: right-click → Open the first time), or run
# ./start-mac-linux.command in a terminal. Leave the window open to keep the
# server running; close it or press Ctrl-C to stop.
cd "$(dirname "$0")" || exit 1

echo "Installing website dependencies (first run only)..."
python3 -m pip install -r requirements-web.txt --quiet || {
  echo "pip failed — try a Python 3.12/3.13 interpreter."; exit 1; }
python3 -m pip install httpx anthropic --quiet 2>/dev/null

echo
echo "Starting Sourced control tower at http://127.0.0.1:8000/console"
echo "Close this window (or Ctrl-C) to stop."
echo
python3 run_console.py
