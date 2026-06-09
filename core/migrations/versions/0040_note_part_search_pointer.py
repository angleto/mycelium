"""Note search: pointer table linking a note part to its searchable blob.

The note-part analogue of ``0004_task_search_pointer``. Decision
2026-06-09: notes are indexed PER PART (one blob per ``note_part``), so a
part re-embeds independently when its body changes. ``text = title ||
body`` of the part; the pointer is the 1:1 binding (UNIQUE on
``blob_id``) plus a ``content_hash`` so the event-listener-driven resync
skips no-op rewrites (an ord reorder or metadata touch doesn't change
``text``).

``note_id`` is denormalised onto the pointer so the unified search
resolves a part blob back to its note in one hop and a note hard-delete
cascades to the pointer directly.

FK asymmetry mirrors the task pointer: ``part_id -> note_part(id)`` and
``note_id -> notes(id)`` are simple FKs (neither partitioned);
``(blob_id, org_id) -> memory_blobs(id, org_id)`` is composite because
``memory_blobs`` is PARTITION BY HASH (org_id). All legs cascade: a part
(or note) delete drops the pointer (the blob cleanup is the listener's
job in ``NotePart.after_delete`` / explicit ``mark_note_part_deleted``,
since a parent->child cascade alone would orphan the blob); a GDPR erase
of the blob drops the pointer (self-healing).

Org scoping: own RLS policy on ``org_id`` (the standard
``app.current_org`` predicate), same shape as ``task_index_pointer``
(not FORCE'd: matches the rest of the per-org tenant data).

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note_part_index_pointer",
        sa.Column(
            "part_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "note_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "blob_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("part_id", name="pk_note_part_index_pointer"),
        sa.UniqueConstraint("blob_id", name="uq_note_part_index_pointer_blob_id"),
        sa.ForeignKeyConstraint(
            ["part_id"],
            ["note_part.id"],
            ondelete="CASCADE",
            name="fk_note_part_index_pointer_part_id_note_part",
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["notes.id"],
            ondelete="CASCADE",
            name="fk_note_part_index_pointer_note_id_notes",
        ),
        sa.ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_note_part_index_pointer_blob_id_memory_blobs",
        ),
    )
    op.create_index(
        "ix_note_part_index_pointer_org_id",
        "note_part_index_pointer",
        ["org_id"],
    )
    op.create_index(
        "ix_note_part_index_pointer_note_id",
        "note_part_index_pointer",
        ["note_id"],
    )
    op.create_index(
        "ix_note_part_index_pointer_blob_id",
        "note_part_index_pointer",
        ["blob_id"],
    )
    op.execute("ALTER TABLE note_part_index_pointer ENABLE ROW LEVEL SECURITY")
    _org_pred = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"
    op.execute(
        f"CREATE POLICY p_note_part_index_pointer ON note_part_index_pointer "
        f"USING ({_org_pred}) WITH CHECK ({_org_pred})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_part_index_pointer TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_note_part_index_pointer ON note_part_index_pointer")
    op.drop_index("ix_note_part_index_pointer_blob_id", table_name="note_part_index_pointer")
    op.drop_index("ix_note_part_index_pointer_note_id", table_name="note_part_index_pointer")
    op.drop_index("ix_note_part_index_pointer_org_id", table_name="note_part_index_pointer")
    op.drop_table("note_part_index_pointer")
