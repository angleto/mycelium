"""Generalise comments into markdown-document annotations (comment | suggestion).

The former task-only ``comments`` table becomes the annotation layer for
any markdown document: a note-part body or a task description, addressed
by the typed FKs ``task_id`` / ``note_part_id`` under a ``doc_kind``
discriminator (XOR CHECK), so ``ON DELETE CASCADE`` integrity survives.

Adds ``kind`` (comment|suggestion), W3C quote anchors
(``anchor_quote``/``prefix``/``suffix``; NULL quote = a whole-document /
work-diary entry), a suggestion payload (``original_text`` ->
``proposed_text``), a soft lifecycle (``status`` open|resolved|accepted|
rejected, ``resolved_at``, ``edited_at``, ``deleted_at``), a
self-referential ``parent_id`` for replies, and Identity authorship
(``author_identity_id`` / ``resolved_by_identity_id``) replacing the
human-only ``user_id`` (backfilled from it, then dropped).

The table name is kept (no rename) for migration safety, exactly as
migration 0020 kept ``task_checklist_items``.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- new columns: nullable / server-defaulted so existing rows stay valid
    op.add_column("comments", sa.Column("doc_kind", sa.String(32), nullable=True))
    op.add_column(
        "comments", sa.Column("note_part_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "comments",
        sa.Column("kind", sa.String(16), nullable=False, server_default="comment"),
    )
    op.add_column("comments", sa.Column("anchor_quote", sa.Text(), nullable=True))
    op.add_column("comments", sa.Column("anchor_prefix", sa.Text(), nullable=True))
    op.add_column("comments", sa.Column("anchor_suffix", sa.Text(), nullable=True))
    op.add_column("comments", sa.Column("original_text", sa.Text(), nullable=True))
    op.add_column("comments", sa.Column("proposed_text", sa.Text(), nullable=True))
    op.add_column(
        "comments",
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )
    op.add_column("comments", sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "comments", sa.Column("author_identity_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "comments",
        sa.Column("resolved_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("comments", sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("comments", sa.Column("edited_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("comments", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_foreign_key(
        "fk_comments_note_part_id_note_part",
        "comments",
        "note_part",
        ["note_part_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_comments_parent_id_comments",
        "comments",
        "comments",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_comments_author_identity_id_identities",
        "comments",
        "identities",
        ["author_identity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_comments_resolved_by_identity_id_identities",
        "comments",
        "identities",
        ["resolved_by_identity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_comments_note_part_id",
        "comments",
        ["note_part_id"],
        postgresql_where=sa.text("note_part_id IS NOT NULL"),
    )
    op.create_index(
        "ix_comments_parent_id",
        "comments",
        ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )

    # task_id was NOT NULL (task-only); note-part annotations leave it NULL.
    op.alter_column(
        "comments", "task_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True
    )

    # --- backfill: FORCE RLS + no GUC fails closed, so drop FORCE for the
    #     duration (the 2026-05-27 incident / migration 0011 pattern).
    op.execute("ALTER TABLE comments NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE identities NO FORCE ROW LEVEL SECURITY")
    try:
        # every pre-existing row is a task comment
        op.execute("UPDATE comments SET doc_kind = 'task_description' WHERE doc_kind IS NULL")
        # map the legacy human author to its user Identity
        op.execute(
            """
            UPDATE comments c
               SET author_identity_id = i.id
              FROM identities i
             WHERE i.org_id = c.org_id
               AND i.user_id = c.user_id
               AND i.kind = 'user'
               AND c.user_id IS NOT NULL
               AND c.author_identity_id IS NULL
            """
        )
    finally:
        op.execute("ALTER TABLE identities FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE comments FORCE ROW LEVEL SECURITY")

    op.alter_column("comments", "doc_kind", existing_type=sa.String(32), nullable=False)

    # drop the legacy human-only author column (replaced by author_identity_id)
    op.drop_constraint("comments_user_id_fkey", "comments", type_="foreignkey")
    op.drop_column("comments", "user_id")

    # --- CHECK constraints: closed sets + the XOR document handle
    op.create_check_constraint("kind", "comments", "kind IN ('comment', 'suggestion')")
    op.create_check_constraint(
        "status",
        "comments",
        "status IN ('open', 'resolved', 'accepted', 'rejected')",
    )
    op.create_check_constraint(
        "doc_kind",
        "comments",
        "doc_kind IN ('note_part', 'task_description')",
    )
    op.create_check_constraint(
        "doc_xor",
        "comments",
        "(doc_kind = 'task_description' AND task_id IS NOT NULL AND note_part_id IS NULL) "
        "OR (doc_kind = 'note_part' AND note_part_id IS NOT NULL AND task_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("doc_xor", "comments", type_="check")
    op.drop_constraint("doc_kind", "comments", type_="check")
    op.drop_constraint("status", "comments", type_="check")
    op.drop_constraint("kind", "comments", type_="check")

    # restore the legacy human-author column from the identity link
    op.add_column("comments", sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("ALTER TABLE comments NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE identities NO FORCE ROW LEVEL SECURITY")
    try:
        op.execute(
            """
            UPDATE comments c
               SET user_id = i.user_id
              FROM identities i
             WHERE i.id = c.author_identity_id
               AND i.user_id IS NOT NULL
            """
        )
    finally:
        op.execute("ALTER TABLE identities FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE comments FORCE ROW LEVEL SECURITY")
    op.create_foreign_key(
        "comments_user_id_fkey", "comments", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )

    # the restored NOT NULL on task_id only holds for task rows; drop the rest
    op.execute("DELETE FROM comments WHERE doc_kind <> 'task_description' OR task_id IS NULL")
    op.alter_column(
        "comments", "task_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )

    op.drop_index("ix_comments_parent_id", table_name="comments")
    op.drop_index("ix_comments_note_part_id", table_name="comments")
    op.drop_constraint(
        "fk_comments_resolved_by_identity_id_identities", "comments", type_="foreignkey"
    )
    op.drop_constraint("fk_comments_author_identity_id_identities", "comments", type_="foreignkey")
    op.drop_constraint("fk_comments_parent_id_comments", "comments", type_="foreignkey")
    op.drop_constraint("fk_comments_note_part_id_note_part", "comments", type_="foreignkey")
    for col in (
        "deleted_at",
        "edited_at",
        "resolved_at",
        "resolved_by_identity_id",
        "author_identity_id",
        "parent_id",
        "status",
        "proposed_text",
        "original_text",
        "anchor_suffix",
        "anchor_prefix",
        "anchor_quote",
        "kind",
        "note_part_id",
        "doc_kind",
    ):
        op.drop_column("comments", col)
