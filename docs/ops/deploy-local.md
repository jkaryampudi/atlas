# Deploying Atlas to a Linux box (home server or cloud — same kit)

The Mac is the dev machine; production is a box that never sleeps. This kit
targets any Ubuntu/Debian machine on your LAN (a spare PC, the GPU box, a
mini PC) and deploys **unchanged** to a cloud VM (Lightsail/EC2/Hetzner)
later — systemd, Docker and Tailscale don't care where the metal is.

## What you need on the box (once)

```bash
# 1. Docker (with the compose plugin)
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
# log out/in so the docker group applies

# 2. Python 3.12+
sudo apt install -y python3.12 python3.12-venv   # (or 3.13)

# 3. Tailscale — the private path to the console from your Mac and phone.
#    The API binds to 127.0.0.1 ONLY (it has no auth yet); Tailscale IS the
#    auth boundary until step-up tokens ship. (ADR-0018: docker-compose publishes
#    the api (8000) and db (5432) on 127.0.0.1 ONLY — previously bare 0.0.0.0,
#    exposing the unauthenticated API and local-only DB on all interfaces. Redis
#    is NOT host-published at all: it is unused by code and no host workflow needs
#    it; the api still reaches it over the compose network at redis:6379. The
#    binds are asserted by a static config-parse test
#    (tests/unit/test_docker_compose_bind.py); in a Docker environment, confirm at
#    runtime that api/db answer on loopback but not the host LAN address, and that
#    6379 is not reachable from the host at all.)
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
```

## Cutover (about 15 minutes)

```bash
# on the box — clone the repo (private repo: add a deploy key or use gh auth)
git clone git@github.com:jkaryampudi/atlas.git ~/atlas && cd ~/atlas

# bring the secrets over from the Mac (never in git):
#   (on the Mac)  scp ~/Documents/atlas/.env <box>:~/atlas/.env

# provision: venv, containers, migrations, systemd units, timers, health check
./ops/provision.sh

# bring the audit chain over — this doubles as the backup RESTORE DRILL:
#   (on the Mac)  docker exec atlas-db-1 pg_dump -U atlas atlas | gzip > /tmp/atlas.sql.gz
#                 scp /tmp/atlas.sql.gz <box>:~/
./ops/migrate_from_dump.sh ~/atlas.sql.gz
# it refuses to clobber a box that already holds audit events, restores,
# then runs verify_chain — the move is only done when the chain verifies.
```

## Reaching the console

From any Tailscale-connected device (Mac, phone):

```bash
# simplest: expose the API inside your tailnet only
sudo tailscale serve --bg 8001
# then open https://<box-name>.<tailnet>.ts.net from anywhere you're logged in
```

Approvals from the couch; the seal stays yours.

## What runs when (all times UTC — the box should stay on UTC)

| Unit | When | What |
|---|---|---|
| `atlas-api.service` | always (Restart=always) | API + console on 127.0.0.1:8001 |
| `atlas-daily.timer` | 23:30 UTC (= 09:30 AEST) | the full T0–T9 cycle |
| `atlas-backup.timer` | 00:30 UTC (= 10:30 AEST) | pg_dump, 30-day retention, optional off-box copy |
| `atlas-alert@.service` | on unit failure | pages ATLAS_ALERT_URL when a unit dies before it can report |

Off-box backups: set `ATLAS_BACKUP_REMOTE` in `.env` (any rclone remote —
`b2:atlas-backups`, `s3:bucket/atlas`, `gdrive:AtlasBackups`) and install
rclone. A backup on the same disk does not survive the disk.

## Health checks

```bash
systemctl status atlas-api            # running?
systemctl list-timers 'atlas-*'      # when do the next cycle/backup fire?
journalctl -u atlas-daily -n 50      # last cycle's full output
curl -s localhost:8001/v1/system/health
```

…and the console's jobs board shows every cycle with per-step results.

## Decommissioning the Mac's jobs (after the box is verified)

```bash
# on the Mac
launchctl unload ~/Library/LaunchAgents/com.atlas.*.plist
rm ~/Library/LaunchAgents/com.atlas.*.plist
```

Keep the Mac's repo checkout for development; the box owns production. Ship
changes with `git push` from the Mac, then on the box:
`cd ~/atlas && git pull && ./.venv/bin/pip install -q -e ".[dev]" && ./.venv/bin/alembic upgrade head && sudo systemctl restart atlas-api`.
