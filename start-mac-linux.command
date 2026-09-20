#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
echo "Installing dependencies (first run only)..."
python3 -m pip install -r requirements-web.txt || { echo "pip failed — try Python 3.12/3.13"; exit 1; }
echo
echo "Starting Sourced at http://127.0.0.1:8000/console  (Ctrl+C to stop)"
echo
python3 run_console.py
