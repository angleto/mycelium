"""F1: unified taxonomy (tags + client/project profiles), tasks,
subtasks, assignees, comments. RLS+FORCE + grants, same patterns as
0001 (docs/adr/0002, 0003, 0007).

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_TABLES = (
    "tags",
    "client_profile",
    "project_profile",
    "tasks",
    "task_tags",
    "task_assignees",
    "comments",
)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE tag_kind AS ENUM ('generic', 'client', 'project')",
    "CREATE TYPE task_status AS ENUM ('todo', 'in_progress', 'blocked', 'done')",
    "CREATE TYPE exec_kind AS ENUM ('human', 'llm_agent')",
    """
    CREATE TABLE tags (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      kind tag_kind NOT NULL,
      name varchar(120) NOT NULL,
      color varchar(16),
      status varchar(16) NOT NULL DEFAULT 'active',
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_tags_org_id UNIQUE (org_id, kind, name)
    )
    """,
    "CREATE INDEX ix_tags_org_id ON tags (org_id)",
    """
    CREATE TABLE client_profile (
      tag_id uuid PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      ragione_sociale varchar(200) NOT NULL,
      id_paese varchar(2),
      id_codice varchar(30),
      codice_fiscale varchar(30),
      indirizzo varchar(200),
      cap varchar(10),
      comune varchar(120),
      provincia varchar(4),
      nazione varchar(2),
      codice_destinatario varchar(7),
      pec varchar(320),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_client_profile_org_id ON client_profile (org_id)",
    """
    CREATE TABLE project_profile (
      tag_id uuid PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      client_tag_id uuid REFERENCES tags(id) ON DELETE SET NULL,
      tariffa numeric(12, 2),
      valuta varchar(3) NOT NULL DEFAULT 'EUR',
      budget numeric(14, 2),
      workflow_id uuid,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_project_profile_org_id ON project_profile (org_id)",
    "CREATE INDEX ix_project_profile_client_tag_id ON project_profile (client_tag_id)",
    """
    CREATE TABLE tasks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      title varchar(300) NOT NULL,
      description text,
      priority smallint NOT NULL DEFAULT 3,
      start_date date,
      due_date date,
      status task_status NOT NULL DEFAULT 'todo',
      estimate_effort_h numeric(8, 2),
      parent_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
      executor_kind exec_kind NOT NULL DEFAULT 'human',
      executor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
      is_archived boolean NOT NULL DEFAULT false,
      deleted_at timestamptz,
      created_by uuid REFERENCES users(id) ON DELETE SET NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_tasks_org_id ON tasks (org_id)",
    "CREATE INDEX ix_tasks_parent_task_id ON tasks (parent_task_id)",
    """
    CREATE TABLE task_tags (
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      tag_id uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      CONSTRAINT pk_task_tags PRIMARY KEY (task_id, tag_id)
    )
    """,
    "CREATE INDEX ix_task_tags_org_id ON task_tags (org_id)",
    "CREATE INDEX ix_task_tags_tag_id ON task_tags (tag_id)",
    """
    CREATE TABLE task_assignees (
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      CONSTRAINT pk_task_assignees PRIMARY KEY (task_id, user_id)
    )
    """,
    "CREATE INDEX ix_task_assignees_org_id ON task_assignees (org_id)",
    """
    CREATE TABLE comments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      user_id uuid REFERENCES users(id) ON DELETE SET NULL,
      body text NOT NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_comments_org_id ON comments (org_id)",
    "CREATE INDEX ix_comments_task_id ON comments (task_id)",
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
    for table in _TABLES:
        for stmt in _rls(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in (
        "comments",
        "task_assignees",
        "task_tags",
        "tasks",
        "project_profile",
        "client_profile",
        "tags",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum in ("exec_kind", "task_status", "tag_kind"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
