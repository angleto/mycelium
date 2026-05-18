"""F6b (additive): voice/text/conversation notes (docs/adr/0020, 0021,
FR-16). ``notes`` (transcript + S3 audio_ref + status) and
``note_turns`` (conversation thread). Raw audio lives in S3, never the
DB. RLS+FORCE + flow_app grants, same patterns as 0010.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = ("notes", "note_turns")

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE note_kind AS ENUM ('voice', 'text', 'conversation')",
    "CREATE TYPE note_status AS ENUM ('captured', 'transcribing', 'ready', 'error')",
    "CREATE TYPE turn_role AS ENUM ('user', 'assistant')",
    """
    CREATE TABLE notes (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      project_id uuid,
      kind note_kind NOT NULL,
      status note_status NOT NULL DEFAULT 'captured',
      title varchar(300),
      transcript text,
      summary text,
      audio_ref varchar(512),
      audio_seconds integer,
      last_error text,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_notes_org_id ON notes (org_id)",
    "CREATE INDEX ix_notes_project_id ON notes (project_id)",
    """
    CREATE TABLE note_turns (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      note_id uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      role turn_role NOT NULL,
      content text NOT NULL,
      ord integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_note_turns_note_id UNIQUE (note_id, ord)
    )
    """,
    "CREATE INDEX ix_note_turns_org_id ON note_turns (org_id)",
    "CREATE INDEX ix_note_turns_note_id ON note_turns (note_id)",
)


def _rls(table: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY p_{table} ON {table} USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO flow_app",
    )


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)
    for table in _RLS_TABLES:
        for stmt in _rls(table):
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS note_turns CASCADE")
    op.execute("DROP TABLE IF EXISTS notes CASCADE")
    for typ in ("turn_role", "note_status", "note_kind"):
        op.execute(f"DROP TYPE IF EXISTS {typ}")
