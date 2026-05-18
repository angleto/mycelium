"""Multi issuer profiles (the invoice "intestazione").

Replaces the single-row ``org_fiscal_profile`` with ``issuer_profiles``:
an org can hold several billing identities (ditta individuale, a
controlled SRL, ...), exactly one flagged default (partial unique
index), pre-selected at draft creation and selectable per invoice.
``invoices.issuer_profile_id`` records the chosen identity; the emitted
header is frozen in ``invoices.xml`` at transmit (ADR-0009), so editing
or removing a profile later never mutates an already-emitted document.

The existing per-org profile is migrated into one default
``issuer_profiles`` row per org. RLS is toggled off on the source/target
only for the in-migration backfill (owner-permitted, role-independent;
the local owner happens to also bypass RLS, production may not).

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    # The source has FORCE RLS; the owner runs migrations. Disable RLS
    # for the cross-table backfill, re-enable on the survivors after.
    "ALTER TABLE org_fiscal_profile DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE invoices DISABLE ROW LEVEL SECURITY",
    """
    CREATE TABLE issuer_profiles (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      label varchar(120) NOT NULL DEFAULT 'Principale',
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
      is_default boolean NOT NULL DEFAULT false,
      conservation_adhesion conservation_adhesion NOT NULL DEFAULT 'none',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_issuer_profiles_org_id ON issuer_profiles (org_id)",
    (
        "CREATE UNIQUE INDEX uq_issuer_profiles_default "
        "ON issuer_profiles (org_id) WHERE is_default"
    ),
    # Migrate the single per-org profile into one default identity.
    """
    INSERT INTO issuer_profiles
      (org_id, label, regime_fiscale, paese, piva, codice_fiscale,
       denominazione, indirizzo, cap, comune, provincia, nazione, rea,
       conservation_adhesion, is_default, version, created_at, updated_at)
    SELECT
      org_id, 'Principale', regime_fiscale, paese, piva, codice_fiscale,
      denominazione, indirizzo, cap, comune, provincia, nazione, rea,
      conservation_adhesion, true, 1, created_at, updated_at
    FROM org_fiscal_profile
    """,
    (
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS issuer_profile_id uuid "
        "REFERENCES issuer_profiles(id) ON DELETE RESTRICT"
    ),
    """
    UPDATE invoices i
       SET issuer_profile_id = p.id
      FROM issuer_profiles p
     WHERE p.org_id = i.org_id AND p.is_default
    """,
    "DROP TABLE org_fiscal_profile CASCADE",
    "ALTER TABLE issuer_profiles ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE issuer_profiles FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_issuer_profiles ON issuer_profiles "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON issuer_profiles TO flow_app",
    "ALTER TABLE invoices ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE invoices FORCE ROW LEVEL SECURITY",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE invoices DISABLE ROW LEVEL SECURITY",
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
    INSERT INTO org_fiscal_profile
      (org_id, regime_fiscale, paese, piva, codice_fiscale, denominazione,
       indirizzo, cap, comune, provincia, nazione, rea,
       conservation_adhesion, version, created_at, updated_at)
    SELECT
      org_id, regime_fiscale, paese, piva, codice_fiscale, denominazione,
      indirizzo, cap, comune, provincia, nazione, rea,
      conservation_adhesion, 1, created_at, updated_at
    FROM issuer_profiles WHERE is_default
    """,
    "ALTER TABLE invoices DROP COLUMN IF EXISTS issuer_profile_id",
    "DROP TABLE IF EXISTS issuer_profiles CASCADE",
    "ALTER TABLE org_fiscal_profile ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE org_fiscal_profile FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_org_fiscal_profile ON org_fiscal_profile "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON org_fiscal_profile TO flow_app",
    "ALTER TABLE invoices ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE invoices FORCE ROW LEVEL SECURITY",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
