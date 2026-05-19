"""Coordination handoff protocol + contract-net offer (docs/adr/0025, P4).

Adds the ``task_handoffs`` table -- a typed coordination message bound
to a dependency edge: the producer's artifact (a note id, nullable =
message-only) + a short system message, delivered to the successor's
resolved executor (human -> notification + note link; llm_agent ->
consumed by the P3 runtime context). Org-scoped + RLS exactly like the
other tenant tables: the ``p_task_handoffs`` policy and flow_app grants
use the canonical org predicate (``app.current_org``), copied verbatim
from the 0044/0046-style plain-table create. ``handoff_status`` is a
NEW native enum (pending|delivered|consumed|cancelled).

The task FKs are ``ON DELETE CASCADE`` (a handoff cannot outlive either
edge endpoint); ``from/to_executor_id`` are ``ON DELETE SET NULL`` (the
handoff is a historical coordination record that survives an executor
removal). ``ix_task_handoffs_predecessor_task_id`` /
``ix_task_handoffs_successor_task_id`` mirror the existing ``ix_*_*_id``
index naming (the on-completion fan-out queries by predecessor; the
inbound-context / list queries by successor).

Also adds one plain column ``tasks.offered`` (``boolean NOT NULL
DEFAULT false``): the lightweight contract-net "announced/awaiting
claim" flag (a single transient per-task boolean, like
``is_archived``/``is_milestone`` -- no history, no bid table; full
bidding is beyond P4's minimal contract-net). ``tasks`` is already
org-scoped + RLS with the ``p_tasks`` policy and flow_app grants, so a
plain column add inherits the table's policy/grants (same as the
0043/0044/0045 column-add migrations).

Downgrade is symmetric: drop the ``tasks.offered`` column, then the
table (CASCADE), then the enum type.

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    ("CREATE TYPE handoff_status AS ENUM ('pending', 'delivered', 'consumed', 'cancelled')"),
    """
    CREATE TABLE task_handoffs (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      predecessor_task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      successor_task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      from_executor_id uuid REFERENCES executors(id) ON DELETE SET NULL,
      to_executor_id uuid REFERENCES executors(id) ON DELETE SET NULL,
      message varchar(1000) NOT NULL DEFAULT '',
      artifact_note_id uuid,
      status handoff_status NOT NULL DEFAULT 'pending',
      delivered_at timestamptz,
      consumed_at timestamptz,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_task_handoffs PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_task_handoffs_org_id ON task_handoffs (org_id)",
    "CREATE INDEX ix_task_handoffs_predecessor_task_id ON task_handoffs (predecessor_task_id)",
    "CREATE INDEX ix_task_handoffs_successor_task_id ON task_handoffs (successor_task_id)",
    "ALTER TABLE task_handoffs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE task_handoffs FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_task_handoffs ON task_handoffs "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON task_handoffs TO flow_app",
    "ALTER TABLE tasks ADD COLUMN offered boolean NOT NULL DEFAULT false",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks DROP COLUMN IF EXISTS offered",
    "DROP TABLE IF EXISTS task_handoffs CASCADE",
    "DROP TYPE IF EXISTS handoff_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
