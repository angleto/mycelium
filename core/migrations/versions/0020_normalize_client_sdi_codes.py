"""Normalize the client codici destinatario, and clear the ones a clamp forged.

Until this release nothing normalized a client's codice destinatario. Two
defects followed, both measured on production on 2026-10-16:

- A code typed in lower case on a Stripe customer reached the client record
  verbatim, and the document built from it failed the XSD
  (``[A-Z0-9]{6,7}``). The facet admits no lower case and SdI issues
  upper-case codes, so upper-casing it IS the correct value: one row.
- A code typed with eight characters was clamped to seven on its way in
  (``clamp_field``), which produces a well-formed code naming a DIFFERENT
  recipient -- SdI would deliver the invoice to whoever owns it. There is no
  correct shorter form of an over-long code, so a forged one is cleared: the
  counterpart then needs a real code or its PEC, and the payment says so.

Every write path now normalizes and refuses an invalid code, and the
connector drops one rather than clamping it; this repairs the rows written
before they did.

WHICH ROWS.

1. A code that differs from its whitespace-stripped, upper-cased form, where
   that form passes the facet: rewritten to it.
2. A code that fails the facet even after that: cleared. It cannot be used to
   deliver and has no repair.
3. A code equal to the first seven characters of a LONGER code that the
   linked provider customer states in its latest ``customer.*`` event, under
   any of the connector's ``metadata_sdi_keys`` (matched case- and
   separator-insensitively, as ``first_present`` does): cleared. Only Stripe
   customer events carry that metadata, which is where the clamp ran.

Measured on production before writing this (read-only, 2026-10-16, with
this file's own SQL turned into counts): step 3 matches 1 row, steps 1-2
match 1 row to rewrite and none to clear. The counts do not say whether the
forged code is also the lower-case one; the order makes the outcome the same
either way, because step 3 clears it before step 1 could look at it.

The ``version`` bump is what the service's own writes do, so an editor holding
the old version is refused rather than silently reverting the change.

Idempotent: a normalized row no longer differs from its normal form, a
cleared one is NULL.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACET = "^[A-Z0-9]{6,7}$"

#: Step 3 runs FIRST: it compares the stored code with the provider's, and
#: step 1 must not normalize a forged code into something that no longer
#: matches the prefix it was cut from.
CLEAR_FORGED_SQL = r"""
WITH latest AS (
    SELECT DISTINCT ON (e.connector_id, e.payload -> 'data' -> 'object' ->> 'id')
           e.connector_id,
           e.payload -> 'data' -> 'object' ->> 'id' AS customer_id,
           e.payload -> 'data' -> 'object' -> 'metadata' AS metadata
      FROM payment_connector_events AS e
     WHERE e.event_type IN ('customer.created', 'customer.updated')
     ORDER BY e.connector_id, e.payload -> 'data' -> 'object' ->> 'id', e.occurred_at DESC
),
stated AS (
    SELECT l.client_tag_id,
           upper(regexp_replace(kv.value, '\s', '', 'g')) AS code
      FROM latest AS x
      JOIN payment_customer_links AS l
        ON l.connector_id = x.connector_id AND l.provider_customer_id = x.customer_id
      JOIN payment_connectors AS c ON c.id = x.connector_id
     CROSS JOIN LATERAL jsonb_each_text(
           CASE WHEN jsonb_typeof(x.metadata) = 'object' THEN x.metadata ELSE '{}'::jsonb END
       ) AS kv
     WHERE lower(regexp_replace(kv.key, '[^a-zA-Z0-9]', '', 'g')) IN (
           SELECT lower(regexp_replace(k, '[^a-zA-Z0-9]', '', 'g'))
             FROM unnest(c.metadata_sdi_keys) AS k
       )
)
UPDATE client_profile AS p
   SET sdi_code = NULL,
       version = p.version + 1,
       updated_at = now()
  FROM stated AS s
 WHERE s.client_tag_id = p.tag_id
   AND p.sdi_code IS NOT NULL
   AND length(s.code) > 7
   AND upper(regexp_replace(p.sdi_code, '\s', '', 'g')) = left(s.code, 7)
"""

NORMALIZE_SQL = rf"""
UPDATE client_profile
   SET sdi_code = CASE
           WHEN upper(regexp_replace(sdi_code, '\s', '', 'g')) ~ '{_FACET}'
           THEN upper(regexp_replace(sdi_code, '\s', '', 'g'))
       END,
       version = version + 1,
       updated_at = now()
 WHERE sdi_code IS NOT NULL
   AND (
         sdi_code <> upper(regexp_replace(sdi_code, '\s', '', 'g'))
         OR NOT (sdi_code ~ '{_FACET}')
       )
"""


def upgrade() -> None:
    op.execute(CLEAR_FORGED_SQL)
    op.execute(NORMALIZE_SQL)


def downgrade() -> None:
    # Deliberately empty. A normalized code is the same code, and a cleared one
    # either named another recipient or could not be delivered to at all.
    pass
