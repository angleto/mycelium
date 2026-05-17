"""F4 (additive): time tracking. One ``time_entries`` table (timer or
manual), with a DB-level guarantee of at most one running timer per
(org, user) via a partial unique index. RLS+FORCE + flow_app grants,
same patterns as 0005 (docs/adr/0002, FR-5).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = ("time_entries",)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE time_source AS ENUM ('timer', 'manual')",
    """
    CREATE TABLE time_entries (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      started_at timestamptz NOT NULL,
      ended_at timestamptz,
      duration_seconds integer,
      source time_source NOT NULL,
      billable boolean NOT NULL DEFAULT true,
      rate_snapshot numeric(12, 2),
      currency varchar(3) NOT NULL DEFAULT 'EUR',
      note text,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_time_entries_interval
        CHECK (ended_at IS NULL OR ended_at > started_at),
      CONSTRAINT ck_time_entries_duration
        CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
    )
    """,
    "CREATE INDEX ix_time_entries_org_id ON time_entries (org_id)",
    "CREATE INDEX ix_time_entries_task_id ON time_entries (task_id)",
    "CREATE INDEX ix_time_entries_user_id ON time_entries (user_id)",
    # At most one running timer (ended_at IS NULL) per (org, user).
    """
    CREATE UNIQUE INDEX uq_time_entries_running
      ON time_entries (org_id, user_id)
      WHERE ended_at IS NULL
    """,
)


def _rls(table: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY p_{table} ON {table} USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO flow_app",
    )


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)
    for table in _RLS_TABLES:
        for stmt in _rls(table):
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS time_entries CASCADE")
    op.execute("DROP TYPE IF EXISTS time_source")
