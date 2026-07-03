"""retrieval_trace + note_edge_usage: Fase 0 substrate of the
search-informed graph (task 561c6aca, plan si-procedi-a-sceiverla).

Two additive tables, ZERO behaviour change:

- ``retrieval_trace``: append-only trace of the returned top-m per
  search, written by the new side-effect-only ``RetrievalTraceStage``.
  One row PER SEARCH with a JSONB ``items`` list of
  ``{"blob_id", "rank"}`` (not one row per item): the Phase-2
  aggregation pairs *ranking-adjacent* items within one search, so with
  the search as the row adjacency is array order and pairing is a
  linear O(m) pass with no grouping key or window function -- and the
  read-path write stays a single INSERT (design risk #6). Content-free
  (ids + ranks only); erased blobs leave inert ids that no longer
  resolve at aggregation time. Retention = windowed delete on the
  ``(org_id, created_at)`` index.

- ``note_edge_usage``: pair-keyed clone of ``note_coactivity`` (0051)
  holding the search-informed per-edge counters the offline
  ``refresh_edge_usage`` (Phase 2) will materialise from the traces.
  Canonical undirected pair ``note_a_id <= note_b_id``
  (``graph._pair_key``); direction lives in ``forward_count`` /
  ``backward_count`` only. Created EMPTY in Fase 0.

RLS FORCE + org policy + app grants in the 0051/0025 pattern; note FKs
cascade so a hard-deleted note drops its usage edges.

Revision ID: 0081
Revises: 0080
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "retrieval_trace",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_retrieval_trace_org_id", "retrieval_trace", ["org_id"])
    op.create_index("ix_retrieval_trace_org_created", "retrieval_trace", ["org_id", "created_at"])

    op.execute("ALTER TABLE retrieval_trace ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE retrieval_trace FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_retrieval_trace ON retrieval_trace "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE retrieval_trace TO mycelium_app")

    op.create_table(
        "note_edge_usage",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "note_a_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "note_b_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("traversal_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("forward_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("backward_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_traversed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decay_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "note_a_id", "note_b_id", name="uq_note_edge_usage_pair"),
    )
    op.create_index("ix_note_edge_usage_org_id", "note_edge_usage", ["org_id"])

    op.execute("ALTER TABLE note_edge_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_edge_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_note_edge_usage ON note_edge_usage "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_edge_usage TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_note_edge_usage ON note_edge_usage")
    op.drop_index("ix_note_edge_usage_org_id", table_name="note_edge_usage")
    op.drop_table("note_edge_usage")

    op.execute("DROP POLICY IF EXISTS p_retrieval_trace ON retrieval_trace")
    op.drop_index("ix_retrieval_trace_org_created", table_name="retrieval_trace")
    op.drop_index("ix_retrieval_trace_org_id", table_name="retrieval_trace")
    op.drop_table("retrieval_trace")
