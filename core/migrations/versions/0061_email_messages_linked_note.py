"""email_messages.linked_note_id (WS-3, email -> note).

Symmetric with ``linked_task_id``: a message can be promoted to a Note.
FK to notes.id ON DELETE SET NULL (deleting the note leaves the email).

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages",
        sa.Column("linked_note_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_email_messages_linked_note_id_notes",
        "email_messages",
        "notes",
        ["linked_note_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_email_messages_linked_note_id_notes",
        "email_messages",
        type_="foreignkey",
    )
    op.drop_column("email_messages", "linked_note_id")
