#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

export HT_WEB_HOST="${HT_WEB_HOST:-127.0.0.1}"
export HT_WEB_PORT="${HT_WEB_PORT:-8765}"

exec ./venv/bin/python -u handingtime_web/server.py
