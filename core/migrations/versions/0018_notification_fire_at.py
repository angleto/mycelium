"""Add notifications.fire_at + notifications.attempts (reminder delivery gate).

The reminder pipeline enqueues a notification as soon as its firing
moment enters the look-ahead window (``scan_reminders``), but dispatch
must HOLD it until that moment actually arrives. Before this migration
the firing moment lived only inside ``dedupe_key`` (a string, never
queryable), so ``dispatch_pending`` sent every pending reminder in the
next tick -- up to ~2 days early. ``fire_at`` persists the moment so the
dispatch query can gate on ``fire_at IS NULL OR fire_at <= now()``
(NULL = send immediately, for non-reminder notifications).

``attempts`` bounds retries: a transient send failure marks the row
``failed``; a later scan revives it to ``pending`` for retry, but only
while ``attempts`` is under the service cap, so a permanently-broken
target is not retried on every tick forever.

Existing rows backfill ``fire_at = NULL`` (dispatch immediately, the
pre-0018 behaviour) and ``attempts = 0``.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    # Dispatch hot path scans pending rows whose fire_at has arrived.
    op.create_index(
        op.f("ix_notifications_fire_at"),
        "notifications",
        ["fire_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_fire_at"), table_name="notifications")
    op.drop_column("notifications", "attempts")
    op.drop_column("notifications", "fire_at")
