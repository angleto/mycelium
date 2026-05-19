"""Executor registry + admission-control dispatch fields (docs/adr/0025, P2).

Three plain column adds (no data backfill beyond the literal defaults):

- ``executors.capability_tags`` (``text[] NOT NULL DEFAULT '{}'``): the
  capability set an ``llm_agent`` advertises; a task is eligible for an
  agent iff its ``required_capabilities`` are a subset. ``text[]`` is the
  established norm for a flat string list in this schema (mirrors
  ``users.backup_codes_hash`` added as ``text[]`` in 0015); JSONB is
  reserved for structured dicts (organizations.settings, fiscal_profile).
- ``tasks.required_capabilities`` (``text[] NOT NULL DEFAULT '{}'``): the
  capabilities a task needs from its executor (empty = any agent).
- ``schedule.assigned_executor_id`` (``uuid NULL`` FK ``executors(id)``
  ON DELETE SET NULL), ``schedule.unassignable`` (``boolean NOT NULL
  DEFAULT false``) and ``schedule.unassignable_reason`` (``varchar(200)
  NULL``): the admission-control dispatch result -- the chosen executor,
  or a flagged dispatch gap with a short stable reason.

``executors``, ``tasks`` and ``schedule`` are already org-scoped + RLS
(ENABLE + FORCE) with their existing policies and flow_app grants; a
plain column add inherits the table's existing policy and grants, so no
extra GRANT/POLICY is needed (same as the 0043/0044 column-add
migrations). The FK target ``executors.id`` is org-scoped under the same
``app.current_org`` predicate, so SET-NULL stays within the tenant.
Index ``ix_schedule_assigned_executor_id`` mirrors the existing
``ix_*_*_id`` index naming. Downgrade drops the index then the columns,
symmetrically.

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE executors ADD COLUMN capability_tags text[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE tasks ADD COLUMN required_capabilities text[] NOT NULL DEFAULT '{}'",
    (
        "ALTER TABLE schedule ADD COLUMN assigned_executor_id uuid "
        "REFERENCES executors(id) ON DELETE SET NULL"
    ),
    "ALTER TABLE schedule ADD COLUMN unassignable boolean NOT NULL DEFAULT false",
    "ALTER TABLE schedule ADD COLUMN unassignable_reason varchar(200)",
    "CREATE INDEX ix_schedule_assigned_executor_id ON schedule (assigned_executor_id)",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_schedule_assigned_executor_id",
    "ALTER TABLE schedule DROP COLUMN IF EXISTS unassignable_reason",
    "ALTER TABLE schedule DROP COLUMN IF EXISTS unassignable",
    "ALTER TABLE schedule DROP COLUMN IF EXISTS assigned_executor_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS required_capabilities",
    "ALTER TABLE executors DROP COLUMN IF EXISTS capability_tags",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
