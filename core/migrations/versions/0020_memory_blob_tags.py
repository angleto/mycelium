"""Memory blob tags (additive): structured facets on memory.

A ``MemoryBlob`` can carry tags, an orthogonal axis to the hard
``(org, project)`` isolation boundary: tags narrow retrieval *inside*
that boundary, never across it. The join mirrors ``blob_sources``
(composite FK to the hash-partitioned ``memory_blobs (id, org_id)``),
plus a FK to ``tags`` and the same RLS/grant pattern. The org FK keeps
parity with the workspace-delete cascade invariant (migration 0019).

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE memory_blob_tags (
      blob_id uuid NOT NULL,
      org_id uuid NOT NULL,
      tag_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_memory_blob_tags PRIMARY KEY (blob_id, tag_id),
      CONSTRAINT fk_memory_blob_tags_blob_id_memory_blobs
        FOREIGN KEY (blob_id, org_id)
        REFERENCES memory_blobs (id, org_id) ON DELETE CASCADE,
      CONSTRAINT fk_memory_blob_tags_tag_id_tags
        FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE,
      CONSTRAINT fk_memory_blob_tags_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES organizations (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_memory_blob_tags_org_id ON memory_blob_tags (org_id)",
    "CREATE INDEX ix_memory_blob_tags_tag_id ON memory_blob_tags (tag_id)",
    "ALTER TABLE memory_blob_tags ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_blob_tags FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_memory_blob_tags ON memory_blob_tags "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON memory_blob_tags TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS memory_blob_tags CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
