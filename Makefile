.PHONY: sync lint fmt type test test-db-up test-db-down web-check eval eval-humus eval-bench \
        mcp-coverage mcp-coverage-check up down \
        db-bootstrap migrate db-harden revision run-api run-mcp run-worker run-sdi

sync:
	uv sync --all-packages

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

# Two invocations, because they check different things. The first is the
# shipped code under `strict`. The second covers the trees that used to sit
# outside the gate entirely -- the tests, the conftest, and scripts/ -- where
# the configured override keeps only the checks that catch a caller which
# cannot run (see the mypy overrides in pyproject.toml for why the rest is
# off). A test that no longer matches the signature it calls is invisible to
# ruff and costs a full suite run to find; this is the cheap way to see it.
type:
	uv run mypy -p mycelium_core -p mycelium_api -p mycelium_mcp -p mycelium_worker -p mycelium_sdi_inbound
	uv run mypy core/tests api/tests worker/tests cli/tests scripts

test:
	uv run pytest

# --- the throwaway test database -------------------------------------------
#
# The suite refuses to start against a database that has not been marked
# disposable (see _test_db_guard.py). This is the recipe that produces one,
# and it is the CI job's, run end-to-end.
#
# Do NOT point the suite at the local development stack (`make up`, host
# port 5433). Its data is real enough to miss, and the ACL hardening the
# suite applies is not something anybody undoes.
#
# TWO THINGS MEASURED, both of which cost an hour the first time:
#
#  * `pg_isready` turns true DURING initdb, against the entrypoint's
#    temporary server, so it is not a readiness signal here. The loop below
#    waits for a real query instead.
#  * the suite takes about 31 minutes on a VIRGIN database, and the rows
#    accumulate: the same suite on the same container a second time took
#    51 minutes, and on a development database after eight runs it had not
#    finished in 90. Recreate the container per run rather than reusing it.
TEST_DB_CONTAINER ?= mycelium-test-fresh
TEST_DB_PORT ?= 5439
TEST_DB_SYNC := postgresql+psycopg://mycelium:mycelium@localhost:$(TEST_DB_PORT)/mycelium
TEST_DB_ASYNC := postgresql+asyncpg://mycelium_app:mycelium_app@localhost:$(TEST_DB_PORT)/mycelium

test-db-up:
	@docker rm -f $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_DB_CONTAINER) --tmpfs /var/lib/postgresql/data \
	  -e POSTGRES_USER=mycelium -e POSTGRES_PASSWORD=mycelium -e POSTGRES_DB=mycelium \
	  -p $(TEST_DB_PORT):5432 pgvector/pgvector:pg16
	@echo "waiting for a real query (pg_isready is true during initdb)..."
	@until PGPASSWORD=mycelium psql -h localhost -p $(TEST_DB_PORT) -U mycelium \
	  -d mycelium -tAc "SELECT 1" >/dev/null 2>&1; do sleep 2; done
	PGPASSWORD=mycelium psql -h localhost -p $(TEST_DB_PORT) -U mycelium -d mycelium \
	  -q -v ON_ERROR_STOP=1 -v app_pw=mycelium_app -f deploy/local/bootstrap_roles.sql
	MYCELIUM_DATABASE_URL_SYNC="$(TEST_DB_SYNC)" MYCELIUM_DATABASE_URL="$(TEST_DB_ASYNC)" \
	  MYCELIUM_DB_APP_PASSWORD=mycelium_app uv run alembic -c core/alembic.ini upgrade head
	PGPASSWORD=mycelium psql -h localhost -p $(TEST_DB_PORT) -U mycelium -d mycelium \
	  -q -v ON_ERROR_STOP=1 -f deploy/local/harden_function_acls.sql
	PGPASSWORD=mycelium psql -h localhost -p $(TEST_DB_PORT) -U mycelium -d mycelium \
	  -q -v ON_ERROR_STOP=1 -f deploy/local/mark_test_database.sql
	@echo
	@echo "Ready. Export these, then run the suite (about 31 minutes):"
	@echo "    export MYCELIUM_DATABASE_URL='$(TEST_DB_ASYNC)'"
	@echo "    export MYCELIUM_DATABASE_URL_SYNC='$(TEST_DB_SYNC)'"
	@echo "    export MYCELIUM_DB_APP_PASSWORD=mycelium_app"

test-db-down:
	docker rm -f $(TEST_DB_CONTAINER)


