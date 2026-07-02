"""Durable two-phase transmit: dispatch lease + NomeFile org resolver.

Task b6a0df8f (lost-ACK durability, ADR-0046). Two pieces:

- ``invoices.sdi_dispatch_started_at``: the dispatch lease. Phase 1 of the
  two-phase transmit commits it together with the fiscal identifiers
  (numero, ProgressivoInvio, NomeFile, frozen XML) BEFORE any byte reaches
  SdI; while the lease is fresh a concurrent transmit is refused, and after
  it expires (crash mid-dispatch) the invoice becomes retryable again
  (state=transmitted with a NULL IdentificativoSdI).

- ``sdi_resolve_invoice_org_by_filename``: the lost-ACK reconcile hook. A
  pushed notification (RC/NS/MC/...) whose IdentificativoSdI matches no
  invoice (we never saw the sync ACK) is correlated by NomeFile instead --
  possible exactly because the file name is now committed pre-dispatch.
  Same shape as ``sdi_resolve_invoice_org`` (0001 baseline): SECURITY
  DEFINER (the sdi-inbound service runs with no org GUC), pinned
  search_path, explicit grant (harden_function_acls revokes PUBLIC).

Revision ID: 0079
Revises: 0078
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("sdi_dispatch_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # Persisted proof of a resend (the retry leg stamps it): the same-ident
    # NS-00002 duplicate-echo guard is only sound when a resend happened.
    op.add_column(
        "invoices",
        sa.Column("sdi_resent_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # The SdI environment (test|production) active at the pre-dispatch commit:
    # the NomeFile-dedupe safety net only holds within ONE environment, so a
    # retry is refused when the runtime switch changed between attempts.
    op.add_column(
        "invoices",
        sa.Column("sdi_env_used", sa.String(length=16), nullable=True),
    )
    # nome_file is now a correlation key (the lost-ACK reconcile): index the
    # lookups and keep the resolver off drafts (a definite-fail revert keeps
    # the file name on a draft for reuse, but a draft was provably never
    # filed, so it must never adopt a notification).
    op.create_index(
        "ix_invoices_nome_file",
        "invoices",
        ["nome_file"],
        postgresql_where=sa.text("nome_file IS NOT NULL"),
    )
    op.execute(
        """
        CREATE FUNCTION public.sdi_resolve_invoice_org_by_filename(p_nome_file text)
        RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path TO 'public', 'pg_temp'
        AS $$
          SELECT org_id FROM invoices
          WHERE nome_file = p_nome_file AND state <> 'draft'
          ORDER BY created_at DESC
          LIMIT 1
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.sdi_resolve_invoice_org_by_filename(text) TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.sdi_resolve_invoice_org_by_filename(text)")
    op.drop_index("ix_invoices_nome_file", table_name="invoices")
    op.drop_column("invoices", "sdi_env_used")
    op.drop_column("invoices", "sdi_resent_at")
    op.drop_column("invoices", "sdi_dispatch_started_at")
