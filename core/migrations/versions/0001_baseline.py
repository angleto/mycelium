"""F0 baseline: extensions, entities, RLS+FORCE, partitioning,
append-only, runtime role and SECURITY DEFINER provisioning.

See docs/adr/0002, 0005, 0007, 0015.

Revision ID: 0001
Revises:
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    # Runtime role, idempotent, WITHOUT a password (set by the
    # bootstrap from env: no secret in git). docs/adr/0015.
    """
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flow_app') THEN
        CREATE ROLE flow_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
      END IF;
    END $$
    """,
    # users: global, NO RLS (login resolves the email before having an
    # org context).
    """
    CREATE TABLE users (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      email varchar(320) NOT NULL,
      password_hash varchar(255) NOT NULL,
      is_active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_users_email UNIQUE (email)
    )
    """,
    """
    CREATE TABLE organizations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name varchar(200) NOT NULL,
      fiscal_profile jsonb,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TYPE role AS ENUM ('owner', 'admin', 'member', 'guest')",
    """
    CREATE TABLE memberships (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      role role NOT NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_memberships_org_id UNIQUE (org_id, user_id)
    )
    """,
    "CREATE INDEX ix_memberships_org_id ON memberships (org_id)",
    "CREATE INDEX ix_memberships_user_id ON memberships (user_id)",
    """
    CREATE TABLE activity_log (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      actor_id uuid,
      entity varchar(80) NOT NULL,
      entity_id uuid,
      action varchar(80) NOT NULL,
      diff jsonb,
      ts timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_activity_log_org_id ON activity_log (org_id)",
    # Append-only: no UPDATE/DELETE, hardened at the DB level.
    """
    CREATE FUNCTION forbid_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
      RAISE EXCEPTION 'append-only table: % not allowed', TG_OP;
    END
    $fn$
    """,
    """
    CREATE TRIGGER trg_activity_log_append_only
    BEFORE UPDATE OR DELETE ON activity_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
    # memory_blobs: PARTITION BY HASH (org_id). The PK must include the
    # partition key. docs/adr/0005, 0007.
    """
    CREATE TABLE memory_blobs (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      project_id uuid,
      namespace varchar(40) NOT NULL DEFAULT 'email',
      tier varchar(8) NOT NULL DEFAULT 'hot',
      text text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_memory_blobs PRIMARY KEY (id, org_id)
    ) PARTITION BY HASH (org_id)
    """,
    """
    DO $$
    DECLARE i int;
    BEGIN
      FOR i IN 0..7 LOOP
        EXECUTE format(
          'CREATE TABLE memory_blobs_p%s PARTITION OF memory_blobs '
          'FOR VALUES WITH (MODULUS 8, REMAINDER %s)', i, i);
      END LOOP;
    END $$
    """,
    "CREATE INDEX ix_memory_blobs_org_id ON memory_blobs (org_id)",
    "CREATE INDEX ix_memory_blobs_project_id ON memory_blobs (project_id)",
    # RLS + FORCE on every org-scoped entity.
    "ALTER TABLE organizations ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE organizations FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memberships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memberships FORCE ROW LEVEL SECURITY",
    "ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE activity_log FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memory_blobs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_blobs FORCE ROW LEVEL SECURITY",
    # Policy: GUC absent -> NULL -> no rows (fail-closed).
    """
    CREATE POLICY p_organizations ON organizations
    USING (id = nullif(current_setting('app.current_org', true), '')::uuid)
    WITH CHECK (id = nullif(current_setting('app.current_org', true), '')::uuid)
    """,
    """
    CREATE POLICY p_memberships ON memberships
    USING (org_id = nullif(current_setting('app.current_org', true), '')::uuid)
    WITH CHECK (org_id = nullif(current_setting('app.current_org', true), '')::uuid)
    """,
    """
    CREATE POLICY p_activity_log ON activity_log
    USING (org_id = nullif(current_setting('app.current_org', true), '')::uuid)
    WITH CHECK (org_id = nullif(current_setting('app.current_org', true), '')::uuid)
    """,
    """
    CREATE POLICY p_memory_blobs ON memory_blobs
    USING (
      org_id = nullif(current_setting('app.current_org', true), '')::uuid
      AND (
        nullif(current_setting('app.current_project', true), '') IS NULL
        OR project_id
           = nullif(current_setting('app.current_project', true), '')::uuid
      )
    )
    WITH CHECK (org_id = nullif(current_setting('app.current_org', true), '')::uuid)
    """,
    # Tenant provisioning: the single point that creates org+membership,
    # SECURITY DEFINER (owner), fixed search_path. docs/adr/0015.
    """
    CREATE FUNCTION provision_organization(p_name text, p_user_id uuid)
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
    """,
    "REVOKE ALL ON FUNCTION provision_organization(text, uuid) FROM PUBLIC",
    # Minimal grants to the runtime role.
    "GRANT USAGE ON SCHEMA public TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON users TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO flow_app",
    "GRANT SELECT, INSERT ON activity_log TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON memory_blobs TO flow_app",
    "GRANT EXECUTE ON FUNCTION provision_organization(text, uuid) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS memory_blobs CASCADE",
    "DROP TABLE IF EXISTS activity_log CASCADE",
    "DROP TABLE IF EXISTS memberships CASCADE",
    "DROP TABLE IF EXISTS organizations CASCADE",
    "DROP TABLE IF EXISTS users CASCADE",
    "DROP FUNCTION IF EXISTS provision_organization(text, uuid)",
    "DROP FUNCTION IF EXISTS forbid_mutation()",
    "DROP TYPE IF EXISTS role",
    "DROP ROLE IF EXISTS flow_app",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
