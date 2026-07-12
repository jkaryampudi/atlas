#!/bin/bash
# Provision a Linux host (Ubuntu/Debian) as the Atlas production box.
# Idempotent: safe to re-run. Run from the repo root as your normal user:
#     ./ops/provision.sh
# Prereqs it checks but does not install: docker (with compose), python3.12+.
# See docs/ops/deploy-local.md for the full cutover walkthrough.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$REPO"

echo "== atlas provision: repo=$REPO user=$USER_NAME"

command -v docker >/dev/null || { echo "docker missing — install docker first"; exit 1; }
PY=$(command -v python3.12 || command -v python3.13 || command -v python3 || true)
[ -n "$PY" ] || { echo "python3.12+ missing"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' \
  || { echo "python >= 3.12 required, found $($PY --version)"; exit 1; }
[ -f .env ] || { echo ".env missing — copy it from the Mac (it is never in git)"; exit 1; }

echo "== venv + install"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q -e ".[dev]"

echo "== database containers"
docker compose up -d db redis

echo "== waiting for postgres"
for i in $(seq 1 30); do
  docker exec atlas-db-1 pg_isready -U atlas >/dev/null 2>&1 && break
  sleep 1
done

echo "== migrations"
set -a; source .env; set +a
export ATLAS_DATABASE_URL="${ATLAS_DATABASE_URL:-postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas}"
./.venv/bin/alembic upgrade head

echo "== systemd units"
for t in ops/systemd/*.tmpl; do
  out="/etc/systemd/system/$(basename "${t%.tmpl}")"
  sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$USER_NAME|g" "$t" | sudo tee "$out" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now atlas-api.service atlas-daily.timer atlas-backup.timer

echo "== verify"
sleep 3
curl -sf http://127.0.0.1:8001/v1/system/health >/dev/null \
  && echo "API: ok (127.0.0.1:8001 — reach it via Tailscale/SSH tunnel)" \
  || { echo "API failed — journalctl -u atlas-api"; exit 1; }
systemctl list-timers 'atlas-*' --no-pager | sed -n '1,4p'
echo "== done. Next: ops/migrate_from_dump.sh <dump.sql.gz> to bring the chain over."
