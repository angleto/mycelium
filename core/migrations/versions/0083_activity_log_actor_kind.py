"""Distinguish actor types on the activity log.

Today the activity log captures ``actor_id`` (a user UUID, or NULL for
system). When an LLM agent run or an MCP-token caller act on a human's
behalf the log records ``actor_id=user`` indistinguishably from the
human clicking in the SPA. This migration adds the missing axis.

- ``actor_kind`` (text, NOT NULL, default 'human_direct') with a check
  constraint over the closed set
  {human_direct, human_api, human_telegram, agent_run, mcp_token,
  system}. We pick text+check over a native enum because the closed
  set may evolve (e.g. an ``adjudication`` kind later); evolving a
  CHECK is a single ALTER, while a native enum requires ALTER TYPE
  ADD VALUE + transactional commit issues on Postgres < 12.
- ``actor_subject_id`` (uuid, NULL) for ``agent_run.id`` /
  ``agent_tokens.id``. NULL for ``human_*`` and ``system`` (the
  caller's user id is already ``actor_id``).

Backward-compatible by construction: every preexisting row gets
``actor_kind='human_direct'`` from the DEFAULT, and the new
GUCs/helpers default to the same value when not specified -- so the
121 ``audit.log`` call sites need not change.

Revision: 0083
Down revision: 0082
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    ALTER TABLE activity_log
      ADD COLUMN actor_kind text NOT NULL DEFAULT 'human_direct',
      ADD COLUMN actor_subject_id uuid
    """,
    """
    ALTER TABLE activity_log
      ADD CONSTRAINT ck_activity_log_actor_kind CHECK (actor_kind IN (
        'human_direct',
        'human_api',
        'human_telegram',
        'agent_run',
        'mcp_token',
        'system'
      ))
    """,
    "CREATE INDEX ix_activity_log_actor_kind ON activity_log (actor_kind)",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_activity_log_actor_kind",
    "ALTER TABLE activity_log DROP CONSTRAINT IF EXISTS ck_activity_log_actor_kind",
    "ALTER TABLE activity_log DROP COLUMN IF EXISTS actor_subject_id",
    "ALTER TABLE activity_log DROP COLUMN IF EXISTS actor_kind",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
