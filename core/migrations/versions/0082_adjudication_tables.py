"""Adjudication framework (docs/adr/0027).

Two new tables backing the framework defined in ADR-0027:

- ``adjudications``: one row per arbitration process, with strategy id,
  config, status (running|resolved|escalated|aborted), outcome and
  cost columns; ``VersionMixin`` for optimistic concurrency on
  status/outcome transitions.
- ``adjudication_steps``: append-only event log with a polymorphic
  ``kind`` (``turn``, ``vote``, ``score``, ``escalation``,
  ``synthesis``, ``intervention``, ``tool_call``) and an optional
  ``embedding`` for the kinds that produce text (used by the debate
  convergence detector at P3).

RLS posture is ENABLE (not FORCE), matching the post-#48 default:
the tables are only touched via tenant sessions, no SECURITY DEFINER
reads.

The two native enum types (``adjudication_status`` and
``adjudication_step_kind``) are created up front via ``CREATE TYPE``;
the SQLAlchemy column declarations carry ``create_type=False`` so the
ORM never tries to (re-)create them. The pgvector extension is
already present (migration 0010); reuse the existing dimension 384.

Revision: 0082
Down revision: 0081
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"
_DIM = 384


UPGRADE: tuple[str, ...] = (
    """
    CREATE TYPE adjudication_status AS ENUM (
      'running', 'resolved', 'escalated', 'aborted'
    )
    """,
    """
    CREATE TYPE adjudication_step_kind AS ENUM (
      'turn', 'vote', 'score', 'escalation',
      'synthesis', 'intervention', 'tool_call'
    )
    """,
    """
    CREATE TABLE adjudications (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
      question_text text NOT NULL,
      context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      strategy_id varchar(120) NOT NULL,
      strategy_config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      status adjudication_status NOT NULL,
      outcome_json jsonb,
      confidence numeric(4, 3),
      cost_tokens bigint NOT NULL DEFAULT 0,
      cost_wall_ms bigint NOT NULL DEFAULT 0,
      started_at timestamptz NOT NULL,
      ended_at timestamptz,
      created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_adjudications_org_id ON adjudications (org_id)",
    "CREATE INDEX ix_adjudications_task_id ON adjudications (task_id)",
    "CREATE INDEX ix_adjudications_org_status ON adjudications (org_id, status)",
    "ALTER TABLE adjudications ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_adjudications ON adjudications "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON adjudications TO flow_app",
    f"""
    CREATE TABLE adjudication_steps (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      adjudication_id uuid NOT NULL REFERENCES adjudications(id) ON DELETE CASCADE,
      step_no integer NOT NULL,
      kind adjudication_step_kind NOT NULL,
      payload_json jsonb NOT NULL,
      agent_id varchar(160),
      embedding vector({_DIM}),
      created_at timestamptz NOT NULL,
      CONSTRAINT uq_adjudication_steps_adjudication_id_step_no
        UNIQUE (adjudication_id, step_no)
    )
    """,
    "CREATE INDEX ix_adjudication_steps_org_id ON adjudication_steps (org_id)",
    "CREATE INDEX ix_adjudication_steps_adjudication_id ON adjudication_steps (adjudication_id)",
    "ALTER TABLE adjudication_steps ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_adjudication_steps ON adjudication_steps "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON adjudication_steps TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS adjudication_steps CASCADE",
    "DROP TABLE IF EXISTS adjudications CASCADE",
    "DROP TYPE IF EXISTS adjudication_step_kind",
    "DROP TYPE IF EXISTS adjudication_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
