.PHONY: up down test lint type migrate replay doctor
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
