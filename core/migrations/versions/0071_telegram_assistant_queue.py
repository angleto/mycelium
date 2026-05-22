"""Telegram conversational assistant: durable job queue + conversation
state (ADR-0026, P3).

Two operational tables for the async assistant channel:

- ``telegram_assistant_jobs``: a free-text Telegram message enqueued by
  the webhook (fast ack) and processed by the worker (run the LLM turn,
  send the reply). UNIQUE on ``update_id`` makes the enqueue idempotent
  under Telegram retries. Status: pending -> running -> done|failed.
- ``telegram_conversations``: the last few (role, text) turns per chat,
  so the assistant is multi-turn.

RLS posture mirrors ``telegram_updates`` (migration 0055): both tables
are operational queue/state for the single bot, written from the webhook
and the worker which run under ``admin_session`` (no tenant GUC). They
carry an unrestricted policy (``USING (true)``) -- tenant confinement of
the actual note/task work happens in ``assistant.run_turn`` which opens a
per-user ``tenant_session``. RLS is ENABLED (not FORCE) and EXECUTE/CRUD
is granted to ``flow_app``; we deliberately do NOT use FORCE here (the
admin_session reads would be filtered to zero rows on managed Postgres --
see the #48 / #125 SECURITY DEFINER + FORCE RLS lesson).

Revision: 0071
Down revision: 0070
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE telegram_assistant_jobs (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id bigint NOT NULL,
      update_id bigint NOT NULL,
      prompt_text text NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'pending',
      reply_text text,
      error text,
      started_at timestamptz,
      finished_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_telegram_assistant_jobs PRIMARY KEY (id),
      CONSTRAINT uq_telegram_assistant_jobs_update_id UNIQUE (update_id),
      CONSTRAINT ck_telegram_assistant_jobs_status
        CHECK (status IN ('pending', 'running', 'done', 'failed'))
    )
    """,
    (
        "CREATE INDEX ix_telegram_assistant_jobs_pending "
        "ON telegram_assistant_jobs (created_at) WHERE status = 'pending'"
    ),
    "ALTER TABLE telegram_assistant_jobs ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_telegram_assistant_jobs ON telegram_assistant_jobs "
        "USING (true) WITH CHECK (true)"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_assistant_jobs TO flow_app",
    """
    CREATE TABLE telegram_conversations (
      chat_id bigint NOT NULL,
      turns jsonb NOT NULL DEFAULT '[]'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_telegram_conversations PRIMARY KEY (chat_id)
    )
    """,
    "ALTER TABLE telegram_conversations ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_telegram_conversations ON telegram_conversations "
        "USING (true) WITH CHECK (true)"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_conversations TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS telegram_conversations CASCADE",
    "DROP TABLE IF EXISTS telegram_assistant_jobs CASCADE",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
