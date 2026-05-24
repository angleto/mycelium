"""Received invoices (passive cycle): storage for inbound FatturaElettronica
that SdI pushes to the accredited channel when the cedente has indicated
our channel as their CodiceDestinatario (we are the cessionario).

The active cycle (invoices we emit) lives in ``invoices``; this table is
its mirror for the passive cycle (invoices we receive). Org resolution
mirrors ``sdi_resolve_invoice_org`` (0074): the cross-org lookup runs
under ``admin_session`` via a SECURITY DEFINER ``sdi_resolve_recipient_org``
keyed on the recipient ``CodiceDestinatario`` -> the IssuerProfile holding
that code -> its ``org_id``. The actual insert then runs under a normal
``tenant_session`` so the write stays RLS-scoped to the resolved tenant.

ADR-0011 v1 declared the passive cycle deferred; this migration adds the
schema and the resolver, leaving the downstream pipeline (notify user,
build EsitoCommittente, classify the invoice in taxonomy) for a follow-up.
The minimal "store the raw XML + answer 200" is enough to let WSR01 pass
on the AdE interoperability plan and to avoid an MC bounce when a real
fattura passive arrives.

IssuerProfile gains ``codice_destinatario_ricezione`` (NULLABLE, 7 chars)
to record the codice destinatario AdE assigns when accreditation includes
the "Ricezione" service. Until the owner sets it, ``sdi_resolve_recipient_org``
returns NULL and the inbound logs the orphan + answers 200 (SdI does not
retry; the operator can backfill later).

Revision: 0103
Down revision: 0101

NB: this migration's down_revision skips 0102 because the 0102 migration
(eisenhower defaults, a separate WIP on another dev's local tree) was not
yet pushed to origin/v2.0 when this commit landed. The image build pulls
from origin and would fail with KeyError: '0102'. When the 0102 dev
pushes their migration they should rebase their down_revision to "0103"
to chain after this one (and Alembic will pick the linear order again).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0103"
down_revision: str | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


_RESOLVER = """
CREATE OR REPLACE FUNCTION sdi_resolve_recipient_org(p_codice text)
RETURNS TABLE(org_id uuid, issuer_profile_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
  SELECT org_id, id FROM issuer_profiles
  WHERE codice_destinatario_ricezione = p_codice
  LIMIT 1
$fn$
"""


UPGRADE: tuple[str, ...] = (
    # IssuerProfile gets the recipient CodiceDestinatario AdE assigns at
    # accreditation Ricezione. NULLABLE: profiles without ricezione don't
    # need it. UNIQUE (partial, only when set): a codice destinatario AdE
    # is per-channel and uniquely identifies one recipient.
    "ALTER TABLE issuer_profiles ADD COLUMN codice_destinatario_ricezione varchar(7)",
    (
        "CREATE UNIQUE INDEX uq_issuer_codice_destinatario_ricezione "
        "ON issuer_profiles (codice_destinatario_ricezione) "
        "WHERE codice_destinatario_ricezione IS NOT NULL"
    ),
    """
    CREATE TABLE received_invoices (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      issuer_profile_id uuid NOT NULL REFERENCES issuer_profiles(id) ON DELETE RESTRICT,
      identificativo_sdi varchar(64) NOT NULL,
      nome_file varchar(120) NOT NULL,
      formato_trasmissione varchar(8) NOT NULL,
      sender_id_paese varchar(2) NOT NULL,
      sender_id_codice varchar(28) NOT NULL,
      sender_denominazione varchar(200),
      codice_destinatario varchar(7) NOT NULL,
      received_at timestamptz NOT NULL DEFAULT now(),
      raw_xml bytea NOT NULL,
      processing_status varchar(20) NOT NULL DEFAULT 'new',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # IdentificativoSdI is globally unique across the channel; a duplicate
    # delivery is a SdI retry, idempotency lives here.
    "CREATE UNIQUE INDEX uq_received_invoices_idsdi ON received_invoices (identificativo_sdi)",
    "CREATE INDEX ix_received_invoices_org_id ON received_invoices (org_id)",
    "CREATE INDEX ix_received_invoices_issuer ON received_invoices (issuer_profile_id)",
    "ALTER TABLE received_invoices ENABLE ROW LEVEL SECURITY",
    # NOT FORCE: same posture as 0072 (sdi_mandates) - the table owner
    # bypasses RLS so the SECURITY DEFINER resolver can read across orgs,
    # while flow_app stays org-scoped through the policy.
    (
        f"CREATE POLICY p_received_invoices ON received_invoices "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON received_invoices TO flow_app",
    _RESOLVER,
    "GRANT EXECUTE ON FUNCTION sdi_resolve_recipient_org(text) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS sdi_resolve_recipient_org(text)",
    "DROP TABLE IF EXISTS received_invoices CASCADE",
    "DROP INDEX IF EXISTS uq_issuer_codice_destinatario_ricezione",
    "ALTER TABLE issuer_profiles DROP COLUMN IF EXISTS codice_destinatario_ricezione",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
