.PHONY: up down test lint type migrate replay doctor verify-chain
# load local env (.env is gitignored) so doctor/test/replay see ATLAS_* vars
-include .env
export
up:        ## start local stack (postgres, redis, api, dashboard)
	docker compose up -d
down:
	docker compose down
test:
	pytest
lint:
	ruff check atlas tests
type:
	mypy
migrate:
	alembic upgrade head
replay:    ## deterministic daily-cycle replay on fixtures: make replay DATE=2026-07-10
	python -m atlas.dcp.market_data.replay --date $(DATE)
doctor:    ## diagnose local environment
	python -m atlas.tools.doctor
verify-chain:  ## nightly audit hash-chain verification (alert on non-zero exit)
	python -m atlas.tools.verify_chain
cov-risk:  ## Phase 4 exit criterion: 100% branch coverage on dcp/risk
	pytest --cov=atlas.dcp.risk --cov-branch --cov-fail-under=100 -q
api:       ## run the read-only API locally (port 8001; 8000 is taken on this machine)
	uvicorn atlas.api.main:app --port 8001
dashboard: ## ops console (pure API client); needs `pip install -e ".[dashboard]"`
	ATLAS_API_URL=http://localhost:8001 streamlit run atlas/dashboard/overview.py
daily:     ## run the T0-T9 daily pipeline once, now (same entry launchd uses)
	python -m atlas.ops.daily
backup:    ## pg_dump the atlas DB to ~/AtlasBackups (or ATLAS_BACKUP_DIR)
	./ops/backup.sh
install-ops:  ## install launchd jobs: API (KeepAlive), daily 09:30, backup 10:30
	mkdir -p ~/Library/Logs
	cp ops/launchd/com.atlas.*.plist ~/Library/LaunchAgents/
	launchctl unload ~/Library/LaunchAgents/com.atlas.api.plist 2>/dev/null || true
	launchctl unload ~/Library/LaunchAgents/com.atlas.daily.plist 2>/dev/null || true
	launchctl unload ~/Library/LaunchAgents/com.atlas.backup.plist 2>/dev/null || true
	launchctl load ~/Library/LaunchAgents/com.atlas.api.plist
	launchctl load ~/Library/LaunchAgents/com.atlas.daily.plist
	launchctl load ~/Library/LaunchAgents/com.atlas.backup.plist
	@echo "installed — logs in ~/Library/Logs/atlas-*.log"
