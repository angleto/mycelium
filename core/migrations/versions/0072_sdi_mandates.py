"""SdI transmission mandate (docs/adr/0011, FR-9).

The per-issuer-profile (per VAT subject) authorization a tenant grants Flow
to transmit its invoices through the single accredited SdICoop channel
(Flow as intermediary). At most one active mandate per issuer profile
(partial unique index); revoke is a status flip, not a delete (audited
history).

RLS posture: tenant org-scoped data, written and read only under a
tenant_session (the org owner grants/revokes; ``invoice.transmit`` checks
it). ENABLE + org policy + flow_app grants; deliberately NOT FORCE -- the
same posture as 0068/0069/0071, since nothing reads this table from
admin_session or a SECURITY DEFINER function (the #48/#125 lesson).

Revision: 0072
Down revision: 0071
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    "CREATE TYPE sdi_mandate_status AS ENUM ('active', 'revoked')",
    """
    CREATE TABLE sdi_mandates (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      issuer_profile_id uuid NOT NULL REFERENCES issuer_profiles(id) ON DELETE CASCADE,
      status sdi_mandate_status NOT NULL DEFAULT 'active',
      scope varchar(40) NOT NULL DEFAULT 'transmit',
      reference varchar(200),
      granted_at timestamptz NOT NULL DEFAULT now(),
      revoked_at timestamptz,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_sdi_mandates_org_id ON sdi_mandates (org_id)",
    "CREATE INDEX ix_sdi_mandates_issuer_profile_id ON sdi_mandates (issuer_profile_id)",
    (
        "CREATE UNIQUE INDEX uq_sdi_mandates_active ON sdi_mandates (issuer_profile_id) "
        "WHERE status = 'active'"
    ),
    "ALTER TABLE sdi_mandates ENABLE ROW LEVEL SECURITY",
    (
        f"CREATE POLICY p_sdi_mandates ON sdi_mandates "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON sdi_mandates TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS sdi_mandates CASCADE",
    "DROP TYPE IF EXISTS sdi_mandate_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
