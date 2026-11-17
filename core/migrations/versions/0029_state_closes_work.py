"""A workflow state says whether entering it declares the work done, and a
task says who handed it over.

``workflow_states.closes_work`` is what the close guard in ``tasks.set_state``
reads: a model (an agent, or a Telegram turn) enters such a state only after
a check made by another worker. It is an attribute of the state and not a
name, for the reason ``is_terminal`` is: the workflow is configuration and a
project can override it, so ``done`` is whatever a workflow calls it.

It is not ``is_terminal`` either. A terminal state can end the work without
declaring it done -- ``wontfix``, ``duplicated`` -- and those need no check:
they say the work will not be done, which is not a claim a verifier can
refute by reading it.

Backfill: every terminal state closes work, which is the safe reading of a
state nobody has classified yet (a model is asked for a check it could have
skipped, never the reverse). A workspace whose terminal states include ones
like ``wontfix`` turns the flag off on those with ``update_workflow``.

``create_default_workflow`` is redefined so a new workspace's ``done`` is
born with the flag on; its body is otherwise the baseline's.

``tasks.handed_over_by_worker_id`` is the other half the guard reads: the
worker that passed the task into the state it is in now. It is a position
rather than a search of the lease history, because a handoff from before the
task was last reopened would otherwise still count, and ordering a lease's
release against a move would compare the application's clock with the
database's.

Writing a workflow becomes a person's act, and the ``workflows:write`` scope
key is retired from every stored grant: it decided where an agent's work
needs another worker's check, and an agent holding it could turn the check
off for itself.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exported for the test, which runs these exact statements against real rows.
RETIRE_WORKFLOWS_WRITE: tuple[str, ...] = (
    "UPDATE ai_assistants SET scope = scope - 'workflows:write' WHERE scope ? 'workflows:write'",
    "UPDATE oauth_authorization_requests SET granted_scope = granted_scope - 'workflows:write' "
    "WHERE granted_scope ? 'workflows:write'",
)


_DEFAULT_WORKFLOW = """
CREATE OR REPLACE FUNCTION public.create_default_workflow(p_org uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_wf uuid;
      s_todo uuid;
      s_prog uuid;
      s_done uuid;
    BEGIN
      SELECT id INTO v_wf FROM workflow_defs
        WHERE org_id = p_org AND is_default LIMIT 1;
      IF v_wf IS NOT NULL THEN
        RETURN v_wf;
      END IF;
      INSERT INTO workflow_defs (org_id, name, is_default)
        VALUES (p_org, 'Default', true) RETURNING id INTO v_wf;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal, closes_work)
        VALUES (p_org, v_wf, 'todo', 1, true, false, false)
        RETURNING id INTO s_todo;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal, closes_work)
        VALUES (p_org, v_wf, 'in_progress', 2, false, false, false)
        RETURNING id INTO s_prog;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal, closes_work)
        VALUES (p_org, v_wf, 'done', 3, false, true, true)
        RETURNING id INTO s_done;
      INSERT INTO workflow_transitions
        (org_id, workflow_id, from_state_id, to_state_id)
      VALUES
        (p_org, v_wf, s_todo, s_prog),
        (p_org, v_wf, s_prog, s_done),
        (p_org, v_wf, s_prog, s_todo),
        (p_org, v_wf, s_done, s_prog);
      RETURN v_wf;
    END
    $$;
"""

_BASELINE_DEFAULT_WORKFLOW = """
CREATE OR REPLACE FUNCTION public.create_default_workflow(p_org uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_wf uuid;
      s_todo uuid;
      s_prog uuid;
      s_done uuid;
    BEGIN
      SELECT id INTO v_wf FROM workflow_defs
        WHERE org_id = p_org AND is_default LIMIT 1;
      IF v_wf IS NOT NULL THEN
        RETURN v_wf;
      END IF;
      INSERT INTO workflow_defs (org_id, name, is_default)
        VALUES (p_org, 'Default', true) RETURNING id INTO v_wf;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'todo', 1, true, false)
        RETURNING id INTO s_todo;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'in_progress', 2, false, false)
        RETURNING id INTO s_prog;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'done', 3, false, true)
        RETURNING id INTO s_done;
      INSERT INTO workflow_transitions
        (org_id, workflow_id, from_state_id, to_state_id)
      VALUES
        (p_org, v_wf, s_todo, s_prog),
        (p_org, v_wf, s_prog, s_done),
        (p_org, v_wf, s_prog, s_todo),
        (p_org, v_wf, s_done, s_prog);
      RETURN v_wf;
    END
    $$;
"""


def upgrade() -> None:
    # ADD COLUMN with a constant default is a catalog change, but it takes an
    # ACCESS EXCLUSIVE lock on workflow_states; behind an open transaction
    # reading it every task move would queue. Ten seconds, then fail.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "workflow_states",
        sa.Column("closes_work", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE workflow_states SET closes_work = true WHERE is_terminal")
    op.execute(_DEFAULT_WORKFLOW)
    # Nullable, no default, no backfill: a catalog change only. Every task
    # starts with nobody having handed it over, which is the safe reading --
    # a model asked for a check it could have skipped, never the reverse.
    op.add_column(
        "tasks",
        sa.Column(
            "handed_over_by_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "agent_workers.id",
                ondelete="SET NULL",
                name="fk_tasks_handed_over_by_worker_id_agent_workers",
            ),
            nullable=True,
        ),
    )

    # Writing a workflow became a person's act, so the key that granted it
    # to an assistant reaches nothing. Left in a stored grant it would still
    # be shown as a permission the assistant holds, and the catalogue that
    # validates a grant no longer knows it.
    for stmt in RETIRE_WORKFLOWS_WRITE:
        op.execute(stmt)


def downgrade() -> None:
    # The retired key is not given back: nothing can tell a grant this
    # migration removed from one nobody made, and the code that would honour
    # it is gone with the upgrade.
    op.drop_column("tasks", "handed_over_by_worker_id")
    op.execute(_BASELINE_DEFAULT_WORKFLOW)
    op.drop_column("workflow_states", "closes_work")
