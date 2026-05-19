"""Memory channels as a controlled, seeded vocabulary with a stable key.

Background: a memory "channel" is a tag of kind ``memory_channel``
(migration 0039). Integrations (email ingest, Telegram) need a
DETERMINISTIC, well-known channel to write into; an arbitrary
user-named tag gives an integration no stable target. The fix keeps
channels as ``memory_channel`` tags (already RLS-scoped) but turns them
into a controlled vocabulary keyed by a stable slug.

``tags.system_key`` is the stable slug for a memory_channel tag (e.g.
``email``, ``telegram``, ``manual``, ``agent``). It is NULL for every
non-channel tag and for any future admin-added custom channel without
an integration binding; only seeded/keyed channels set it.

The PARTIAL UNIQUE index ``uq_tags_org_system_key`` enforces "at most
one channel per (org, key)" exactly where it matters (``system_key IS
NOT NULL``) without constraining the millions of NULL non-channel tags
and without colliding with the existing ``(org_id, kind, name)``
unique constraint. Enable/disable reuses the pre-existing
``tags.status`` soft-state ('active' vs 'archived'); NO new boolean
column is added (the model already has soft-archive state and the tag
PATCH path already round-trips ``status``).

``tags`` is already org-scoped + RLS (ENABLE + FORCE) with the
``p_tags`` policy and flow_app grants; a plain column add plus an index
inherit the table's existing policy and grants, so no extra GRANT is
needed (same as the 0029/0030/0037/0041 column-add migrations).
Downgrade drops the index then the column, symmetrically.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE tags ADD COLUMN system_key varchar(64) NULL",
    # Partial: only keyed channels participate; NULL (every non-channel
    # tag, plus keyless custom channels) is exempt and never collides.
    "CREATE UNIQUE INDEX uq_tags_org_system_key ON tags (org_id, system_key) "
    "WHERE system_key IS NOT NULL",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS uq_tags_org_system_key",
    "ALTER TABLE tags DROP COLUMN IF EXISTS system_key",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
