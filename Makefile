.PHONY: sync lint fmt type test eval up down db-bootstrap migrate revision \
        run-api run-mcp run-worker run-sdi

sync:
	uv sync --all-packages

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy -p mycelium_core -p mycelium_api -p mycelium_mcp -p mycelium_worker -p mycelium_sdi_inbound

test:
	uv run pytest

# Offline retrieval eval gate (ADR-0035 / Mycelio WS-E1): deterministic
# gold-set recall@k/MRR + dense-tier health over the real pipeline. Runs
# in CI as part of `test`; this target runs just the gate for a quick
# local baseline check.
eval:
	uv run pytest core/tests/test_eval_offline.py -q

up:
	docker compose -f deploy/local/docker-compose.yml up -d

down:
	docker compose -f deploy/local/docker-compose.yml down

# Create/ensure the runtime role mycelium_app and set its password from
# MYCELIUM_DB_APP_PASSWORD (env). Run after `up`, before `migrate`.
db-bootstrap:
	docker compose -f deploy/local/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-mycelium} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-mycelium} -d $${POSTGRES_DB:-mycelium} \
	  -v app_pw="$${MYCELIUM_DB_APP_PASSWORD:?set MYCELIUM_DB_APP_PASSWORD}" \
	  -f - < deploy/local/bootstrap_roles.sql

migrate:
	uv run alembic -c core/alembic.ini upgrade head

revision:
	uv run alembic -c core/alembic.ini revision --autogenerate -m "$(m)"

run-api:
	uv run uvicorn mycelium_api.main:app --reload

run-mcp:
	uv run python -m mycelium_mcp.main

run-worker:
	uv run python -m mycelium_worker.main

run-sdi:
	uv run uvicorn mycelium_sdi_inbound.main:app --reload --port 8081

# CLI dev convenience: ``make cli ARGS="task list --json"``.
cli:
	uv run mycelium $(ARGS)

# Run the CLI smoke tests only (offline, no backend needed).
test-cli:
	uv run pytest cli/tests -x
