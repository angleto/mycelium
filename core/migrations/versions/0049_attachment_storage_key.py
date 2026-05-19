"""Pluggable attachment storage: add storage_key, allow data NULL.

Supports the new ``s3`` attachment backend (``attachment_store.py``)
without changing the HTTP contract. Two ALTERs on ``attachments``:

- add ``storage_key varchar(512)`` (NULL): the object-store key when
  the bytes live off-DB. Legacy / ``pg``-backend rows keep it NULL.
- drop the NOT NULL on ``data``: an ``s3`` row stores ``data NULL`` +
  ``storage_key`` set; legacy rows keep their bytes untouched.

No RLS / grant changes: the existing ``p_attachments`` policy and the
``flow_app`` privileges already cover the table and every column
(ADR-0002/0007), exactly like the column-only ALTERs in the migration
history. ALTER TABLE only, following the 0048 op style.

Downgrade re-imposes ``data NOT NULL`` and drops ``storage_key``. This
is intentionally NOT loss-tolerant: if any ``s3``-only row exists
(``data IS NULL``), re-adding NOT NULL fails by design -- those bytes
live only in the object store and silently destroying or zero-filling
the column would be data loss. Move the bytes back first (re-run the
data migration in reverse / restore from the object store) before
downgrading a deployment that used the ``s3`` backend. A pure ``pg``
deployment downgrades cleanly (every row has ``data`` set).

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE attachments ADD COLUMN storage_key varchar(512)",
    "ALTER TABLE attachments ALTER COLUMN data DROP NOT NULL",
)

DOWNGRADE: tuple[str, ...] = (
    # Fails (by design) if any s3-only row has data NULL: the bytes are
    # off-DB and must be restored before this downgrade is safe.
    "ALTER TABLE attachments ALTER COLUMN data SET NOT NULL",
    "ALTER TABLE attachments DROP COLUMN IF EXISTS storage_key",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
