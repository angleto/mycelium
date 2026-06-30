"""Recover note_part(ord=0) from entity_revision snapshots (incident
2026-05-27, task 1cd8bc0a).

Background: migration 0011 introduced ``note_part`` and backfilled
each note's transcript into ``note_part(ord=0)``. The backfill ran
without bracketing the ``INSERT...SELECT FROM notes`` in a
NO FORCE / FORCE RLS pair; since the migration role (``mycelium``)
sees ``FORCE ROW LEVEL SECURITY`` like everyone else, and there is
no ``app.current_org`` GUC during a migration run, the SELECT
returned zero rows -- silently. Migration 0012 then dropped the
``transcript`` column, and the SPA / API surfaced empty bodies.

The data is still recoverable from ``entity_revision`` (every note
edit was snapshotted; the JSON ``snapshot.transcript`` carries the
body). This migration re-runs the backfill using the same NO FORCE
trick the fixed 0011 now applies, and falls back to the most recent
(sealed-or-not) revision's transcript per note.

Idempotent: the INSERT is guarded by NOT EXISTS, so a re-run after
the in-place recovery script already populated the table is a
no-op. Notes that have no eligible revision (created before
revision logging, kind='voice' with no transcript, ...) stay
without a part; the SPA already handles an empty body.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Relax FORCE RLS so the migration role (table owner) can SELECT
    # across orgs without an ``app.current_org`` GUC. Same trick the
    # patched 0011 backfill now uses.
    op.execute("ALTER TABLE notes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entity_revision NO FORCE ROW LEVEL SECURITY")
    try:
        # Most-recent revision per note (sealed OR open); reach for
        # ``snapshot->>'transcript'`` and insert as part(ord=0) iff
        # the note doesn't already have one. The NOT EXISTS makes
        # this idempotent with the in-place recovery script run on
        # 2026-05-27.
        op.execute(
            """
            WITH latest_rev AS (
                SELECT DISTINCT ON (entity_id)
                    entity_id AS note_id,
                    snapshot,
                    org_id
                FROM entity_revision
                WHERE entity_kind = 'note'
                ORDER BY entity_id,
                         COALESCE(sealed_at, last_edit_at) DESC NULLS LAST
            )
            INSERT INTO note_part (org_id, note_id, ord, body)
            SELECT n.org_id, n.id, 0, lr.snapshot->>'transcript'
              FROM notes n
              JOIN latest_rev lr ON lr.note_id = n.id
             WHERE lr.snapshot ? 'transcript'
               AND lr.snapshot->>'transcript' IS NOT NULL
               AND lr.snapshot->>'transcript' <> ''
               AND NOT EXISTS (
                 SELECT 1 FROM note_part np
                  WHERE np.note_id = n.id AND np.ord = 0
               )
            """
        )
    finally:
        op.execute("ALTER TABLE entity_revision FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE note_part FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE notes FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # No-op: a fresh part(ord=0) rebuilt from a snapshot is
    # indistinguishable from a part written through the regular API.
    # The right downgrade is migration 0012's roll-back (re-add the
    # transcript column + re-fill from parts), which 0012 already
    # handles. Trying to delete the parts we wrote here would
    # remove user content blindly.
    pass
