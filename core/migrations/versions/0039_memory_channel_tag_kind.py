"""Add 'memory_channel' to the tag_kind enum (additive).

A memory "channel" is an orthogonal facet for grouping memory blobs
(e.g. an agent's working set). It is just another ``tag_kind`` value,
so no new table is needed: channel tags are created/listed via the
existing tag endpoints and folded into the memory tag AND-filter.

``ALTER TYPE ... ADD VALUE`` runs outside the migration transaction
(autocommit block): Postgres forbids using a new enum label in the
same transaction that added it. Idempotent via IF NOT EXISTS.

``tag_kind`` is a native pg enum already referenced by the org-scoped
``tags`` table (RLS unchanged); adding a label does not touch grants
or policies.

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tag_kind ADD VALUE IF NOT EXISTS 'memory_channel'")


def downgrade() -> None:
    # Postgres cannot drop a single enum label; leaving 'memory_channel'
    # in the type is harmless (no rows reference it after a downgrade of
    # the feature). Intentional no-op; kept so the chain is symmetric.
    pass
