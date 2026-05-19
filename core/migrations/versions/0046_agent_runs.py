"""Agent execution runtime: agent_runs table (docs/adr/0025, P3).

Adds the ``agent_runs`` table -- the metered, bounded, killable record
of one ``llm_agent`` task driven end-to-end (spawn -> work -> artifact
-> complete). Org-scoped + RLS exactly like the other tenant tables:
the ``p_agent_runs`` policy and flow_app grants use the canonical org
predicate (``app.current_org``), copied verbatim from the
0044-style plain-table create. ``agent_run_status`` is a NEW native
enum (queued|running|succeeded|failed|cancelled|blocked).

``task_id`` FK ON DELETE CASCADE (a run cannot outlive its task);
``executor_id`` FK ON DELETE SET NULL (run history stays readable when
an executor is removed). ``ix_agent_runs_task_id`` mirrors the existing
``ix_*_*_id`` index naming (the active-run lookup is by task).
Downgrade is symmetric: drop the table (CASCADE) then the enum type.

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    (
        "CREATE TYPE agent_run_status AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')"
    ),
    """
    CREATE TABLE agent_runs (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      executor_id uuid REFERENCES executors(id) ON DELETE SET NULL,
      status agent_run_status NOT NULL,
      steps integer NOT NULL DEFAULT 0,
      credits_spent numeric(14, 4) NOT NULL DEFAULT 0,
      started_at timestamptz,
      ended_at timestamptz,
      error varchar(500),
      artifact_note_id uuid,
      cancel_requested boolean NOT NULL DEFAULT false,
      blocked_reason varchar(120),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_agent_runs PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_agent_runs_org_id ON agent_runs (org_id)",
    "CREATE INDEX ix_agent_runs_task_id ON agent_runs (task_id)",
    "ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_agent_runs ON agent_runs "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_runs TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS agent_runs CASCADE",
    "DROP TYPE IF EXISTS agent_run_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
