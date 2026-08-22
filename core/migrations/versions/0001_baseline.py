"""Squashed baseline (second cutover, 2026-08-22).

Replaces the 0001..0099 chain with a single tabula-rasa snapshot of the
production schema as of 2026-08-22. The chain had been squashed once
before, on 2026-05-25 (0001..0104 of the v1.0 era); this is the same
operation applied again. See docs/migrations.md for the cutover
instructions -- existing deployments stamp directly to revision
``0001`` and must NOT replay it, the schema is already there.

The DDL lives in the sibling ``0001_baseline.sql`` (cleaned ``pg_dump
--schema-only`` of the post-0099 schema). The loader streams the file
to the bind as a single multi-statement script: psycopg accepts that
shape and runs each statement in the same transaction Alembic already
opened.

The 16 revisions that carried DATA transformations, not just schema,
are kept whole under ``core/migrations/archive/`` with an index of what
each one repaired and whether it ever ran in production. A squash
captures the schema and drops the transformations, and four of those
backfills had silently no-opped in production (see the archive README
and the ADR-0015 amendment): archiving them is what keeps that
recoverable rather than merely lost.

Revision ID: 0001
Revises:
Create Date: 2026-08-22
"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SQL_PATH = pathlib.Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    # psycopg tokenises ``%`` as a parameter placeholder even when no
    # params are passed, and the dump carries one literal ``%`` inside a
    # PL/pgSQL ``RAISE EXCEPTION`` format. Doubling escapes it back to a
    # single ``%`` on the server side.
    sql = _SQL_PATH.read_text().replace("%", "%%")
    # ``pg_dump`` emits CREATE FUNCTION before CREATE TABLE; some bodies
    # reference tables defined later. Suppress body validation so the
    # forward references resolve at call time (PL/pgSQL semantics).
    prelude = "SET LOCAL check_function_bodies = off;\n"
    op.get_bind().exec_driver_sql(prelude + sql)


def downgrade() -> None:
    # Squashed baseline: the only meaningful downgrade is to zero.
    # Drop everything in ``public`` (tables, functions, types, policies,
    # indexes) by recreating the schema. Extensions live in ``public``
    # and are recreated by ``upgrade``.
    #
    # The runtime role is deliberately NOT dropped. It is bootstrapped
    # out of band, before migrations run, and its password comes from
    # the environment (deploy/local/bootstrap_roles.sql, ADR-0015): a
    # migration cannot recreate it, only destroy it. Dropping it here
    # made downgrade->upgrade impossible -- the baseline's GRANTs would
    # then land on a role that no longer exists -- which went unnoticed
    # while the chain was long enough that `downgrade -1` never reached
    # revision zero.
    op.execute("DROP SCHEMA IF EXISTS public CASCADE")
    op.execute("CREATE SCHEMA public")
    op.execute("GRANT ALL ON SCHEMA public TO public")
    # ``alembic_version`` lives in ``public`` and went down with it, but
    # Alembic updates its own bookkeeping AFTER this function returns,
    # and it insists on deleting EXACTLY one row ("expected to match one
    # row when deleting '0001'"). So the table has to come back with the
    # row still in it: Alembic then removes it and leaves the empty
    # table a database at revision zero is supposed to have.
    op.execute(
        "CREATE TABLE alembic_version ("
        "  version_num VARCHAR(32) NOT NULL,"
        "  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    )
    op.execute(f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')")
