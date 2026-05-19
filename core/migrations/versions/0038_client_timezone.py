"""Add an IANA timezone to a client profile.

A client can carry a preferred IANA timezone name (e.g.
``Europe/Rome``) so time entries / the per-task time report can be
read in the client's local time. Existing client profiles stay NULL
(no backfill); the column is optional.

``client_profile`` is already org-scoped + RLS. A plain column add
inherits the table's existing RLS policy and flow_app grants, so no
extra GRANT is needed (same as the 0029/0030/0037 column-add
migrations).

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = ("ALTER TABLE client_profile ADD COLUMN IF NOT EXISTS timezone text",)

DOWNGRADE: tuple[str, ...] = ("ALTER TABLE client_profile DROP COLUMN IF EXISTS timezone",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
