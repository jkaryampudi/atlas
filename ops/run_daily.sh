#!/bin/zsh
# Nightly T0–T9 pipeline (fired by launchd at 09:30 AEST = 23:30 UTC — after
# the US close AND yesterday's ASX close, when EODHD end-of-day data exists).
# Exit code is the ground truth: launchd logs it, the pipeline alerts on its
# own failures, and this wrapper alerts if the process died before it could.
set -uo pipefail
cd /Users/jayakrishnakaryampudi/Documents/atlas
[ -f .env ] && set -a && source .env && set +a
export ATLAS_DATABASE_URL="${ATLAS_DATABASE_URL:-postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas}"

./.venv/bin/python -m atlas.ops.daily
rc=$?
# exit 3 = pre-session guard refusal (polite, expected on early manual runs) — not a failure
if [ $rc -ne 0 ] && [ $rc -ne 3 ]; then
  ./.venv/bin/python - <<PY
from atlas.ops.alerts import notify
notify("Atlas daily pipeline FAILED", "exit code ${rc} — see ~/Library/Logs/atlas-daily.log", priority="high")
PY
fi
exit $rc
