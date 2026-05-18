.PHONY: sync lint fmt type test up down db-bootstrap migrate revision \
        run-api run-mcp run-worker run-sdi

sync:
	uv sync --all-packages

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy -p flow_core -p flow_api -p flow_mcp -p flow_worker -p flow_sdi_inbound

test:
	uv run pytest

up:
	docker compose -f deploy/local/docker-compose.yml up -d

down:
	docker compose -f deploy/local/docker-compose.yml down

# Create/ensure the runtime role flow_app and set its password from
# FLOW_DB_APP_PASSWORD (env). Run after `up`, before `migrate`.
db-bootstrap:
	docker compose -f deploy/local/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-flow} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-flow} -d $${POSTGRES_DB:-flow} \
	  -v app_pw="$${FLOW_DB_APP_PASSWORD:?set FLOW_DB_APP_PASSWORD}" \
	  -f - < deploy/local/bootstrap_roles.sql

migrate:
	uv run alembic -c core/alembic.ini upgrade head

revision:
	uv run alembic -c core/alembic.ini revision --autogenerate -m "$(m)"

run-api:
	uv run uvicorn flow_api.main:app --reload

run-mcp:
	uv run python -m flow_mcp.main

run-worker:
	uv run python -m flow_worker.main

run-sdi:
	uv run uvicorn flow_sdi_inbound.main:app --reload --port 8081
