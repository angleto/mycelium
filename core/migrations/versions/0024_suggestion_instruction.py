"""Link a suggestion to the instruction it carries out.

A person who wants an agent to revise a passage leaves a comment on it and
assigns the comment to the agent: that is an instruction. The agent answers
with suggestions, and ``instruction_id`` is the pointer from each suggestion
back to the comment that asked for it. Without it a reviewer -- the person
reading the diff, or an agent asked to verify the edits adversarially -- has
the edits and the instructions but not which edit answers which instruction,
so neither an unexecuted instruction nor an edit nobody asked for is visible.

``SET NULL`` like ``decision_comment_id``: purging the instruction must not
cascade the suggestions away with it. Indexed, because the instruction's card
lists the suggestions that answer it.

Nullable addition with no default and no backfill: a metadata-only change, no
rewrite of ``comments``.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column("instruction_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_comments_instruction_id_comments",
        "comments",
        "comments",
        ["instruction_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_comments_instruction_id", "comments", ["instruction_id"])


def downgrade() -> None:
    op.drop_index("ix_comments_instruction_id", table_name="comments")
    op.drop_constraint("fk_comments_instruction_id_comments", "comments", type_="foreignkey")
    op.drop_column("comments", "instruction_id")
