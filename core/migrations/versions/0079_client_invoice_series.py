"""Per-client invoice sezionale: client_profile.invoice_series.

Additive nullable column holding the series prefix used for a client's
invoices (a sezionale per cliente, so each client owns an independent
progressive sequence under numbering keyed by issuer+series, migration 0078).
NULL on existing rows; resolved (derived + made unique) lazily at the first
draft for that client.

Revision: 0079
Down revision: 0078
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE client_profile ADD COLUMN invoice_series varchar(20)")


def downgrade() -> None:
    op.execute("ALTER TABLE client_profile DROP COLUMN IF EXISTS invoice_series")
