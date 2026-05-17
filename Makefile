.PHONY: sync lint type test fmt up down migrate revision

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

migrate:
	uv run alembic -c core/alembic.ini upgrade head

revision:
	uv run alembic -c core/alembic.ini revision --autogenerate -m "$(m)"
