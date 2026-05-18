"""F7a (additive): Italian electronic invoicing (docs/adr/0009, 0010,
0011, FR-9). Issuer fiscal profile, invoices (immutable after
emission), lines, and a concurrency-safe per-(org, series, year)
number counter. RLS+FORCE + flow_app grants, same patterns as 0011.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = (
    "org_fiscal_profile",
    "invoices",
    "invoice_lines",
    "invoice_counters",
)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE conservation_adhesion AS ENUM ('none', 'requested', 'active')",
    "CREATE TYPE invoice_kind AS ENUM ('invoice', 'credit_note')",
    "CREATE TYPE document_type AS ENUM ('TD01', 'TD04')",
    """
    CREATE TYPE invoice_state AS ENUM
      ('draft', 'transmitted', 'delivered', 'accepted', 'rejected')
    """,
    """
    CREATE TYPE sdi_status AS ENUM
      ('none', 'RC', 'MC', 'NS', 'AT')
    """,
    "CREATE TYPE payment_status AS ENUM ('unpaid', 'paid')",
    """
    CREATE TYPE conservation_status AS ENUM
      ('out_of_coverage', 'ade_pending', 'ade_covered')
    """,
    """
    CREATE TABLE org_fiscal_profile (
      org_id uuid PRIMARY KEY,
      regime_fiscale varchar(4) NOT NULL DEFAULT 'RF01',
      paese varchar(2) NOT NULL DEFAULT 'IT',
      piva varchar(28),
      codice_fiscale varchar(16),
      denominazione varchar(200) NOT NULL,
      indirizzo varchar(200) NOT NULL DEFAULT '',
      cap varchar(10) NOT NULL DEFAULT '',
      comune varchar(120) NOT NULL DEFAULT '',
      provincia varchar(4),
      nazione varchar(2) NOT NULL DEFAULT 'IT',
      rea varchar(40),
      conservation_adhesion conservation_adhesion NOT NULL DEFAULT 'none',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE invoice_counters (
      org_id uuid NOT NULL,
      series varchar(20) NOT NULL,
      year integer NOT NULL,
      last_number integer NOT NULL DEFAULT 0,
      CONSTRAINT pk_invoice_counters PRIMARY KEY (org_id, series, year)
    )
    """,
    """
    CREATE TABLE invoices (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      client_tag_id uuid NOT NULL,
      kind invoice_kind NOT NULL DEFAULT 'invoice',
      document_type document_type NOT NULL DEFAULT 'TD01',
      parent_invoice_id uuid REFERENCES invoices(id) ON DELETE RESTRICT,
      series varchar(20) NOT NULL DEFAULT 'A',
      year integer NOT NULL,
      number integer,
      state invoice_state NOT NULL DEFAULT 'draft',
      currency varchar(3) NOT NULL DEFAULT 'EUR',
      causale varchar(200),
      taxable numeric(14, 2) NOT NULL DEFAULT 0,
      vat numeric(14, 2) NOT NULL DEFAULT 0,
      total numeric(14, 2) NOT NULL DEFAULT 0,
      identificativo_sdi varchar(40),
      sdi_status sdi_status NOT NULL DEFAULT 'none',
      payment_status payment_status NOT NULL DEFAULT 'unpaid',
      conservation_status conservation_status NOT NULL DEFAULT 'out_of_coverage',
      xml text,
      issued_at timestamptz,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_invoices_org_id UNIQUE (org_id, series, year, number)
    )
    """,
    "CREATE INDEX ix_invoices_org_id ON invoices (org_id)",
    "CREATE INDEX ix_invoices_identificativo_sdi ON invoices (identificativo_sdi)",
    "CREATE INDEX ix_invoices_client_tag_id ON invoices (client_tag_id)",
    """
    CREATE TABLE invoice_lines (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
      line_no integer NOT NULL,
      description varchar(1000) NOT NULL,
      quantity numeric(12, 4) NOT NULL DEFAULT 1,
      unit_price numeric(14, 4) NOT NULL,
      vat_rate numeric(5, 2) NOT NULL DEFAULT 22,
      natura varchar(4),
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_invoice_lines_invoice_id UNIQUE (invoice_id, line_no)
    )
    """,
    "CREATE INDEX ix_invoice_lines_org_id ON invoice_lines (org_id)",
    "CREATE INDEX ix_invoice_lines_invoice_id ON invoice_lines (invoice_id)",
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
    for table in (
        "invoice_lines",
        "invoices",
        "invoice_counters",
        "org_fiscal_profile",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for typ in (
        "conservation_status",
        "payment_status",
        "sdi_status",
        "invoice_state",
        "document_type",
        "invoice_kind",
        "conservation_adhesion",
    ):
        op.execute(f"DROP TYPE IF EXISTS {typ}")
