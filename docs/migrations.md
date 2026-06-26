# Database migrations

Mycelium uses Alembic against a managed PostgreSQL (16+, with `pgvector`,
`pg_trgm`, `btree_gist`). Migrations run as the owner role `flow`
(BYPASSRLS); the application connects as `flow_app` (subject to RLS).
See [ADR-0015](adr/0015-rls-two-role-and-provisioning.md).

## Current chain

The chain starts at a single squashed baseline:

```
core/migrations/versions/
├── 0001_baseline.py   # loader (Alembic revision = "0001")
└── 0001_baseline.sql  # cleaned pg_dump of the post-v1.0 schema
```

`alembic upgrade head` applies the SQL file as one multi-statement
script. `SET LOCAL check_function_bodies = off` lets functions reference
tables that pg_dump emits later in the file.

## The 2026-05-25 squash (cutover)

Up to v1.0 the chain grew incrementally to 104 revisions, with embedded
backfills tied to dataset shapes that production has long left behind.
v2.0 collapses all of it into a single baseline that mirrors the
post-cutover schema (extensions, types, tables, RLS + FORCE policies,
GRANTs to `flow_app`, functions, triggers, indexes).

### Why

- Production was already aligned on `0104` (post `mycelium.xeno.garden`
  cutover): the incremental history had no live consumer left.
- New environments paid a long, brittle upgrade path through historical
  backfills. Several late backfills now fail on data shapes that exist
  only in old dev DBs (e.g. `0104` trips the GiST EXCLUDE on
  `task_participants`).
- Autogenerate diffs against the squashed metadata are meaningful again
  (a real diff against the live schema, not a delta against artefacts).

### What this means for existing deployments

Production (and any environment standing at `0104`) **must not replay
the new baseline**: the schema is already there. Stamp the row in
`alembic_version` to the new revision and stop:

```bash
# one-off, run once per environment that was at >= 0001 in the old chain
alembic -c core/alembic.ini stamp 0001
```

Fresh environments run `alembic upgrade head` normally.

### What was preserved

The baseline carries the full schema as of `0104`:

- 81 tables, all with `ENABLE ROW LEVEL SECURITY` (50 also `FORCE`).
- 37 enum types, 172 indexes, 69 RLS policies, 24 functions, 10
  triggers, 87 GRANTs / 16 REVOKEs to `flow_app`.
- `flow_app` runtime role (idempotent `CREATE ROLE`); the password is
  injected out-of-band by `deploy/local/bootstrap_roles.sql` (dev) /
  the deploy job (prod), as before.
- `provision_organization`, `create_default_workflow`,
  `create_default_calendar`: per-org seeding stays on-demand (no
  static seed data at migration time).

### What was dropped

- The migration-by-migration history. Historical doc/ADR references to
  specific `migration NNNN` mark facts about the system's evolution;
  they remain accurate even though the file no longer exists on
  disk. Treat them as historical citations, not pointers.
- The `alembic_version` table CREATE (Alembic manages this itself).

## Adding a new migration

```bash
make revision m="short description"
# (= uv run alembic -c core/alembic.ini revision --autogenerate -m "...")
```

Autogenerate sees `Base.metadata`, so it picks up table/column/index
diffs from SQLAlchemy models. Non-model objects (RLS policies,
functions, triggers, GRANTs) still need hand-written `op.execute(...)`
in the revision body.

Pre-commit: `ruff format --check .` and `ruff check .` must be green
before the revision lands ([memory: flow-frontend-tsc-cache]).