# The SPA gate, matching what CI's `web` job runs. Worth a target of its
# own: `tsc --noEmit` from the repo root silently checks NOTHING (the root
# tsconfig.json is a references stub with "files": []), so the obvious
# invocation reports success on a file it never opened. `tsc -b` is the
# one that type-checks.
#
# `check:api-types` regenerates the SPA's API types from the API's OpenAPI
# document and fails if the committed ones differ, so this target needs a
# synced backend environment (`make sync`) as well as node. It runs first:
# every step after it reads types that are only meaningful once they are
# known to be the API's.
web-check:
	cd web && pnpm install --frozen-lockfile \
	  && pnpm check:api-types \
	  && pnpm exec eslint . \
	  && pnpm check:shared \
	  && pnpm check:i18n \
	  && pnpm check:css \
	  && pnpm check:lengths \
	  && pnpm typecheck \
	  && pnpm test

# The browser extension gate, matching CI's `extension` job. Its own
# package with its own lockfile, so `pnpm install` here does not touch
# the SPA's tree.
extension-check:
	cd extension && pnpm install --frozen-lockfile \
	  && pnpm exec eslint . \
	  && pnpm check:messages \
	  && pnpm check:grammar \
	  && pnpm check:tokens \
	  && pnpm typecheck \
	  && pnpm test

# A loadable unpacked directory at extension/dist/unpacked. There is no
# default origin: it decides which deployment the package talks to, which
# origin it may fetch from and which origin may hand it a credential, so
# a missing value stops the build rather than picking somewhere.
#   MYCELIUM_EXTENSION_ORIGIN=https://mycelium.xeno.garden make extension-build
extension-build:
	cd extension && pnpm build

# The archive: the upload artifact, and what a deployment serves for
# download. Refuses a non-https origin, because the store would accept a
# localhost build and every installer would get an extension talking to
# their own machine.
#
# The script is `zip` and not `pack` because `pnpm pack` is a BUILT-IN
# that shadows a script of that name: this target used to produce an npm
# tarball of the package sources, silently, and never the archive it
# names.
extension-zip:
	cd extension && pnpm zip

# Offline retrieval eval gate (ADR-0035 / Mycelio WS-E1): deterministic
# gold-set recall@k/MRR + dense-tier health over the real pipeline. Runs
# in CI as part of `test`; this target runs just the gate for a quick
# local baseline check.
eval:
	uv run pytest core/tests/test_eval_offline.py -q

# Humus retrieval A/B over a REAL corpus (task 4836a6cc / note 9a2adb4a §4):
# same run_humus_ab matrix as the CI test, from gold JSONL files. Provide the
# files (and optionally org/actor, else an owner is auto-resolved):
#   make eval-humus RAW=raw.jsonl CON=consolidation.jsonl [ORG=<uuid> ACTOR=<uuid>]
eval-humus:
	uv run python scripts/eval_humus_ab.py --raw "$(RAW)" --consolidation "$(CON)" \
		$(if $(ORG),--org $(ORG),) $(if $(ACTOR),--actor $(ACTOR),)

# Public memory benchmarks (LongMemEval / LOCOMO, task cc4653bd): ingest an
# operator-provided dataset file into throwaway orgs and score retrieval on
# the same run_eval path as CI. NEVER against prod (creates orgs/blobs):
#   make eval-bench DATASET=longmemeval FILE=~/.../longmemeval_oracle.json [K=10 LIMIT=20 QLIMIT=50]
eval-bench:
	uv run python scripts/eval_public_bench.py --dataset "$(DATASET)" --path "$(FILE)" \
		$(if $(K),--k $(K),) $(if $(LIMIT),--limit-instances $(LIMIT),) \
		$(if $(QLIMIT),--limit-questions $(QLIMIT),)

# Regenerate the auto-generated tool inventory in docs/mcp-coverage.md from
# the live registry (counts + per-domain listing never drift from code).
mcp-coverage:
	uv run python scripts/gen_mcp_coverage.py

# CI/pre-commit gate: fail if docs/mcp-coverage.md is stale. DB-free and
# embedder-free (importing the server only registers tool callables).
mcp-coverage-check:
	uv run python scripts/gen_mcp_coverage.py --check

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

# Reproduce the production function-execute posture: revoke the default
# PUBLIC execute on our functions so mycelium_app keeps only its explicit
# grants (see the SQL header and docs/adr/0015). Run after `migrate`. The
# pytest suite applies this automatically (root conftest); this target is
# for the local docker stack so `make run-api` matches prod too.
db-harden:
	docker compose -f deploy/local/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-mycelium} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-mycelium} -d $${POSTGRES_DB:-mycelium} \
	  -f - < deploy/local/harden_function_acls.sql

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
