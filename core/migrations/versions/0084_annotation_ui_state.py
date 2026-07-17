"""Per-user collapse state for annotation cards (comments & suggestions).

Mirrors ``note_part_ui_state`` (migration 0011): a lazily-materialised
per-user side table keyed (user_id, annotation_id) — no row = expanded.
No ``org_id`` column: the RLS policy joins through the parent ``comments``
row with an EXISTS subquery, exactly like the note-part precedent.

Revision ID: 0084
Revises: 0083
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "annotation_ui_state",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "annotation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("comments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collapsed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "annotation_id", name="pk_annotation_ui_state"),
    )
    op.execute("ALTER TABLE annotation_ui_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE annotation_ui_state FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_annotation_ui_state ON annotation_ui_state "
        "USING (EXISTS ("
        "  SELECT 1 FROM comments c "
        "    WHERE c.id = annotation_ui_state.annotation_id "
        f"      AND c.{_ORG_PRED}"
        ")) WITH CHECK (EXISTS ("
        "  SELECT 1 FROM comments c "
        "    WHERE c.id = annotation_ui_state.annotation_id "
        f"      AND c.{_ORG_PRED}"
        "))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE annotation_ui_state TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_annotation_ui_state ON annotation_ui_state")
    op.drop_table("annotation_ui_state")
