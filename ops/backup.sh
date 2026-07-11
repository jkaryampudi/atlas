#!/bin/zsh
# Nightly pg_dump of the atlas database. The audit chain is irreplaceable by
# construction (append-only hash chain) — losing the DB loses the fund's
# entire provenance. Dumps land in ~/AtlasBackups (sync that directory to
# iCloud/Drive/another machine — an on-disk backup does not survive the disk).
set -uo pipefail
cd /Users/jayakrishnakaryampudi/Documents/atlas
[ -f .env ] && set -a && source .env && set +a
DEST="${ATLAS_BACKUP_DIR:-$HOME/AtlasBackups}"
mkdir -p "$DEST"
STAMP=$(date +%F)

if docker exec atlas-db-1 pg_dump -U atlas atlas | gzip > "$DEST/atlas-$STAMP.sql.gz"; then
  # keep 30 days; a dump that is suspiciously tiny is a failure, not a backup
  find "$DEST" -name 'atlas-*.sql.gz' -mtime +30 -delete
  size=$(stat -f%z "$DEST/atlas-$STAMP.sql.gz")
  if [ "$size" -lt 10240 ]; then
    ./.venv/bin/python -c "from atlas.ops.alerts import notify; notify('Atlas backup SUSPECT', 'dump is only ${size} bytes', priority='high')"
    exit 2
  fi
  exit 0
else
  ./.venv/bin/python -c "from atlas.ops.alerts import notify; notify('Atlas backup FAILED', 'pg_dump exited non-zero', priority='high')"
  exit 2
fi
