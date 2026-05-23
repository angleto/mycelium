"""Configurable payment terms / methods, issuer contacts.

Three thin column groups added across issuer / client / invoice:

* ``issuer_profiles``: ``pec``, ``email``, ``telefono``, ``fax`` (PDF +
  optional ContattiTrasmittente), plus ``default_condizioni_pagamento``,
  ``default_modalita_pagamento``, ``default_payment_terms_days`` (fallback
  used when the client carries no own default).
* ``client_profile``: ``default_condizioni_pagamento``,
  ``default_modalita_pagamento``, ``default_payment_terms_days``, plus
  ``invoice_language`` (BCP47 tag for the courtesy PDF only; FatturaPA
  XML stays in Italian, see ``invoice_pdf``).
* ``invoices``: per-document overrides ``condizioni_pagamento``,
  ``modalita_pagamento``, ``payment_terms_days``.

Resolution precedence (in code) is invoice > client > issuer > system
defaults (TP02 / MP05); NULL columns mean "inherit". All columns are
additive nullable, no backfill (existing drafts keep their behaviour: the
hardcoded TP02/MP05 path is replaced by the resolver that falls through
to those same defaults on NULL).

Revision: 0080
Down revision: 0079
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE issuer_profiles
            ADD COLUMN pec varchar(320),
            ADD COLUMN email varchar(320),
            ADD COLUMN telefono varchar(20),
            ADD COLUMN fax varchar(20),
            ADD COLUMN default_condizioni_pagamento varchar(4),
            ADD COLUMN default_modalita_pagamento varchar(4),
            ADD COLUMN default_payment_terms_days integer
        """
    )
    op.execute(
        """
        ALTER TABLE client_profile
            ADD COLUMN default_condizioni_pagamento varchar(4),
            ADD COLUMN default_modalita_pagamento varchar(4),
            ADD COLUMN default_payment_terms_days integer,
            ADD COLUMN invoice_language varchar(8)
        """
    )
    op.execute(
        """
        ALTER TABLE invoices
            ADD COLUMN condizioni_pagamento varchar(4),
            ADD COLUMN modalita_pagamento varchar(4),
            ADD COLUMN payment_terms_days integer
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE invoices
            DROP COLUMN IF EXISTS condizioni_pagamento,
            DROP COLUMN IF EXISTS modalita_pagamento,
            DROP COLUMN IF EXISTS payment_terms_days
        """
    )
    op.execute(
        """
        ALTER TABLE client_profile
            DROP COLUMN IF EXISTS default_condizioni_pagamento,
            DROP COLUMN IF EXISTS default_modalita_pagamento,
            DROP COLUMN IF EXISTS default_payment_terms_days,
            DROP COLUMN IF EXISTS invoice_language
        """
    )
    op.execute(
        """
        ALTER TABLE issuer_profiles
            DROP COLUMN IF EXISTS pec,
            DROP COLUMN IF EXISTS email,
            DROP COLUMN IF EXISTS telefono,
            DROP COLUMN IF EXISTS fax,
            DROP COLUMN IF EXISTS default_condizioni_pagamento,
            DROP COLUMN IF EXISTS default_modalita_pagamento,
            DROP COLUMN IF EXISTS default_payment_terms_days
        """
    )
