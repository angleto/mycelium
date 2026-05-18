"""Tag scoping: a tag may be restricted to one/more projects or
clients. No rows for a tag => global (available everywhere). Same
org-scoped RLS / grants pattern as note_tags.

``target_tag_id`` is a project or client tag; the tag is offered on a
note/task when the tag is global OR a scope row targets that project
(or that project's client).

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE tag_scopes (
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      tag_id uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
      target_tag_id uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
      CONSTRAINT pk_tag_scopes PRIMARY KEY (tag_id, target_tag_id)
    )
    """,
    "CREATE INDEX ix_tag_scopes_org_id ON tag_scopes (org_id)",
    "CREATE INDEX ix_tag_scopes_target ON tag_scopes (target_tag_id)",
    "ALTER TABLE tag_scopes ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tag_scopes FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_tag_scopes ON tag_scopes "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tag_scopes TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS tag_scopes CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
