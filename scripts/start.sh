#!/usr/bin/env bash
# Start the dashboard on your Dhan data. Extra arguments go to app.py, e.g.:
#   scripts/start.sh --universe nse        scripts/start.sh --watchlist nifty50.txt --no-browser
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first."; exit 1; }
.venv/bin/python manage.py check --quick || echo "(check reported problems above; starting anyway)"
exec .venv/bin/python app.py "$@"
