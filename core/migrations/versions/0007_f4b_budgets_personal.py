"""F4b (additive): personal-domain attributes and budget envelopes
(docs/adr/0013, 0014, FR-13/FR-14). Adds the ``budgets`` table and
task columns monetary_cost/location/necessity/budget_id. No parallel
domain: budgets are org-scoped, tasks reuse the existing taxonomy.
RLS+FORCE + flow_app grants for the new table, same patterns as 0006.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = ("budgets",)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE necessity AS ENUM ('must', 'should', 'nice')",
    "CREATE TYPE budget_period AS ENUM ('month', 'quarter', 'year', 'custom')",
    """
    CREATE TABLE budgets (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      name varchar(160) NOT NULL,
      category varchar(120),
      period_kind budget_period NOT NULL,
      period_start date NOT NULL,
      period_end date NOT NULL,
      amount numeric(14, 2) NOT NULL,
      currency varchar(3) NOT NULL DEFAULT 'EUR',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_budgets_period CHECK (period_end >= period_start),
      CONSTRAINT ck_budgets_amount CHECK (amount >= 0)
    )
    """,
    "CREATE INDEX ix_budgets_org_id ON budgets (org_id)",
    "ALTER TABLE tasks ADD COLUMN monetary_cost numeric(14, 2)",
    "ALTER TABLE tasks ADD COLUMN location varchar(200)",
    "ALTER TABLE tasks ADD COLUMN necessity necessity NOT NULL DEFAULT 'should'",
    """
    ALTER TABLE tasks ADD COLUMN budget_id uuid
      REFERENCES budgets(id) ON DELETE SET NULL
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
    for col in ("budget_id", "necessity", "location", "monetary_cost"):
        op.execute(f"ALTER TABLE tasks DROP COLUMN IF EXISTS {col}")
    op.execute("DROP TABLE IF EXISTS budgets CASCADE")
    op.execute("DROP TYPE IF EXISTS budget_period")
    op.execute("DROP TYPE IF EXISTS necessity")
