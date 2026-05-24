"""Make Eisenhower axes mandatory with Low/Low (4/4) as the default.

importance/urgency were nullable since 0017 to keep the legacy
priority-only path intact. The MCP/API/CLI defaults left them unset
when the caller did not pass them, so MCP-created tasks ended up
with importance=NULL urgency=NULL and priority=3 (column default) ---
indistinguishable from "deliberately P3" and inconsistent with the
detail view (which fabricated P16 in JS before the priority-source-of-
truth fix landed). The policy is: every task carries an Eisenhower
position, defaulting to Low/Low when the caller is silent.

Backfill: COALESCE existing NULLs to 4 and recompute priority for the
rows we just touched (only the ones that were genuinely unset; tasks
that already had both axes set keep their stored priority). Then the
columns become NOT NULL with server default 4.

Revision ID: 0102
Revises: 0103
Create Date: 2026-05-24

NB on chaining: this migration was developed locally while 0103
(received_invoices) was being committed by another dev. 0103 was
authored with down_revision="0101" to be safe against the push race;
as the explanatory note in 0103 instructs, this file rebases its
down_revision to "0103" so the chain becomes a straight line
0101 -> 0103 -> 0102 instead of two heads off 0101. The numeric
ordering is irrelevant to Alembic --- revisions are linked by id.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0102"
down_revision: str | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Backfill NULL axes to Low (4). Recompute priority only for rows
    # whose axes were unset (priority = 1..25 clamp on importance *
    # urgency). Rows that already carried both axes keep their stored
    # priority --- the service derived it on create/update.
    #
    # tasks has FORCE ROW LEVEL SECURITY (no app.current_org GUC inside
    # a migration -> RLS hides every row, the UPDATE touches zero,
    # the subsequent SET NOT NULL fails). Disable RLS around the
    # backfill exactly like 0099 / 0104 do for the same reason.
    op.execute("ALTER TABLE tasks DISABLE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE tasks
        SET priority = GREATEST(1, LEAST(25, 4 * 4))
        WHERE importance IS NULL AND urgency IS NULL
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET importance = COALESCE(importance, 4),
            urgency = COALESCE(urgency, 4)
        WHERE importance IS NULL OR urgency IS NULL
        """
    )
    op.execute("ALTER TABLE tasks ALTER COLUMN importance SET NOT NULL")
    op.execute("ALTER TABLE tasks ALTER COLUMN urgency SET NOT NULL")
    op.execute("ALTER TABLE tasks ALTER COLUMN importance SET DEFAULT 4")
    op.execute("ALTER TABLE tasks ALTER COLUMN urgency SET DEFAULT 4")
    op.execute("ALTER TABLE tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks ALTER COLUMN importance DROP DEFAULT")
    op.execute("ALTER TABLE tasks ALTER COLUMN urgency DROP DEFAULT")
    op.execute("ALTER TABLE tasks ALTER COLUMN importance DROP NOT NULL")
    op.execute("ALTER TABLE tasks ALTER COLUMN urgency DROP NOT NULL")
