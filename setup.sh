#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q
./.venv/bin/python -m playwright install chromium
echo "done. activate with: source .venv/bin/activate"
