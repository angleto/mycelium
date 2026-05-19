"""Closed-loop dispatch + approval gates (docs/adr/0025, P5).

Adds the ``dispatch_requests`` table -- a human-in-the-loop approval
gate for an ``llm_agent`` task the P1/P2 scheduler admitted. The closed
loop (``services.dispatch_loop``) recomputes the schedule, creates one
``pending`` request per newly-admitted agent task, and -- only on
explicit approval (a human via the API or the workspace ``auto`` policy
opt-in) -- starts the run via the P3 metered path, recording the
``agent_run_id`` and moving the request to ``dispatched``. Governance
default is human-in-the-loop: no credit spend without an explicit gate.

Org-scoped + RLS exactly like the other tenant tables: the
``p_dispatch_requests`` policy and flow_app grants use the canonical org
predicate (``app.current_org``), copied verbatim from the
0046/0047-style plain-table create. ``dispatch_status`` is a NEW native
enum (pending|approved|dispatched|denied|skipped|failed).

FKs: ``task_id`` is ``ON DELETE CASCADE`` (a request cannot outlive its
task); ``executor_id`` / ``agent_run_id`` are ``ON DELETE SET NULL``
(the request is a historical governance record that survives an
executor or run removal). ``ix_dispatch_requests_task_id`` mirrors the
existing ``ix_*_*_id`` index naming (the loop / queue queries filter by
task and by org).

Downgrade is symmetric: drop the table (CASCADE), then the enum type.

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    (
        "CREATE TYPE dispatch_status AS ENUM "
        "('pending', 'approved', 'dispatched', 'denied', 'skipped', 'failed')"
    ),
    """
    CREATE TABLE dispatch_requests (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      executor_id uuid REFERENCES executors(id) ON DELETE SET NULL,
      status dispatch_status NOT NULL DEFAULT 'pending',
      projected_credit_cost numeric(14, 4) NOT NULL DEFAULT 0,
      agent_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
      requested_at timestamptz NOT NULL DEFAULT now(),
      decided_at timestamptz,
      decided_by uuid,
      reason varchar(200),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_dispatch_requests PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_dispatch_requests_org_id ON dispatch_requests (org_id)",
    "CREATE INDEX ix_dispatch_requests_task_id ON dispatch_requests (task_id)",
    "ALTER TABLE dispatch_requests ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE dispatch_requests FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_dispatch_requests ON dispatch_requests "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON dispatch_requests TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS dispatch_requests CASCADE",
    "DROP TYPE IF EXISTS dispatch_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
