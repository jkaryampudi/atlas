#!/bin/bash
# Restore a pg_dump from the old host into THIS box's postgres, then verify
# the audit chain end-to-end. This IS the restore drill: a migration that
# proves the backup format actually restores, or fails loudly trying.
#     ./ops/migrate_from_dump.sh ~/atlas-2026-07-12.sql.gz
# Refuses to overwrite a database that already contains audit events unless
# ATLAS_MIGRATE_FORCE=1 — a mistaken re-run must not vaporise a live chain.
set -euo pipefail
cd "$(dirname "$0")/.."
DUMP="${1:?usage: migrate_from_dump.sh <dump.sql.gz>}"
[ -f "$DUMP" ] || { echo "no such file: $DUMP"; exit 1; }

EXISTING=$(docker exec atlas-db-1 psql -U atlas -d atlas -tAc \
  "SELECT count(*) FROM audit.decision_events" 2>/dev/null || echo 0)
if [ "${EXISTING:-0}" -gt 0 ] && [ "${ATLAS_MIGRATE_FORCE:-0}" != "1" ]; then
  echo "REFUSING: this database already holds $EXISTING audit events."
  echo "Set ATLAS_MIGRATE_FORCE=1 only if you are certain this box's chain is disposable."
  exit 2
fi

echo "== dropping and recreating the atlas database"
docker exec atlas-db-1 psql -U atlas -d postgres \
  -c "DROP DATABASE IF EXISTS atlas WITH (FORCE)" -c "CREATE DATABASE atlas OWNER atlas"

echo "== restoring $DUMP"
gunzip -c "$DUMP" | docker exec -i atlas-db-1 psql -U atlas -d atlas -q

echo "== verifying the restored audit chain (the whole point)"
set -a; [ -f .env ] && source .env; set +a
export ATLAS_DATABASE_URL="${ATLAS_DATABASE_URL:-postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas}"
./.venv/bin/python -m atlas.tools.verify_chain
echo "== migration complete: the chain survived the move intact."
