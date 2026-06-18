"""Add usage_record.actor_kind: attribute spend to human vs system (WS-F5).

The autonomous metabolism (garden sweep, embedding backfill) meters its
spend just like a user action, but nothing distinguished the two -- so a
per-workspace budget could only cap TOTAL spend, which would also throttle
user actions. Recording the actor kind on each UsageRecord lets the
autonomous-budget cap sum only ``system`` spend and pause the autonomous
jobs alone (WS-F5 / ec88362f G19), never the user.

Nullable: historical rows predate the column (treated as unknown, neither
human nor system); meter() fills it from the session actor GUC going
forward.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_record", sa.Column("actor_kind", sa.String(32), nullable=True))
    # The budget read path sums recent system spend per org; index it.
    op.create_index(
        "ix_usage_record_org_actor_kind_created",
        "usage_record",
        ["org_id", "actor_kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_record_org_actor_kind_created", table_name="usage_record")
    op.drop_column("usage_record", "actor_kind")
