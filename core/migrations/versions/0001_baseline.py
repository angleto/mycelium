"""Squashed baseline (post v1.0 cutover).

Replaces the incremental 0001..0104 chain with a single tabula-rasa
snapshot of the production schema as of 2026-05-25. See CHANGELOG and
docs/migrations.md for the cutover instructions (existing deployments
stamp directly to revision ``0001``; no replay).

The DDL lives in the sibling ``0001_baseline.sql`` (cleaned ``pg_dump
--schema-only`` of the post-0104 schema). The loader streams the file
to the bind as a single multi-statement script: psycopg accepts that
shape and runs each statement in the same transaction Alembic already
opened.

Revision ID: 0001
Revises:
Create Date: 2026-05-25
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
    # indexes) by recreating the schema; then drop the runtime role.
    # Extensions live in ``public`` and are recreated by ``upgrade``.
    op.execute("DROP SCHEMA IF EXISTS public CASCADE")
    op.execute("CREATE SCHEMA public")
    op.execute("GRANT ALL ON SCHEMA public TO public")
    op.execute("DROP ROLE IF EXISTS flow_app")
