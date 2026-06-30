"""Add notes.origin_model_id + notes.review_state: human-gated review state
for AUTONOMOUSLY-generated nodes (ADR-0043, task e87daff4).

A summary the garden generates AUTONOMOUSLY (the unsolicited background
sweep) must NOT enter the corpus until a human approves it, and the model
that produced it must be visible on the artifact. Two NULL-default columns
carry that, orthogonal to ``maturity`` / ``humus_flag``:

- ``origin_model_id``: the LLM ``model_id`` that generated the node (NULL for
  human-authored) -- transparency on the artifact, not only in the transient
  MCP response.
- ``review_state``: NULL for every human/legacy note AND every USER-initiated
  creation (always effective, unchanged from today); ``'proposed'`` set ONLY
  by the autonomous sweep (withheld from every retrieval surface); ``'approved'``
  once a human accepts. No stored ``'rejected'`` -- a reject soft-deletes.

A note is EFFECTIVE iff ``review_state IS DISTINCT FROM 'proposed'`` AND
``deleted_at IS NULL``. NULL-default ⇒ every existing note is effective and
byte-identical. A partial index on the (rare) proposed rows keeps the review
inbox query cheap without touching the millions of ordinary (NULL) notes. No
RLS change: the columns inherit the table-level ``p_notes`` org policy.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("origin_model_id", sa.String(128), nullable=True))
    op.add_column("notes", sa.Column("review_state", sa.String(16), nullable=True))
    # Partial index: only the rare ``proposed`` rows the review inbox lists
    # (the exclusion predicate on the hot read path is a plain negative filter
    # that ANDs against already-selective predicates, so it needs no index).
    op.create_index(
        "ix_notes_review_proposed",
        "notes",
        ["org_id", "created_at"],
        postgresql_where=sa.text("review_state = 'proposed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_notes_review_proposed", table_name="notes")
    op.drop_column("notes", "review_state")
    op.drop_column("notes", "origin_model_id")
