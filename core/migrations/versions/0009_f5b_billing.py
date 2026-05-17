"""F5b (additive): credit metering core (docs/adr/0019, FR-15).

Per-org wallet, append-only idempotent credit_ledger and usage_record
(reuse forbid_mutation() from 0001), DB-driven rate_cards, storage
rates and billing config. RLS+FORCE + grants; the ledger/usage tables
get INSERT only (append-only), the wallet gets UPDATE (atomic
check-and-debit), config tables full CRUD (admin).

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE ledger_entry_kind AS ENUM ('grant', 'debit')",
    "CREATE TYPE rate_unit AS ENUM ('token', 'audio_min', 'tts_char', 'gb_month')",
    "CREATE TYPE cost_basis AS ENUM ('local', 'our_key', 'byok')",
    "CREATE TYPE storage_kind AS ENUM ('db', 's3')",
    """
    CREATE TABLE wallet (
      org_id uuid PRIMARY KEY,
      balance numeric(18, 4) NOT NULL DEFAULT 0,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_wallet_balance CHECK (balance >= 0)
    )
    """,
    """
    CREATE TABLE credit_ledger (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      kind ledger_entry_kind NOT NULL,
      amount numeric(18, 4) NOT NULL,
      operation_id varchar(128),
      reason text,
      balance_after numeric(18, 4) NOT NULL,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_credit_ledger_amount CHECK (amount >= 0),
      CONSTRAINT uq_credit_ledger_org_id UNIQUE (org_id, operation_id)
    )
    """,
    "CREATE INDEX ix_credit_ledger_org_id ON credit_ledger (org_id)",
    """
    CREATE TRIGGER trg_credit_ledger_append_only
    BEFORE UPDATE OR DELETE ON credit_ledger
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
    """
    CREATE TABLE rate_cards (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      model_id varchar(160) NOT NULL,
      provider varchar(80) NOT NULL,
      unit rate_unit NOT NULL DEFAULT 'token',
      credits_per_input numeric(18, 8) NOT NULL DEFAULT 0,
      credits_per_output numeric(18, 8) NOT NULL DEFAULT 0,
      provider_cost_per_input numeric(18, 8),
      provider_cost_per_output numeric(18, 8),
      markup numeric(8, 4) NOT NULL DEFAULT 1,
      is_active boolean NOT NULL DEFAULT true,
      tier varchar(40),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_rate_cards_org_id UNIQUE (org_id, model_id)
    )
    """,
    "CREATE INDEX ix_rate_cards_org_id ON rate_cards (org_id)",
    """
    CREATE TABLE storage_rates (
      org_id uuid NOT NULL,
      kind storage_kind NOT NULL,
      credits_per_gb_month numeric(18, 8) NOT NULL DEFAULT 0,
      is_active boolean NOT NULL DEFAULT true,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_storage_rates PRIMARY KEY (org_id, kind)
    )
    """,
    """
    CREATE TABLE billing_config (
      org_id uuid PRIMARY KEY,
      byok_fee_factor numeric(18, 8) NOT NULL DEFAULT 0.0001,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE usage_record (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      operation_id varchar(128) NOT NULL,
      model_id varchar(160),
      op varchar(80) NOT NULL,
      basis cost_basis NOT NULL,
      units_in numeric(18, 4) NOT NULL DEFAULT 0,
      units_out numeric(18, 4) NOT NULL DEFAULT 0,
      credits numeric(18, 4) NOT NULL,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_usage_record_org_id UNIQUE (org_id, operation_id)
    )
    """,
    "CREATE INDEX ix_usage_record_org_id ON usage_record (org_id)",
    """
    CREATE TRIGGER trg_usage_record_append_only
    BEFORE UPDATE OR DELETE ON usage_record
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
)

# (table, grants)
_RLS: tuple[tuple[str, str], ...] = (
    ("wallet", "SELECT, INSERT, UPDATE"),
    ("credit_ledger", "SELECT, INSERT"),
    ("rate_cards", "SELECT, INSERT, UPDATE, DELETE"),
    ("storage_rates", "SELECT, INSERT, UPDATE, DELETE"),
    ("billing_config", "SELECT, INSERT, UPDATE"),
    ("usage_record", "SELECT, INSERT"),
)


def _rls(table: str, grants: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY p_{table} ON {table} USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})",
        f"GRANT {grants} ON {table} TO flow_app",
    )


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)
    for table, grants in _RLS:
        for stmt in _rls(table, grants):
            op.execute(stmt)


def downgrade() -> None:
    for table in (
        "usage_record",
        "billing_config",
        "storage_rates",
        "rate_cards",
        "credit_ledger",
        "wallet",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for typ in ("storage_kind", "cost_basis", "rate_unit", "ledger_entry_kind"):
        op.execute(f"DROP TYPE IF EXISTS {typ}")
