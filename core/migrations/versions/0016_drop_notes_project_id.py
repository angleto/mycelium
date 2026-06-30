"""Drop ``notes.project_id``, junction becomes the source of truth.

Historically the note's project was tracked twice:

- ``notes.project_id`` (denormalised FK column on the Note row), and
- a project-kind tag row in ``note_tags`` when callers attached it
  explicitly through ``add_note_tag``.

The two could drift: ``create_note`` set ``project_id`` and inserted
only the *client* tag in the junction, so notes created with a
project never carried the corresponding project tag chip in the UI
(the SPA reads tags from the junction). Tasks already use the
junction-only pattern (``task_tags``, no ``tasks.project_id``); this
migration aligns notes with that model.

Backfill first: for every note whose ``project_id`` is not null,
ensure a matching ``note_tags`` row exists. Then drop the column
(and its supporting index).

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO note_tags (org_id, note_id, tag_id)
            SELECT n.org_id, n.id, n.project_id
            FROM notes n
            WHERE n.project_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM note_tags nt
                  WHERE nt.note_id = n.id AND nt.tag_id = n.project_id
              )
            """
        )
    )
    op.drop_index("ix_notes_project_id", table_name="notes")
    op.drop_column("notes", "project_id")


def downgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("project_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_notes_project_id", "notes", ["project_id"])
    # Best-effort restore: pick the project-kind tag from the
    # junction. Notes with multiple project tags (shouldn't exist by
    # contract but possible in legacy data) get the lowest tag_id by
    # ordering; the downgrade is intentionally lossy because the
    # forward direction is the single source of truth from now on.
    op.execute(
        sa.text(
            """
            UPDATE notes n
            SET project_id = sub.tag_id
            FROM (
                SELECT DISTINCT ON (nt.note_id) nt.note_id, nt.tag_id
                FROM note_tags nt
                JOIN tags t ON t.id = nt.tag_id
                WHERE t.kind = 'project'
                ORDER BY nt.note_id, nt.tag_id
            ) sub
            WHERE n.id = sub.note_id
            """
        )
    )
