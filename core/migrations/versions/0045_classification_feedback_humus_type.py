"""Allow ``suggestion_type='humus'`` on classification_feedback (WS-F2).

The autonomous ``humus_flag`` flip in ``distill_note`` mutates a note
without a trace (no revision, no audit, no feedback row), violating the
§12 invariant "every mutation is tracked". WS-F2 routes that flip through
``audit.log`` (action ``auto_humus``) plus an append-only feedback row, so
it becomes auditable and replayable by the learning loop like every other
system-initiated decision (``action='auto'``).

The feedback row carries ``suggestion_type='humus'`` -- a new, system-only
kind alongside ``cluster`` (also a no-op in ``_mutate``). This extends the
CHECK constraint; the model's ``SUGGESTION_TYPES`` frozenset is the mirror.

The original constraint (migration 0021) was created with an explicit name
that the metadata naming convention then double-prefixed and hash-truncated
(``ck_classification_feedback_ck_classification_feedback_s_...``). Rather
than depend on that fragile generated name, the swap locates the existing
suggestion_type CHECK by its definition and replaces it with a cleanly
named one.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_classification_feedback_suggestion_type"

# Drop whichever CHECK constraint currently governs suggestion_type
# (found by its definition, immune to the generated-name drift) and add a
# cleanly named replacement with the given value set.
_DROP_EXISTING = """
DO $$
DECLARE cname text;
BEGIN
  SELECT conname INTO cname
    FROM pg_constraint
   WHERE conrelid = 'classification_feedback'::regclass
     AND contype = 'c'
     AND pg_get_constraintdef(oid) ILIKE '%suggestion_type%';
  IF cname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE classification_feedback DROP CONSTRAINT %I', cname);
  END IF;
END $$;
"""


def _set_values(values: str) -> None:
    op.execute(_DROP_EXISTING)
    op.execute(
        f"ALTER TABLE classification_feedback ADD CONSTRAINT {_NAME} "
        f"CHECK (suggestion_type IN ({values}))"
    )


def upgrade() -> None:
    _set_values("'tag','link','maturity','cluster','humus'")


def downgrade() -> None:
    # No 'humus' rows exist before this migration; if a downgrade is run
    # after they do, drop them so the narrower constraint can re-apply.
    op.execute("DELETE FROM classification_feedback WHERE suggestion_type = 'humus'")
    _set_values("'tag','link','maturity','cluster'")
