"""Invoice soft-delete (recycle bin) + archive.

Two additive visibility columns on ``invoices``, mirroring the task/note
convention, orthogonal to the SdI ``state``:

- ``deleted_at`` (TIMESTAMPTZ, NULL): non-NULL = trashed (reversible; the
  active list hides it). A trashed draft may then be hard-deleted; a
  transmitted document is kept for the fiscal record.
- ``is_archived`` (BOOL, default false): filed away (year-end) but valid.

A composite index supports the default "active" list filter
(``deleted_at IS NULL AND NOT is_archived``) per org.

Revision ID: 0065
Revises: 0064
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_invoices_org_deleted_archived",
        "invoices",
        ["org_id", "deleted_at", "is_archived"],
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_org_deleted_archived", table_name="invoices")
    op.drop_column("invoices", "is_archived")
    op.drop_column("invoices", "deleted_at")
