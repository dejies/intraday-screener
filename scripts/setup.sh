#!/usr/bin/env bash
# One-time setup: virtual environment, dependencies, a .env template, then a health check.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $($PY --version)"
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example: add your Dhan client id and access token (or use --demo)."
fi
.venv/bin/python manage.py check || true
echo
echo "Next: scripts/demo.sh (no account needed) or scripts/start.sh (your Dhan data)."
