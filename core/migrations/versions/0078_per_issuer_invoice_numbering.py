"""Per-issuer invoice numbering: re-key invoice_counters by issuer_profile_id.

The progressive number that identifies an invoice "in modo univoco" (DPR 633/72
art.21 c.2) belongs to the cedente/prestatore, i.e. the VAT subject, not the
org. Keying the counter by ``(org_id, series, year)`` was wrong for an org
holding several P.IVA (issuer profiles): two VAT subjects would interleave a
single sequence. Re-key to ``(issuer_profile_id, series, year)``.

The old org-keyed rows cannot be split per issuer reliably, so they are
discarded and the per-issuer counters are reseeded from the authoritative
source: ``MAX(number)`` over already-numbered invoices, grouped by issuer.
Each issuer's sequence therefore resumes exactly where its last emitted
document left off (immutable invoices are never renumbered).

Revision: 0078
Down revision: 0077
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# NB: org_id is NOT dropped. The table has FORCE RLS with a policy
# ``USING/WITH CHECK (org_id = current_org)``, so org_id must stay to keep
# tenant isolation; it is just no longer part of the numbering key. The new
# counter rows are reseeded with org_id (functionally determined by the
# issuer) so the policy's WITH CHECK still holds for runtime inserts.
UPGRADE: tuple[str, ...] = (
    "ALTER TABLE invoice_counters ADD COLUMN issuer_profile_id uuid",
    "ALTER TABLE invoice_counters DROP CONSTRAINT pk_invoice_counters",
    "DELETE FROM invoice_counters",
    """
    INSERT INTO invoice_counters (org_id, issuer_profile_id, series, year, last_number)
    SELECT org_id, issuer_profile_id, series, year, MAX(number)
    FROM invoices
    WHERE number IS NOT NULL AND issuer_profile_id IS NOT NULL
    GROUP BY org_id, issuer_profile_id, series, year
    """,
    "ALTER TABLE invoice_counters ALTER COLUMN issuer_profile_id SET NOT NULL",
    "ALTER TABLE invoice_counters ADD CONSTRAINT pk_invoice_counters "
    "PRIMARY KEY (issuer_profile_id, series, year)",
    # Invoice-number uniqueness follows the counter: per issuer, not per org
    # (two VAT subjects in one org may legitimately share series/year/number).
    "ALTER TABLE invoices DROP CONSTRAINT uq_invoices_org_id",
    "ALTER TABLE invoices ADD CONSTRAINT uq_invoices_issuer "
    "UNIQUE (issuer_profile_id, series, year, number)",
)


DOWNGRADE: tuple[str, ...] = (
    # NB: if per-issuer numbering produced two invoices sharing
    # (org, series, year, number), restoring the org-wide unique constraint
    # below will fail -- that collision is exactly what the upgrade allows.
    "ALTER TABLE invoices DROP CONSTRAINT uq_invoices_issuer",
    "ALTER TABLE invoices ADD CONSTRAINT uq_invoices_org_id UNIQUE (org_id, series, year, number)",
    "ALTER TABLE invoice_counters DROP CONSTRAINT pk_invoice_counters",
    "DELETE FROM invoice_counters",
    "ALTER TABLE invoice_counters DROP COLUMN issuer_profile_id",
    """
    INSERT INTO invoice_counters (org_id, series, year, last_number)
    SELECT org_id, series, year, MAX(number)
    FROM invoices
    WHERE number IS NOT NULL
    GROUP BY org_id, series, year
    """,
    "ALTER TABLE invoice_counters ADD CONSTRAINT pk_invoice_counters "
    "PRIMARY KEY (org_id, series, year)",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
