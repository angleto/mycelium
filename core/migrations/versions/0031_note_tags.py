"""Note<->tag association (note_tags).

Notes become taggable like tasks: a single relation for every tag kind
(docs/adr/0003), org_id carried for RLS. Same pattern as task_tags +
the RLS / flow_app grants of the other org-scoped tables.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE note_tags (
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      note_id uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      tag_id uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
      CONSTRAINT pk_note_tags PRIMARY KEY (note_id, tag_id)
    )
    """,
    "CREATE INDEX ix_note_tags_org_id ON note_tags (org_id)",
    "CREATE INDEX ix_note_tags_tag_id ON note_tags (tag_id)",
    "ALTER TABLE note_tags ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE note_tags FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_note_tags ON note_tags "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON note_tags TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS note_tags CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
