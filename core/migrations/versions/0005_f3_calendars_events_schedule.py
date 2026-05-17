"""F3 (additive): working calendars, events (no-ubiquity), derived
schedule table, and task scheduler fields. Seeds a default working
calendar per org (provision_organization). RLS+FORCE + grants, same
patterns as 0001/0002/0003 (docs/adr/0002, 0004, 0008).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = (
    "working_calendars",
    "calendar_holidays",
    "user_calendar",
    "events",
    "event_participants",
    "schedule",
)

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE schedule_mode AS ENUM ('auto', 'manual')",
    "CREATE TYPE constraint_kind AS ENUM ('none', 'SNET', 'MSO', 'MFO')",
    """
    CREATE TABLE working_calendars (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      name varchar(120) NOT NULL,
      is_default boolean NOT NULL DEFAULT false,
      timezone varchar(64) NOT NULL DEFAULT 'Europe/Rome',
      weekly_hours jsonb NOT NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_working_calendars_org_id UNIQUE (org_id, name)
    )
    """,
    "CREATE INDEX ix_working_calendars_org_id ON working_calendars (org_id)",
    """
    CREATE TABLE calendar_holidays (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      calendar_id uuid NOT NULL
        REFERENCES working_calendars(id) ON DELETE CASCADE,
      day date NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_calendar_holidays_calendar_id
        UNIQUE (calendar_id, day)
    )
    """,
    "CREATE INDEX ix_calendar_holidays_org_id ON calendar_holidays (org_id)",
    """
    CREATE TABLE user_calendar (
      org_id uuid NOT NULL,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      calendar_id uuid NOT NULL
        REFERENCES working_calendars(id) ON DELETE CASCADE,
      daily_capacity_h numeric(5, 2) NOT NULL DEFAULT 8,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_user_calendar PRIMARY KEY (org_id, user_id)
    )
    """,
    """
    CREATE TABLE events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      project_tag_id uuid,
      client_tag_id uuid,
      title varchar(300) NOT NULL,
      start_at timestamptz NOT NULL,
      end_at timestamptz NOT NULL,
      location varchar(200),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_events_interval CHECK (end_at > start_at)
    )
    """,
    "CREATE INDEX ix_events_org_id ON events (org_id)",
    """
    CREATE TABLE event_participants (
      event_id uuid NOT NULL
        REFERENCES events(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      CONSTRAINT pk_event_participants
        PRIMARY KEY (event_id, user_id)
    )
    """,
    "CREATE INDEX ix_event_participants_org_id ON event_participants (org_id)",
    "CREATE INDEX ix_event_participants_user_id ON event_participants (user_id)",
    """
    CREATE TABLE schedule (
      task_id uuid PRIMARY KEY
        REFERENCES tasks(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      es timestamptz,
      ef timestamptz,
      ls timestamptz,
      lf timestamptz,
      slack_minutes integer,
      on_logical_critical_path boolean NOT NULL DEFAULT false,
      scheduled_start timestamptz,
      scheduled_end timestamptz,
      computed_at timestamptz NOT NULL DEFAULT now(),
      input_fingerprint text
    )
    """,
    "CREATE INDEX ix_schedule_org_id ON schedule (org_id)",
    "ALTER TABLE tasks ADD COLUMN remaining_effort_h numeric(8, 2)",
    "ALTER TABLE tasks ADD COLUMN actual_start timestamptz",
    "ALTER TABLE tasks ADD COLUMN schedule_mode schedule_mode NOT NULL DEFAULT 'auto'",
    "ALTER TABLE tasks ADD COLUMN constraint_kind constraint_kind NOT NULL DEFAULT 'none'",
    "ALTER TABLE tasks ADD COLUMN constraint_date timestamptz",
    "ALTER TABLE tasks ADD COLUMN is_milestone boolean NOT NULL DEFAULT false",
    """
    CREATE FUNCTION create_default_calendar(p_org uuid)
    RETURNS uuid
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_cal uuid;
    BEGIN
      SELECT id INTO v_cal FROM working_calendars
        WHERE org_id = p_org AND is_default LIMIT 1;
      IF v_cal IS NOT NULL THEN
        RETURN v_cal;
      END IF;
      INSERT INTO working_calendars
        (org_id, name, is_default, weekly_hours)
      VALUES (
        p_org, 'Default', true,
        ('{"mon":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"tue":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"wed":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"thu":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"fri":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"sat":[],"sun":[]}'::jsonb)
      ) RETURNING id INTO v_cal;
      RETURN v_cal;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION create_default_calendar(uuid) FROM PUBLIC",
    """
    DO $$
    DECLARE r record;
    BEGIN
      FOR r IN SELECT id FROM organizations LOOP
        PERFORM create_default_calendar(r.id);
      END LOOP;
    END $$
    """,
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
      PERFORM create_default_calendar(v_org);
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
          PERFORM create_default_workflow(v_org);
          RETURN v_org;
        END
        $fn$
        """
    )
    for col in (
        "is_milestone",
        "constraint_date",
        "constraint_kind",
        "schedule_mode",
        "actual_start",
        "remaining_effort_h",
    ):
        op.execute(f"ALTER TABLE tasks DROP COLUMN IF EXISTS {col}")
    for table in (
        "schedule",
        "event_participants",
        "events",
        "user_calendar",
        "calendar_holidays",
        "working_calendars",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS create_default_calendar(uuid)")
    op.execute("DROP TYPE IF EXISTS constraint_kind")
    op.execute("DROP TYPE IF EXISTS schedule_mode")
