"""What the schedule's dates rest on (task 27dc7e68, ADR-0004 amendment).

``schedule.estimate_missing``    the task has no estimate, so its duration
                                 is unknown rather than 0.
``schedule.dates_lower_bound``   the row's dates depend on an unknown
                                 duration, its own or one upstream.
``schedule.due_margin_minutes``  by how much the leveled finish meets (+)
                                 or misses (-) the task's own due date.
``schedule.on_critical_chain``   dropped. Measured on production on
                                 2026-11-26, its 258 tasks did not contain
                                 the critical path it claimed to extend,
                                 and nothing outside the Schedule view
                                 read it.

The rows are derived and rewritten by every recompute, so the new columns
need no backfill: their defaults hold until the next recompute, which the
dispatch tick runs within a minute. The downgrade restores the column with
its default, not its values, which no recompute after this one produces.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "schedule",
        sa.Column(
            "estimate_missing", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "schedule",
        sa.Column(
            "dates_lower_bound", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column("schedule", sa.Column("due_margin_minutes", sa.Integer(), nullable=True))
    op.drop_column("schedule", "on_critical_chain")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "schedule",
        sa.Column(
            "on_critical_chain", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.drop_column("schedule", "due_margin_minutes")
    op.drop_column("schedule", "dates_lower_bound")
    op.drop_column("schedule", "estimate_missing")
