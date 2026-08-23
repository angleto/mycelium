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

## The 2026-08-22 squash (current cutover)

The chain had grown back to 99 revisions. It was collapsed again, by the
same procedure as 2026-05-25 below: a cleaned `pg_dump --schema-only
--no-owner --exclude-table=public.alembic_version` of the post-`0099`
schema became the new `0001_baseline.sql`, and revisions `0002..0099`
were deleted.

Verified before landing: a fresh `alembic upgrade head` on the squashed
chain produces the same schema as replaying the full chain, down to 64
lines that differ only in how PostgreSQL re-renders `= ANY (ARRAY[...])`
inside CHECK constraints and partial indexes (semantically identical, and
a fixed point from here on — a second dump/restore round trip is byte
identical).

### What existing deployments must do

```bash
# once, on every environment that was at 0002..0099
alembic -c core/alembic.ini stamp 0001
```

**Do not run `upgrade`** on those: the schema is already there and the
baseline would try to create it again.

That command only works while the checkout still carries the OLD chain —
Alembic resolves the CURRENT revision before stamping, and `0099` no
longer exists once the squash has landed. Production was stamped from the
running pod, whose image predates the squash, so it resolved fine. From a
checkout that already has the squashed chain, the same environment needs:

```bash
alembic -c core/alembic.ini stamp 0001 --purge
```

`--purge` clears `alembic_version` instead of trying to resolve what is
in it. Same end state, and it is the form to use for a local database
that was sitting at an old revision.

### The roundtrip test now wipes the database it runs on

`core/tests/test_migrations.py` does `downgrade -1` then `upgrade head`.
With a single revision, `-1` is revision zero, so the downgrade drops and
recreates schema `public` — every row in that database included. It used
to revert one migration; now it empties the whole thing. Point it at a
throwaway database, never at one whose contents you want to keep.

### Two latent bugs this exposed

While the chain was long, `downgrade -1` never reached revision zero, so
the baseline's own `downgrade()` had never run. It had two faults, both
now fixed:

- it dropped the `mycelium_app` role, which is bootstrapped out of band
  and whose password a migration does not have — so it could destroy it
  but never recreate it, and the following `upgrade`'s GRANTs would land
  on a missing role;
- it dropped schema `public` including `alembic_version`, then Alembic
  tried to delete its bookkeeping row from a table that no longer
  existed (and it requires that delete to match exactly one row).

### The backfill archive

A squash keeps the schema and drops the data transformations. The 16
revisions that carried them are preserved whole under
`core/migrations/archive/`, with an index of what each repaired and
whether it ever ran in production. Four of them had silently no-opped
there (see below); archiving is what keeps that recoverable.

### Two SEEDS the archive triage missed (repaired by 0003)

The triage above sorted revisions into "schema" and "data
transformation". It missed a third category: a **seed**, an INSERT of
reference data that is a fresh database's starting state rather than a
repair of existing rows. Two revisions were classified as schema-only
and were neither archived nor carried into the baseline:

| revision | seeded | consequence on a fresh DB |
|---|---|---|
| `0074_system_settings_sdi_env` | the `system_settings` singleton | `_get_or_create` raced on first read; five concurrent invoice transmissions hit `UniqueViolationError` on the `id IS TRUE` PK. Turned CI red on tag `v2.2.19`. |
| `0043_default_rate_card` | 7 fleet fallback rate cards | `billing.resolve_rate` returns None for any model with no per-org card, and `_compute_credits` raises `rate_card.not_found` — every non-BYOK LLM call on a new deployment. Silent: the suite seeds its own cards. |

Production was never affected: it was stamped to `0001`, not replayed,
so both seeds survived (verified 2026-08-23). Only databases *built*
from the squashed baseline lacked them — CI, and any new environment.

`0003_restore_squashed_seeds.py` restores both idempotently
(`ON CONFLICT DO NOTHING`), so it is a no-op wherever they already
exist. Its `downgrade()` is deliberately a no-op: nothing distinguishes
a row it inserted from one that predates it, so deleting would strip
the fleet rate cards from a database that never needed repairing.

**For the next squash:** grep the revisions being dropped for INSERTs
inside `upgrade()`, not just for the word "backfill".

```bash
git show <pre-squash-ref>:core/migrations/versions/<rev>.py \
  | awk '/^def upgrade/,/^def downgrade/' | grep -nE "INSERT INTO|op\.bulk_insert"
```

An INSERT that seeds reference data must be carried into the new
baseline or replaced by a follow-up migration; an INSERT that repairs
existing rows can be archived. `core/tests/test_migrations.py::
test_reference_data_seeds_survive_the_chain` asserts the outcome, so a
future squash that drops these again turns red instead of shipping.

## The 2026-05-25 squash (first cutover, historical)

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

**Since 2026-08-22 the runner handles this centrally** and the manual
bracket below is no longer required. `core/migrations/env.py` wraps the
whole run in `mycelium_core.migration_rls.owner_sees_all_tenants`, which
lifts `FORCE` for the duration of the migration transaction and restores
it on the way out — and does nothing at all where the role already
bypasses RLS (dev, CI), so no locks are taken there.

That fix exists because documenting the trap was not enough. This
section has described it since the 0011/0013 incident, and `0016`,
`0022`, `0039`, `0095` and `0099` still shipped without the bracket. All
five were audited in production on 2026-08-22: `0099` and `0039` had
really no-opped and were repaired (22 annotations re-anchored; 48 time
entries realigned), `0022` and `0095` had no rows to touch anyway, and
`0016` is not recoverable but not a defect either. Per-migration outcomes
are in `core/migrations/archive/README.md`.

The point stands regardless of how much damage each one did: a rule that
has to be remembered on every revision is not a defence, and it was
forgotten five times after being written down.

The reasoning behind the mechanism, and the environment divergence that
made it invisible (the owner role is a superuser in dev and CI, not on
managed PostgreSQL), is in the ADR-0015 amendment.

The historical guidance follows, still valid as background and as the
pattern to use for a one-off repair script run outside Alembic:

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

Worked example, with its operational aftermath: `0086` repairs the
structural client/project tagging under this bracket and then guards
the result with constraint triggers (ADR-0050). What that costs an
operator afterwards -- how to check a live database, why restoring a
pre-0086 dump is the dangerous case, and why the list `0086` prints
must be captured at upgrade time (it is a temp table, `ON COMMIT
DROP`) -- is in
[runbooks/tag-structural-invariant.md](runbooks/tag-structural-invariant.md).
