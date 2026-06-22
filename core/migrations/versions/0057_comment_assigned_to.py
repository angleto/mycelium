"""Add comments.assigned_to_identity_id: assign an annotation to a person
(task 861b360b, annotations backlog 1f161485 #1).

The Google-Docs "assign this comment to @someone" capability, dropped in the
v3 simplification and now restored. A nullable FK to ``identities`` with
``ON DELETE SET NULL`` -- exactly like ``author_identity_id`` /
``resolved_by_identity_id`` -- so a removed identity clears the assignment
instead of orphaning it. Indexed for the "assigned to me" inbox query.
NULL-default ⇒ every existing annotation is unassigned and unchanged.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column("assigned_to_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Name matches the metadata naming_convention
    # (fk_%(table)s_%(col)s_%(referred_table)s) so the model's inline FK and
    # this constraint share a name and autogenerate sees no phantom drift.
    op.create_foreign_key(
        "fk_comments_assigned_to_identity_id_identities",
        "comments",
        "identities",
        ["assigned_to_identity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # The "assigned to me" inbox filters by assignee across the workspace.
    op.create_index(
        op.f("ix_comments_assigned_to_identity_id"),
        "comments",
        ["assigned_to_identity_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_comments_assigned_to_identity_id"), table_name="comments")
    op.drop_constraint(
        "fk_comments_assigned_to_identity_id_identities", "comments", type_="foreignkey"
    )
    op.drop_column("comments", "assigned_to_identity_id")
