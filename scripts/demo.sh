#!/usr/bin/env bash
# Simulated market, no account needed. Extra arguments go to app.py, e.g. scripts/demo.sh --demo-start 09:25
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first."; exit 1; }
exec .venv/bin/python app.py --demo "$@"
