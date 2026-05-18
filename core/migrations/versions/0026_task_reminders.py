"""Per-task reminders (Google-Calendar style).

A task can have N reminders, each ``offset_minutes`` before its
due_date (0 = at due). The scanner enqueues a notification per
reminder to the task's assignees on their enabled channels. RLS +
flow_app grants + org FK cascade, same pattern as the other
org-scoped tables.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE task_reminders (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      offset_minutes integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_task_reminders_task_offset UNIQUE (task_id, offset_minutes),
      CONSTRAINT ck_task_reminders_offset CHECK (offset_minutes >= 0)
    )
    """,
    "CREATE INDEX ix_task_reminders_org_id ON task_reminders (org_id)",
    "CREATE INDEX ix_task_reminders_task_id ON task_reminders (task_id)",
    "ALTER TABLE task_reminders ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE task_reminders FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_task_reminders ON task_reminders "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON task_reminders TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS task_reminders CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
