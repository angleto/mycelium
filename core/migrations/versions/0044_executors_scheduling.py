"""Executor model + resource-aware scheduling fields (docs/adr/0025, P1).

Adds the first-class ``executors`` table (a work resource: human =
a workspace user bound by its working calendar + a context-switch
penalty; llm_agent = a K-parallel pool with a credit budget + per-hour
rate). Org-scoped + RLS exactly like the other tenant tables: the
``p_executors`` policy and flow_app grants use the canonical org
predicate (``app.current_org``), copied verbatim from the
0028/0040-style plain-table creates. ``executor_kind`` is a NEW native
enum (distinct from the task ``exec_kind`` type, which keeps its name).

Also adds two columns to ``schedule``: ``on_critical_chain`` (the
resource-aware critical chain, distinct from the logical critical
path) and ``projected_cost`` (projected LLM credit cost; projection
only, not metered). ``schedule`` is already org-scoped + RLS with the
``p_schedule`` policy and flow_app grants, so the plain column adds
inherit the table's policy/grants (same as the 0029/0037/0042
column-add migrations). Downgrade is symmetric: drop the columns, then
the table, then the enum type.

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE executor_kind AS ENUM ('human', 'llm_agent')",
    """
    CREATE TABLE executors (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      kind executor_kind NOT NULL,
      name varchar(120) NOT NULL,
      user_id uuid REFERENCES users(id) ON DELETE CASCADE,
      context_switch_cost_minutes integer NOT NULL DEFAULT 0,
      provider varchar(60),
      model_id varchar(120),
      max_parallel integer NOT NULL DEFAULT 4,
      credit_budget numeric(14, 4),
      credit_rate_per_hour numeric(14, 4) NOT NULL DEFAULT 0,
      enabled boolean NOT NULL DEFAULT true,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_executors PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_executors_org_id ON executors (org_id)",
    "CREATE INDEX ix_executors_user_id ON executors (user_id)",
    "ALTER TABLE executors ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE executors FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_executors ON executors "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON executors TO flow_app",
    ("ALTER TABLE schedule ADD COLUMN on_critical_chain boolean NOT NULL DEFAULT false"),
    ("ALTER TABLE schedule ADD COLUMN projected_cost numeric(14, 4) NOT NULL DEFAULT 0"),
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE schedule DROP COLUMN IF EXISTS projected_cost",
    "ALTER TABLE schedule DROP COLUMN IF EXISTS on_critical_chain",
    "DROP TABLE IF EXISTS executors CASCADE",
    "DROP TYPE IF EXISTS executor_kind",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
