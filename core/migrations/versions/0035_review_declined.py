"""A declined proposal is a decision, not a deletion (ADR-0075).

Until now a person rejecting a generated note soft-deleted it and left
``review_state = 'proposed'``. That pair of states sat in reach of the
workspace trash purge and of the retention sweep, although the trash view
did not show it, so a rejection could be erased without anyone choosing to
erase it; and restoring it put it back where no surface would let the person
read it. This migration gives the decision a state of its own:

- ``notes.review_state`` may be ``'declined'``. The effective predicate
  (``note_effective.GATED_STATES``) withholds it exactly as it withholds
  ``'proposed'``, so nothing that was hidden becomes visible.
- ``notes.review_decided_at``: when a person last approved or declined the
  note, cleared when a declined proposal is reopened. It orders the review
  history, the role ``deleted_at`` used to play for rejections.
- ``ck_notes_review_state``: the column is a closed set. Added NOT VALID and
  validated in a second statement, so the full-table check runs under a lock
  that does not block writes; if any row holds a value outside the set the
  validation fails and names the constraint, rather than the set silently
  admitting it.
- Every proposal rejected the old way becomes ``'declined'`` with
  ``deleted_at`` cleared and its version advanced, so a client holding the
  old version is refused instead of acting on a state that changed under it.
  ``review_decided_at`` takes the moment of the rejection, which is what
  ``deleted_at`` recorded. These rows were invisible before and stay
  invisible: they move from one gated state to another.

- ``trg_note_pins_drop_on_review`` (0033) fired on entering ``'proposed'``.
  It now fires on entering the gated set from outside it, so a note that
  reaches ``'declined'`` by any path also loses its pins. The two literals
  are the ``GATED_STATES`` of ``note_effective``; a test reads this trigger
  and the CHECK back from the catalog and compares them with that tuple.

``lock_timeout`` is the repo's ten seconds: adding a nullable column and a
NOT VALID constraint takes a brief ACCESS EXCLUSIVE lock on ``notes``, and
behind an open transaction the migration fails and is retried rather than
queueing every write to ``notes`` behind it.

The downgrade turns declined proposals back into soft-deleted proposals,
which is the exact former representation, then drops the constraint and the
column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_notes_review_state"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "notes",
        sa.Column("review_decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        f"ALTER TABLE notes ADD CONSTRAINT {_CHECK} CHECK "
        "(review_state IS NULL OR review_state IN ('proposed', 'approved', 'declined')) "
        "NOT VALID"
    )
    op.execute(f"ALTER TABLE notes VALIDATE CONSTRAINT {_CHECK}")
    op.execute("DROP TRIGGER IF EXISTS trg_note_pins_drop_on_review ON notes")
    op.execute(
        """
        CREATE TRIGGER trg_note_pins_drop_on_review
        AFTER UPDATE OF review_state ON notes
        FOR EACH ROW
        WHEN (
            NEW.review_state IN ('proposed', 'declined')
            AND COALESCE(OLD.review_state, '') NOT IN ('proposed', 'declined')
        )
        EXECUTE FUNCTION note_pins_drop_on_review()
        """
    )
    op.execute(
        """
        UPDATE notes
           SET review_state = 'declined',
               review_decided_at = deleted_at,
               deleted_at = NULL,
               version = version + 1
         WHERE review_state = 'proposed'
           AND deleted_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(
        """
        UPDATE notes
           SET review_state = 'proposed',
               deleted_at = COALESCE(review_decided_at, now()),
               version = version + 1
         WHERE review_state = 'declined'
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_note_pins_drop_on_review ON notes")
    op.execute(
        """
        CREATE TRIGGER trg_note_pins_drop_on_review
        AFTER UPDATE OF review_state ON notes
        FOR EACH ROW
        WHEN (NEW.review_state = 'proposed' AND OLD.review_state IS DISTINCT FROM 'proposed')
        EXECUTE FUNCTION note_pins_drop_on_review()
        """
    )
    op.execute(f"ALTER TABLE notes DROP CONSTRAINT {_CHECK}")
    op.drop_column("notes", "review_decided_at")
