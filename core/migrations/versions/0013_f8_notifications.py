"""F8 (additive): notifications, per-user channel prefs, and task
recurrence (FR-12). Recurrence instances are independent task rows; in
v1 recurrence and dependencies are mutually exclusive (enforced in the
service). RLS+FORCE + flow_app grants, same patterns as 0012.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = ("notifications", "notification_prefs", "task_recurrences")

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE notification_channel AS ENUM ('telegram', 'email')",
    "CREATE TYPE notification_status AS ENUM ('pending', 'sent', 'failed')",
    "CREATE TYPE recurrence_freq AS ENUM ('daily', 'weekly', 'monthly')",
    """
    CREATE TABLE notifications (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      channel notification_channel NOT NULL,
      kind varchar(40) NOT NULL,
      title varchar(300) NOT NULL,
      body text NOT NULL,
      dedupe_key varchar(200),
      status notification_status NOT NULL DEFAULT 'pending',
      sent_at timestamptz,
      last_error text,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_notifications_org_id UNIQUE (org_id, dedupe_key)
    )
    """,
    "CREATE INDEX ix_notifications_org_id ON notifications (org_id)",
    "CREATE INDEX ix_notifications_user_id ON notifications (user_id)",
    """
    CREATE TABLE notification_prefs (
      org_id uuid NOT NULL,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      channel notification_channel NOT NULL,
      enabled boolean NOT NULL DEFAULT true,
      target varchar(320) NOT NULL DEFAULT '',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_notification_prefs PRIMARY KEY (org_id, user_id, channel)
    )
    """,
    """
    CREATE TABLE task_recurrences (
      task_id uuid PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      freq recurrence_freq NOT NULL,
      interval integer NOT NULL DEFAULT 1,
      next_run timestamptz NOT NULL,
      until timestamptz,
      active boolean NOT NULL DEFAULT true,
      last_spawned_at timestamptz,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_task_recurrences_org_id ON task_recurrences (org_id)",
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
    for table in ("notifications", "notification_prefs", "task_recurrences"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for typ in ("recurrence_freq", "notification_status", "notification_channel"):
        op.execute(f"DROP TYPE IF EXISTS {typ}")
