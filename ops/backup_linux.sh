#!/bin/bash
# Nightly pg_dump on the Linux host (systemd: atlas-backup.timer). The audit
# chain is irreplaceable — losing the DB loses the fund's provenance.
# Local dir + optional off-box copy via rclone when ATLAS_BACKUP_REMOTE is set
# (e.g. "b2:atlas-backups" or "s3:my-bucket/atlas") — a backup on the same
# disk does not survive the disk.
set -uo pipefail
cd "$(dirname "$0")/.."
DEST="${ATLAS_BACKUP_DIR:-$HOME/AtlasBackups}"
mkdir -p "$DEST"
STAMP=$(date +%F)
FILE="$DEST/atlas-$STAMP.sql.gz"

fail() {
  ./.venv/bin/python -c "from atlas.ops.alerts import notify; notify('Atlas backup FAILED', '$1', priority='high')"
  exit 2
}

docker exec atlas-db-1 pg_dump -U atlas atlas | gzip > "$FILE" || fail "pg_dump exited non-zero"
size=$(stat -c%s "$FILE")
[ "$size" -lt 10240 ] && fail "dump is only ${size} bytes — suspect"
find "$DEST" -name 'atlas-*.sql.gz' -mtime +30 -delete

if [ -n "${ATLAS_BACKUP_REMOTE:-}" ]; then
  command -v rclone >/dev/null || fail "ATLAS_BACKUP_REMOTE set but rclone not installed"
  rclone copy "$FILE" "$ATLAS_BACKUP_REMOTE/" || fail "rclone copy to $ATLAS_BACKUP_REMOTE failed"
fi
exit 0
