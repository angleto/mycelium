"""Note garden ecosystem: maturity lifecycle + typed M:N links
(docs/adr/0029, P1).

Additive: no existing column is dropped here. ``notes.task_id`` is
preserved (it is dropped only in ADR-0029 P3 after the typed link is
the only writer).

- ``notes.maturity`` text + CHECK + index. Default 'seed'.
- ``notes.promoted_at`` timestamptz NULL. Set when a note is
  transplanted to a task; from that point the note is read-only at
  the service layer (no schema flag beyond this column).
- ``note_note_link``: M:N typed (atom_of, references, replies_to,
  supersedes). UNIQUE(parent, child, kind); no self-link.
- ``note_task_link``: M:N typed (subject, artifact, derived_from,
  promoted_from). UNIQUE(note, task, kind).
- Backfill: every existing ``notes.task_id`` becomes an 'artifact'
  link. ``created_by`` is NULL for backfilled rows (pre-migration
  provenance is unknown); new writes set it from the caller's
  Identity (service layer).

RLS posture: ENABLE (post-#48 default).

Revision: 0088
Down revision: 0087
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    # 1) maturity + promoted_at on notes.
    """
    ALTER TABLE notes
      ADD COLUMN maturity text NOT NULL DEFAULT 'seed'
        CHECK (maturity IN ('seed', 'growing', 'mature', 'dormant')),
      ADD COLUMN promoted_at timestamptz NULL
    """,
    "CREATE INDEX ix_notes_maturity ON notes (maturity)",
    # 2) note_note_link
    """
    CREATE TABLE note_note_link (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      parent_note_id uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      child_note_id uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      kind text NOT NULL CHECK (kind IN (
        'atom_of', 'references', 'replies_to', 'supersedes'
      )),
      created_by uuid REFERENCES identities(id) ON DELETE SET NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_note_note_link UNIQUE (parent_note_id, child_note_id, kind),
      CONSTRAINT ck_note_note_link_no_self CHECK (parent_note_id <> child_note_id)
    )
    """,
    "CREATE INDEX ix_note_note_link_org_id ON note_note_link (org_id)",
    "CREATE INDEX ix_note_note_link_parent ON note_note_link (parent_note_id)",
    "CREATE INDEX ix_note_note_link_child ON note_note_link (child_note_id)",
    "ALTER TABLE note_note_link ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_note_note_link ON note_note_link "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON note_note_link TO flow_app",
    # 3) note_task_link
    """
    CREATE TABLE note_task_link (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      note_id uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      kind text NOT NULL CHECK (kind IN (
        'subject', 'artifact', 'derived_from', 'promoted_from'
      )),
      created_by uuid REFERENCES identities(id) ON DELETE SET NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_note_task_link UNIQUE (note_id, task_id, kind)
    )
    """,
    "CREATE INDEX ix_note_task_link_org_id ON note_task_link (org_id)",
    "CREATE INDEX ix_note_task_link_note ON note_task_link (note_id)",
    "CREATE INDEX ix_note_task_link_task ON note_task_link (task_id)",
    "ALTER TABLE note_task_link ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_note_task_link ON note_task_link "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON note_task_link TO flow_app",
    # 4) Backfill: existing Proposal A links become 'artifact' rows.
    # ``created_by`` is NULL: we cannot reconstruct which Identity
    # wrote the original link (it pre-dates the typed model and the
    # identities table). The service layer enforces a non-NULL
    # ``created_by`` for new writes.
    """
    INSERT INTO note_task_link (org_id, note_id, task_id, kind, created_at)
    SELECT n.org_id, n.id, n.task_id, 'artifact', n.created_at
    FROM notes n
    WHERE n.task_id IS NOT NULL
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS note_task_link CASCADE",
    "DROP TABLE IF EXISTS note_note_link CASCADE",
    "DROP INDEX IF EXISTS ix_notes_maturity",
    "ALTER TABLE notes DROP COLUMN IF EXISTS promoted_at",
    "ALTER TABLE notes DROP COLUMN IF EXISTS maturity",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
