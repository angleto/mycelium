# Database migrations

Mycelium uses Alembic against a managed PostgreSQL (16+, with `pgvector`,
`pg_trgm`, `btree_gist`). Migrations run as the schema owner role; the
application connects as `flow_app` (non-owner, subject to RLS). The
owner is NOT automatically exempt from `FORCE ROW LEVEL SECURITY`: see
"Data repair in migrations" below before writing a backfill.
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

## Data repair in migrations

A migration that reads or writes tenant rows (backfill, repair,
re-tag) runs WITHOUT an `app.current_org` GUC. Most org-scoped tables
carry `FORCE ROW LEVEL SECURITY`, which applies to the table owner
too, so the policy predicate evaluates against an empty GUC and the
statement sees **zero rows**. It does not fail: it commits a no-op.
Migration `0011` shipped exactly that, `0012` then dropped the column
the backfill was supposed to have copied, and prod note bodies came
back empty (incident 2026-05-27, task `1cd8bc0a`, recovered by `0013`).

Do not rely on the migration role being exempt: only a superuser (or
an explicit `BYPASSRLS` attribute) escapes FORCE, and that is a
per-environment property of the deployed role, not something a
revision can assume. Write the repair so it is correct for a plain
owner.

Bracket every such statement:

```python
op.execute("ALTER TABLE notes NO FORCE ROW LEVEL SECURITY")
op.execute("ALTER TABLE note_part NO FORCE ROW LEVEL SECURITY")
try:
    op.execute("INSERT INTO note_part (...) SELECT ... FROM notes ...")
finally:
    op.execute("ALTER TABLE note_part FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notes FORCE ROW LEVEL SECURITY")
```

Rules that make the bracket safe:

- **Every table the statement touches** needs the relaxation, the ones
  it merely reads included, not just the write target. `0011`'s fixed
  backfill relaxes `notes` (read), `note_part` (written, then read
  back) AND `blob_sources` (updated by joining the parts it had just
  inserted).
- **`finally`, always.** An exception between the relaxation and the
  restore would otherwise leave the table without FORCE, i.e. a
  tenant-isolation hole in whatever state the failed upgrade leaves
  behind.
- **Relax FORCE, never disable RLS.** `NO FORCE` restores owner
  visibility only; `DISABLE ROW LEVEL SECURITY` would also drop the
  policy for `flow_app` if the migration aborted mid-way.
- **Make the repair idempotent** (`NOT EXISTS`, `ON CONFLICT DO
  NOTHING`, a `WHERE` that excludes already-repaired rows): a repair is
  routinely re-run after an out-of-band fix has already landed, as
  `0013` was.
- **Assert the row count.** A missing bracket and a genuinely empty
  dataset look identical from the outside, which is why `0011` passed
  review. When the repair must move rows, check `rowcount` (or
  re-`SELECT` and compare) instead of trusting a clean run.

Only a data repair needs this. Pure DDL (`ALTER TABLE`, `CREATE
INDEX`) is not subject to RLS and must stay outside the bracket.
