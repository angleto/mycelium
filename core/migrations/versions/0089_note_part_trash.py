"""Restorable delete for note parts: the ``note_part_trash`` side table.

``delete_note_part`` was the only destructive note operation with no
inverse: it dropped the search blob and the row, leaving the body
recoverable from nowhere (a note revision restores ``title`` /
``transcript`` only, never part structure). That irreversibility is why
it sits on the ``delete:notes`` danger key, which in turn made the whole
capability unreachable for an ordinary assistant (``DEFAULT_SCOPES`` is
reads-only). The fix is the pair every sibling entity already has --
trash + restore on the ordinary write key -- with the hard purge left
where the scope taxonomy put it.

Why a side table and not a ``deleted_at`` tombstone on ``note_part``:
``uq_note_part_note_id_ord`` must stay ``DEFERRABLE`` because
``create_part``'s insert-at-ord shifts a whole range of rows in one
UPDATE, which an IMMEDIATE index rejects row-by-row. PostgreSQL has no
partial UNIQUE *constraint* (only partial unique *indexes*, which cannot
be deferrable), so a tombstone would need trashed rows parked in a
disjoint ord space plus ``deleted_at IS NULL`` filters at every read
site -- where a single miss silently corrupts ``get_body`` and the
memory index. The side table touches no read path and no constraint.

The trashed row keeps the ORIGINAL part id, so a restore is identity-
preserving: deep links, annotations and blob provenance that referenced
the part resolve again after the round trip.

Revision ID: 0089
Revises: 0088
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "note_part_trash",
        # Not gen_random_uuid(): the id is the trashed part's own id,
        # supplied by the service so restore is identity-preserving.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Deleting the note discards its trashed parts too: restoring a
        # part into a note that no longer exists is not a thing.
        sa.Column(
            "note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The ord the part held when it was trashed. Restore aims for it
        # and shifts the survivors aside when the slot got taken.
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("lang", sa.String(16), nullable=True),
        sa.Column("merged_from_note_id", postgresql.UUID(as_uuid=True), nullable=True),
        # The version the part carried when trashed. Restore resumes the
        # sequence from here so a stale ``expected_version`` held across
        # the round trip still loses, as it must.
        sa.Column("part_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "trashed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # No FK: an actor row may be purged (GDPR) while the trash entry
        # is still restorable, and the attribution is audit sugar.
        sa.Column("trashed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_note_part_trash_note_id",
        "note_part_trash",
        ["note_id", "trashed_at"],
    )
    op.create_index("ix_note_part_trash_org_id", "note_part_trash", ["org_id"])

    op.execute("ALTER TABLE note_part_trash ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_part_trash FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_note_part_trash ON note_part_trash "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_part_trash TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_note_part_trash ON note_part_trash")
    op.drop_index("ix_note_part_trash_org_id", table_name="note_part_trash")
    op.drop_index("ix_note_part_trash_note_id", table_name="note_part_trash")
    op.drop_table("note_part_trash")
