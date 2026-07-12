-- Atlas Server: starts the console/API with the in-process scheduler
-- (the daily cycle at 09:30 AEST and the nightly backup fire from inside
-- this process - no launchd, no terminal).
-- Double-click to start. macOS asks ONCE for permission to access
-- Documents - click Allow. Double-clicking again restarts cleanly.
do shell script "lsof -ti :8001 | xargs kill 2>/dev/null ; sleep 1 ; cd /Users/jayakrishnakaryampudi/Documents/atlas && set -a && . ./.env 2>/dev/null ; set +a ; (nohup env ATLAS_INPROC_SCHEDULER=1 ./.venv/bin/uvicorn atlas.api.main:app --port 8001 >> /tmp/atlas-api.log 2>&1 &) ; sleep 2"
display notification "Console: http://localhost:8001/console" with title "Atlas is running"
