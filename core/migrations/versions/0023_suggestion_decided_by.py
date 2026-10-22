"""Record who decided a suggestion an agent accepted, and where.

Accepting a suggestion stays a person's decision. What changes is who may
carry it out: an agent may now execute the accept, but only by citing a
comment in which a person decided it, and the server verifies that comment
before splicing anything (``annotations.accept_suggestion_on_decision``).

Two columns, set together and only on that path. ``decided_by_identity_id``
is the person; ``decision_comment_id`` is the comment. ``resolved_by`` keeps
meaning whoever executed the accept, so a reader never has to guess which of
the two a single column meant. Both stay NULL on an accept a person clicked.

``SET NULL`` on both, like ``author_identity_id`` and ``resolved_by``: an
identity or a comment can be purged, and losing the pointer is better than
cascading the accepted suggestion away with it. The audit row written at
accept time carries the same ids and is append-only.

Nullable additions with no default and no backfill: a metadata-only change,
no rewrite of ``comments``.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-22
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
    op.add_column(
        "comments",
        sa.Column("decided_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "comments",
        sa.Column("decision_comment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_comments_decided_by_identity_id_identities",
        "comments",
        "identities",
        ["decided_by_identity_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_comments_decision_comment_id_comments",
        "comments",
        "comments",
        ["decision_comment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_comments_decision_comment_id_comments", "comments", type_="foreignkey")
    op.drop_constraint(
        "fk_comments_decided_by_identity_id_identities", "comments", type_="foreignkey"
    )
    op.drop_column("comments", "decision_comment_id")
    op.drop_column("comments", "decided_by_identity_id")
