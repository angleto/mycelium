"""No orphan tasks: every task must have a project (hence a client).

Forward, create_task auto-assigns the default project. This migration
backfills existing orphans: per org that has a task with no
project-kind tag, ensure a default "Personal" client + "General"
project (idempotent on the uq_tags (org_id,kind,name)), record the ids
in organizations.settings, and link every orphan task to that project.

RLS is toggled off on the touched tables for the cross-org backfill
(owner-permitted, role-independent), then restored, same as 0028.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS = ("tags", "client_profile", "project_profile", "task_tags", "tasks", "organizations")

_ORPHAN = """
  SELECT t.id, t.org_id FROM tasks t
  WHERE NOT EXISTS (
    SELECT 1 FROM task_tags tt JOIN tags g ON g.id = tt.tag_id
    WHERE tt.task_id = t.id AND g.kind = 'project'
  )
"""

UPGRADE: tuple[str, ...] = (
    *(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY" for t in _RLS),
    # 1. default "Personal" client per org that needs one.
    f"""
    INSERT INTO tags (org_id, kind, name)
    SELECT DISTINCT o.org_id, 'client'::tag_kind, 'Personal'
    FROM ({_ORPHAN}) o
    ON CONFLICT (org_id, kind, name) DO NOTHING
    """,
    """
    INSERT INTO client_profile (tag_id, org_id, ragione_sociale)
    SELECT g.id, g.org_id, 'Personal'
    FROM tags g
    WHERE g.kind = 'client' AND g.name = 'Personal'
      AND NOT EXISTS (SELECT 1 FROM client_profile cp WHERE cp.tag_id = g.id)
    """,
    # 2. default "General" project per org that needs one, under that
    #    org's "Personal" client.
    f"""
    INSERT INTO tags (org_id, kind, name)
    SELECT DISTINCT o.org_id, 'project'::tag_kind, 'General'
    FROM ({_ORPHAN}) o
    ON CONFLICT (org_id, kind, name) DO NOTHING
    """,
    """
    INSERT INTO project_profile (tag_id, org_id, client_tag_id)
    SELECT p.id, p.org_id,
           (SELECT c.id FROM tags c
             WHERE c.org_id = p.org_id AND c.kind = 'client'
               AND c.name = 'Personal' LIMIT 1)
    FROM tags p
    WHERE p.kind = 'project' AND p.name = 'General'
      AND NOT EXISTS (SELECT 1 FROM project_profile pp WHERE pp.tag_id = p.id)
    """,
    # 3. record the default ids in organizations.settings.
    """
    UPDATE organizations o
       SET settings = coalesce(o.settings, '{}'::jsonb)
         || jsonb_build_object(
              'default_client_tag_id',
              (SELECT c.id::text FROM tags c
                WHERE c.org_id = o.id AND c.kind = 'client'
                  AND c.name = 'Personal' LIMIT 1),
              'default_project_tag_id',
              (SELECT p.id::text FROM tags p
                WHERE p.org_id = o.id AND p.kind = 'project'
                  AND p.name = 'General' LIMIT 1))
     WHERE EXISTS (
       SELECT 1 FROM tags p WHERE p.org_id = o.id
         AND p.kind = 'project' AND p.name = 'General')
    """,
    # 4. link every orphan task to its org's General project.
    f"""
    INSERT INTO task_tags (org_id, task_id, tag_id)
    SELECT o.org_id, o.id,
           (SELECT p.id FROM tags p
             WHERE p.org_id = o.org_id AND p.kind = 'project'
               AND p.name = 'General' LIMIT 1)
    FROM ({_ORPHAN}) o
    ON CONFLICT DO NOTHING
    """,
    *(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY" for t in _RLS),
    *(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY" for t in _RLS),
)

# Irreversible data backfill (the link is indistinguishable from a
# user-set one); downgrade is a no-op.
DOWNGRADE: tuple[str, ...] = ()


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
