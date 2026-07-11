#!/bin/zsh
# API server under launchd supervision (KeepAlive) — replaces nohup, which the
# background-task lifecycle has already killed twice on this machine.
set -euo pipefail
cd /Users/jayakrishnakaryampudi/Documents/atlas
[ -f .env ] && set -a && source .env && set +a
export ATLAS_DATABASE_URL="${ATLAS_DATABASE_URL:-postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas}"
exec ./.venv/bin/uvicorn atlas.api.main:app --port 8001
