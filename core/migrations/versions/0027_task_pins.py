"""A person's pinned tasks.

The tasks someone is working on right now, kept at the top of /tasks above
the recent ones so they are one tap away on every device. A pin belongs to
the person and not to the task: two people pin different things in the same
workspace, and pinning changes nothing about the task itself (no
``updated_at`` bump, which would also reorder the Recent list, and no
revision). A closed task stays pinned until it is unpinned; that is what
the person asked for, so finishing a task does not make it vanish from the
place they reach it from.

``task_pins``  one row per (person, task). Workspace RLS on ``org_id``; the
               task and the person cascade, so a purged task or a removed
               account leaves no pin behind.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # The three foreign keys take a SHARE ROW EXCLUSIVE lock on users, tasks
    # and organizations. Behind an open transaction on one of them the
    # migration would wait, and every write to that table would queue behind
    # it: measured on 0026, 23s behind one open UPDATE on users. Ten seconds,
    # then fail; a failed migration is retried, a stalled table is an outage.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "task_pins",
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "pinned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "task_id", name="pk_task_pins"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_task_pins_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_pins_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_task_pins_org_id_organizations",
            ondelete="CASCADE",
        ),
    )
    # The read is always "this person's pins in this workspace, newest
    # first"; the primary key leads with the person, the index adds the
    # workspace and the order.
    op.create_index(
        "ix_task_pins_org_user_pinned_at", "task_pins", ["org_id", "user_id", "pinned_at"]
    )
    op.create_index("ix_task_pins_task_id", "task_pins", ["task_id"])
    op.execute("ALTER TABLE task_pins ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_pins FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_task_pins ON task_pins USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "task_pins")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE task_pins TO mycelium_app")


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE task_pins FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON task_pins")
    op.execute("DROP POLICY IF EXISTS p_task_pins ON task_pins")
    op.drop_index("ix_task_pins_task_id", table_name="task_pins")
    op.drop_index("ix_task_pins_org_user_pinned_at", table_name="task_pins")
    op.drop_table("task_pins")
