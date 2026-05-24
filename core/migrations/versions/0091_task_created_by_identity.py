"""tasks.created_by_identity_id: identify the actor (human or AI)
that created the task.

ADR-0028 introduced ``identities`` as the polymorphic addressable
entity for (user | ai_assistant). The existing ``tasks.created_by``
points to ``users``, so a task created through an MCP agent token
records the human owner of the token rather than the AI assistant
that acted. This migration adds an explicit FK to ``identities`` so
the SPA can render the IdentityBadge for AI-created tasks even when
no assignee is set.

Backfill:
  1) ``activity_log`` rows with ``entity='task' AND action='create' AND
     actor_kind='mcp_token'`` carry ``actor_subject_id = agent_tokens.id``;
     resolve through ``agent_tokens.assistant_id → ai_assistants → identities``
     and write the AI identity onto the task.
  2) Remaining tasks (or those whose mcp_token row no longer resolves)
     fall back to the user identity of ``tasks.created_by``.

Revision: 0091
Down revision: 0090
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    ALTER TABLE tasks
      ADD COLUMN created_by_identity_id uuid
        REFERENCES identities(id) ON DELETE SET NULL
    """,
    "CREATE INDEX ix_tasks_created_by_identity_id ON tasks (created_by_identity_id)",
    # Backfill (1) — tasks created via an MCP agent token: lookup the
    # ai_assistant identity through the token. Picks the earliest
    # 'create' log per task (multiple are possible if the row was
    # touched by tooling that re-emits the action).
    """
    UPDATE tasks t
    SET created_by_identity_id = sub.identity_id
    FROM (
      SELECT DISTINCT ON (al.entity_id)
        al.entity_id::uuid AS task_id,
        i.id AS identity_id
      FROM activity_log al
      JOIN agent_tokens at
        ON at.id = al.actor_subject_id
       AND at.org_id = al.org_id
      JOIN ai_assistants a
        ON a.id = at.assistant_id
       AND a.org_id = al.org_id
      JOIN identities i
        ON i.ai_assistant_id = a.id
       AND i.org_id = al.org_id
      WHERE al.entity = 'task'
        AND al.action = 'create'
        AND al.actor_kind = 'mcp_token'
      ORDER BY al.entity_id, al.ts ASC
    ) sub
    WHERE t.id = sub.task_id
      AND t.created_by_identity_id IS NULL
    """,
    # Backfill (2) — fall back to the user identity for tasks not
    # touched by the AI backfill above. ``identities`` always has a
    # ``kind='user'`` row per (org_id, user_id) thanks to the signup
    # trigger (migration 0083).
    """
    UPDATE tasks t
    SET created_by_identity_id = i.id
    FROM identities i
    WHERE i.org_id = t.org_id
      AND i.user_id = t.created_by
      AND i.kind = 'user'
      AND t.created_by IS NOT NULL
      AND t.created_by_identity_id IS NULL
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_tasks_created_by_identity_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS created_by_identity_id",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
