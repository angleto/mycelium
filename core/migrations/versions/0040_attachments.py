"""Attachments on notes and tasks (DB-BYTEA storage).

A file uploaded to a note OR a task: stored as ``bytea`` in the
``data`` column (no filesystem / object store; fits the single-node
co-tenant deploy, per-file size cap enforced in the service). One row
per file with the parent FK, filename, mime type, size and uploader.
Exactly one of ``note_id`` / ``task_id`` is non-null (table CHECK);
both parent FKs are ``ON DELETE CASCADE`` so deleting the note/task
removes its attachments. ``users`` FK is ``ON DELETE RESTRICT`` (an
uploader is never silently nulled; users are not erased in v1).

Org-scoped + RLS exactly like the other tenant tables: the policy and
flow_app grants are copied verbatim from ``note_tags`` / ``blob_sources``
(the org predicate ``app.current_org``). Clean downgrade drops the
table (CASCADE), symmetric with the create.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE attachments (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      note_id uuid REFERENCES notes(id) ON DELETE CASCADE,
      task_id uuid REFERENCES tasks(id) ON DELETE CASCADE,
      filename varchar(255) NOT NULL,
      mime_type varchar(160) NOT NULL,
      size_bytes integer NOT NULL,
      data bytea NOT NULL,
      uploaded_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_attachments PRIMARY KEY (id),
      CONSTRAINT ck_attachments_one_parent CHECK (
        (note_id IS NOT NULL AND task_id IS NULL)
        OR (note_id IS NULL AND task_id IS NOT NULL)
      )
    )
    """,
    "CREATE INDEX ix_attachments_org_id ON attachments (org_id)",
    "CREATE INDEX ix_attachments_note_id ON attachments (note_id)",
    "CREATE INDEX ix_attachments_task_id ON attachments (task_id)",
    "ALTER TABLE attachments ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE attachments FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_attachments ON attachments "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON attachments TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS attachments CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
