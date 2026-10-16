"""Clear the client codici fiscali that cannot be codici fiscali.

Until this release nothing validated a client's codice fiscale. A payment
connector copied it from the provider's customer metadata, and a ``0`` typed
there to mean "none" -- or a partita IVA with its ``IT`` prefix, or a
wrong-length typo -- became the client's tax code. It satisfied the "VAT
number or tax code" completeness gate because it is not empty, and failed
only when a document was built from it: as an XSD pattern error, or, for the
well-shaped ``IT`` + 11 digits, at SdI. Every write path now refuses such a
value and the connector treats it as absent; this clears the rows written
before they did.

WHICH ROWS. The rule ``mycelium_core.tax_code`` applies on every write,
judged by the counterpart's country (address country, else VAT country,
else Italian):

- Italian: a code that, whitespace removed and upper-cased, has neither form
  a codice fiscale can take, the 16-character personal code (omocodia
  substitutions included) or 11 digits;
- foreign: a code that fails the XSD facet ``[A-Z0-9]{11,16}``, since AdE
  FAQ 63 lets a foreign consumer's CodiceFiscale carry the foreign id.

The check characters are NOT recomputed here; the pre-transmit validation
refuses a stored code that fails them, so a well-shaped code with a wrong
check character is named at transmit rather than cleared silently.

Measured on production before writing this (read-only, 2026-10-16): 11 rows,
all in one workspace, 9 Italian and 2 foreign (one Swiss holding ``0``, one
Belgian holding ten characters; both fail the XSD facet as well). Five hold
``0``, four ``IT`` plus 11 digits, one ten characters, one seventeen. Ten of
the eleven clients also carry a partita IVA, which identifies them on its
own; the eleventh held only ``0``, which never identified anybody. No issuer
profile held a malformed code.

CLEARED, NOT CORRECTED. ``IT`` + 11 digits looks recoverable as a numeric
code, but a company's codice fiscale is not always its partita IVA, and a
guessed identifier on a fiscal document is worse than an absent one. The
provider record still holds the original, and a person can type the right
value; ``fill_client_gaps`` will not, because it now refuses the same value.

Idempotent: a cleared row is NULL and no longer matches.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The forms, spelled as in ``mycelium_core.tax_code`` and kept literal here: a
# migration must not import code that a later release may change.
_PERSON = "^[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]$"
_NUMERIC = "^[0-9]{11}$"
_XSD = "^[A-Z0-9]{11,16}$"

CLEAR_SQL = rf"""
WITH judged AS (
    SELECT tag_id,
           upper(regexp_replace(tax_code, '\s', '', 'g')) AS code,
           upper(coalesce(nullif(country, ''), nullif(country_code, ''), 'IT')) = 'IT'
               AS italian
      FROM client_profile
     WHERE tax_code IS NOT NULL
)
UPDATE client_profile AS c
   SET tax_code = NULL,
       version = c.version + 1,
       updated_at = now()
  FROM judged AS j
 WHERE j.tag_id = c.tag_id
   AND CASE
         WHEN j.italian THEN NOT (j.code ~ '{_PERSON}' OR j.code ~ '{_NUMERIC}')
         ELSE NOT (j.code ~ '{_XSD}')
       END
"""


def upgrade() -> None:
    op.execute(CLEAR_SQL)


def downgrade() -> None:
    # Deliberately empty. The cleared values were not codici fiscali, and the
    # migration does not keep them: restoring them would restore the defect.
    pass
