"""tasks.created_by_token_id: identify the MCP agent_token behind
"bare" tokens (assistant_id IS NULL), so AI authorship survives even
when the token was generated before migration 0059 introduced
ai_assistants.

ADR-0028 hooks AI authorship onto the ``identities`` table through
``agent_tokens.assistant_id → ai_assistants → identities`` (an AI
"kind=ai_assistant" identity per assistant). A bare agent_token has
``assistant_id = NULL``, so the JOIN returns nothing and
``tasks.created_by_identity_id`` falls back to the user identity of
the human owner of the token. That collapses the "AI vs human"
distinction in /tasks for legacy tokens.

The fix is to record the agent_token id on the task itself when the
caller principal is an mcp_token. ``ai_assistants.label`` (when bound)
or ``agent_tokens.name`` (bare) is then surfaced by the serializer as
the display label, and the SPA renders the bot icon regardless.

Backfill: any task whose activity_log "create" row carries
``actor_kind='mcp_token'`` gets its ``created_by_token_id`` set from
``activity_log.actor_subject_id`` (the agent_tokens.id).

Revision: 0093
Down revision: 0092
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    ALTER TABLE tasks
      ADD COLUMN created_by_token_id uuid
        REFERENCES agent_tokens(id) ON DELETE SET NULL
    """,
    "CREATE INDEX ix_tasks_created_by_token_id ON tasks (created_by_token_id)",
    # Backfill: earliest 'create' activity_log row per task whose
    # actor_kind is 'mcp_token' carries the agent_token id we want.
    """
    UPDATE tasks t
    SET created_by_token_id = sub.token_id
    FROM (
      SELECT DISTINCT ON (al.entity_id)
        al.entity_id::uuid AS task_id,
        at.id AS token_id
      FROM activity_log al
      JOIN agent_tokens at
        ON at.id = al.actor_subject_id
       AND at.org_id = al.org_id
      WHERE al.entity = 'task'
        AND al.action = 'create'
        AND al.actor_kind = 'mcp_token'
      ORDER BY al.entity_id, al.ts ASC
    ) sub
    WHERE t.id = sub.task_id
      AND t.created_by_token_id IS NULL
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_tasks_created_by_token_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS created_by_token_id",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
