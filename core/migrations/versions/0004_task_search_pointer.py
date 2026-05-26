"""Task search: pointer table linking a task to its searchable blob.

Extends the existing memory pipeline (``memory_blobs``: FTS generated +
pgvector + RRF) to cover tasks without cloning the index. One blob per
task with ``text = title || description || checklist joined``; the
pointer is the 1:1 binding (UNIQUE on ``blob_id``) plus a ``content_hash``
so an event-listener-driven resync can skip metadata-only mutations
(state, priority, due) and only re-embed on real text changes.

FK asymmetry is deliberate: ``task_id -> tasks(id)`` is a simple FK
(``tasks`` is not partitioned), ``(blob_id, org_id) -> memory_blobs(id,
org_id)`` is composite because ``memory_blobs`` is PARTITION BY HASH
(org_id) (so its PK includes the partition key). Both legs cascade:
a task delete drops the pointer (the *blob* cleanup is the listener's
job in ``Task.after_delete``, since a parent->child cascade alone would
leave the blob orphaned); a GDPR erase of the blob drops the pointer
(self-healing).

Org scoping: own RLS policy on ``org_id`` (the standard
``app.current_org`` predicate, same shape as ``task_checklist_items``).
Not FORCE'd: matches the rest of the per-org tenant data (only the
attachment buckets are FORCE'd because they bypass via service-role
queries).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_index_pointer",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "blob_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("task_id", name="pk_task_index_pointer"),
        sa.UniqueConstraint("blob_id", name="uq_task_index_pointer_blob_id"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name="fk_task_index_pointer_task_id_tasks",
        ),
        sa.ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_task_index_pointer_blob_id_memory_blobs",
        ),
    )
    op.create_index(
        "ix_task_index_pointer_org_id",
        "task_index_pointer",
        ["org_id"],
    )
    op.create_index(
        "ix_task_index_pointer_blob_id",
        "task_index_pointer",
        ["blob_id"],
    )
    op.execute("ALTER TABLE task_index_pointer ENABLE ROW LEVEL SECURITY")
    _org_pred = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"
    op.execute(
        f"CREATE POLICY p_task_index_pointer ON task_index_pointer "
        f"USING ({_org_pred}) WITH CHECK ({_org_pred})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE task_index_pointer TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_task_index_pointer ON task_index_pointer")
    op.drop_index("ix_task_index_pointer_blob_id", table_name="task_index_pointer")
    op.drop_index("ix_task_index_pointer_org_id", table_name="task_index_pointer")
    op.drop_table("task_index_pointer")
