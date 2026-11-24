#!/usr/bin/env bash
# Back up the database, settings and data files to backups/<timestamp>/ (safe while the app runs).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python manage.py backup "$@"
