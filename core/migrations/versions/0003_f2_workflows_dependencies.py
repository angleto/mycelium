"""F2 (additive): configurable workflows + typed task dependencies.

Additive only: tasks.status is untouched here; the state-machine
cutover is migration 0004. Seeds a default workflow per org and makes
provision_organization seed it for new orgs (every org has a default
workflow). RLS+FORCE + grants, same patterns as 0001/0002
(docs/adr/0002, 0004, 0015).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = (
    "workflow_defs",
    "workflow_states",
    "workflow_transitions",
    "task_dependencies",
)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE dependency_type AS ENUM ('FS', 'SS', 'FF', 'SF')",
    """
    CREATE TABLE workflow_defs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      name varchar(120) NOT NULL,
      is_default boolean NOT NULL DEFAULT false,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_workflow_defs_org_id UNIQUE (org_id, name)
    )
    """,
    "CREATE INDEX ix_workflow_defs_org_id ON workflow_defs (org_id)",
    """
    CREATE TABLE workflow_states (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      workflow_id uuid NOT NULL
        REFERENCES workflow_defs(id) ON DELETE CASCADE,
      name varchar(80) NOT NULL,
      ord integer NOT NULL DEFAULT 0,
      is_initial boolean NOT NULL DEFAULT false,
      is_terminal boolean NOT NULL DEFAULT false,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_workflow_states_workflow_id
        UNIQUE (workflow_id, name)
    )
    """,
    "CREATE INDEX ix_workflow_states_org_id ON workflow_states (org_id)",
    "CREATE INDEX ix_workflow_states_workflow_id ON workflow_states (workflow_id)",
    """
    CREATE TABLE workflow_transitions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      workflow_id uuid NOT NULL
        REFERENCES workflow_defs(id) ON DELETE CASCADE,
      from_state_id uuid NOT NULL
        REFERENCES workflow_states(id) ON DELETE CASCADE,
      to_state_id uuid NOT NULL
        REFERENCES workflow_states(id) ON DELETE CASCADE,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_workflow_transitions_workflow_id
        UNIQUE (workflow_id, from_state_id, to_state_id)
    )
    """,
    "CREATE INDEX ix_workflow_transitions_org_id ON workflow_transitions (org_id)",
    "CREATE INDEX ix_workflow_transitions_workflow_id ON workflow_transitions (workflow_id)",
    """
    CREATE TABLE task_dependencies (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      predecessor_id uuid NOT NULL
        REFERENCES tasks(id) ON DELETE CASCADE,
      successor_id uuid NOT NULL
        REFERENCES tasks(id) ON DELETE CASCADE,
      type dependency_type NOT NULL,
      lag_working_minutes integer NOT NULL DEFAULT 0,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_task_dependencies_no_self
        CHECK (predecessor_id <> successor_id),
      CONSTRAINT uq_task_dependencies_predecessor_id
        UNIQUE (predecessor_id, successor_id, type)
    )
    """,
    "CREATE INDEX ix_task_dependencies_org_id ON task_dependencies (org_id)",
    "CREATE INDEX ix_task_dependencies_successor_id ON task_dependencies (successor_id)",
    # Idempotent default-workflow factory (todo -> in_progress -> done).
    """
    CREATE FUNCTION create_default_workflow(p_org uuid)
    RETURNS uuid
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
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
    $fn$
    """,
    "REVOKE ALL ON FUNCTION create_default_workflow(uuid) FROM PUBLIC",
    # Seed existing orgs.
    """
    DO $$
    DECLARE r record;
    BEGIN
      FOR r IN SELECT id FROM organizations LOOP
        PERFORM create_default_workflow(r.id);
      END LOOP;
    END $$
    """,
    # Provisioning now also seeds the default workflow for new orgs.
    """
    CREATE OR REPLACE FUNCTION provision_organization(
      p_name text, p_user_id uuid
    )
    RETURNS uuid
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_org uuid;
    BEGIN
      INSERT INTO organizations (name) VALUES (p_name)
        RETURNING id INTO v_org;
      INSERT INTO memberships (org_id, user_id, role)
        VALUES (v_org, p_user_id, 'owner');
      PERFORM create_default_workflow(v_org);
      RETURN v_org;
    END
    $fn$
    """,
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provision_organization(
          p_name text, p_user_id uuid
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
          v_org uuid;
        BEGIN
          INSERT INTO organizations (name) VALUES (p_name)
            RETURNING id INTO v_org;
          INSERT INTO memberships (org_id, user_id, role)
            VALUES (v_org, p_user_id, 'owner');
          RETURN v_org;
        END
        $fn$
        """
    )
    for table in (
        "task_dependencies",
        "workflow_transitions",
        "workflow_states",
        "workflow_defs",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS create_default_workflow(uuid)")
    op.execute("DROP TYPE IF EXISTS dependency_type")
