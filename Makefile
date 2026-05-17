.PHONY: sync lint fmt type test up down db-bootstrap migrate revision

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
	docker compose -f deploy/docker-compose.yml up -d

down:
	docker compose -f deploy/docker-compose.yml down

# Crea/assicura il ruolo runtime flow_app e ne imposta la password da
# FLOW_DB_APP_PASSWORD (env). Eseguire dopo `up`, prima di `migrate`.
db-bootstrap:
	docker compose -f deploy/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-flow} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-flow} -d $${POSTGRES_DB:-flow} \
	  -v app_pw="$${FLOW_DB_APP_PASSWORD:?set FLOW_DB_APP_PASSWORD}" \
	  -f - < deploy/bootstrap_roles.sql

migrate:
	uv run alembic -c core/alembic.ini upgrade head

revision:
	uv run alembic -c core/alembic.ini revision --autogenerate -m "$(m)"
