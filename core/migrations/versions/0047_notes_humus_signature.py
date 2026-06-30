"""Add notes.humus_signature: idempotency key for synthesised humus (e87daff4).

Phase-2 decomposition (ADR-0039) adds two N:1 humus syntheses beyond the
1:1 distillation: a cross-/intra-cluster PATTERN note over a set of archived
sources, and a quarterly SEASON note. Both must be idempotent so a periodic
job (or a re-run) does not spawn duplicates. ``humus_signature`` is the
stable key:

- pattern: a hash of the sorted source note ids;
- season:  ``"<year>Q<quarter>"``.

A partial unique index on (org_id, humus_kind, humus_signature) enforces
"one synthesis per (kind, signature)" at the DB while leaving the millions
of ordinary notes (NULL signature) unconstrained.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("humus_signature", sa.String(80), nullable=True))
    op.create_index(
        "uq_notes_humus_signature",
        "notes",
        ["org_id", "humus_kind", "humus_signature"],
        unique=True,
        postgresql_where=sa.text("humus_signature IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notes_humus_signature", table_name="notes")
    op.drop_column("notes", "humus_signature")
