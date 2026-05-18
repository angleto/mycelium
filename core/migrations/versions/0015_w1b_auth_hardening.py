"""W1b: auth hardening (ported from bitvision_phoenix; ADR-0024).

Additive. ``users`` gains email-verification, TOTP MFA and admin
columns; three global (not org-scoped, like ``users``) token tables:
email verification, password reset, revoked JWTs (by ``jti``). No RLS:
these are consumed pre-tenant. flow_app gets table grants (new user
columns are covered by the existing table-level grant on ``users``).

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN display_name varchar(200)",
    "ALTER TABLE users ADD COLUMN is_admin boolean NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN email_verified_at timestamptz",
    "ALTER TABLE users ADD COLUMN mfa_secret varchar(64)",
    "ALTER TABLE users ADD COLUMN mfa_enabled_at timestamptz",
    "ALTER TABLE users ADD COLUMN backup_codes_hash text[]",
    """
    CREATE TABLE email_verification_tokens (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash varchar(64) NOT NULL UNIQUE,
      expires_at timestamptz NOT NULL,
      used_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_email_verification_tokens_user_id"
    " ON email_verification_tokens (user_id)",
    """
    CREATE TABLE password_reset_tokens (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash varchar(64) NOT NULL UNIQUE,
      expires_at timestamptz NOT NULL,
      used_at timestamptz,
      requested_ip varchar(64),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_password_reset_tokens_user_id ON password_reset_tokens (user_id)",
    """
    CREATE TABLE revoked_tokens (
      jti uuid PRIMARY KEY,
      revoked_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      revoked_by uuid,
      reason varchar(512),
      subject_id uuid,
      typ varchar(32),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON email_verification_tokens TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON revoked_tokens TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS revoked_tokens CASCADE",
    "DROP TABLE IF EXISTS password_reset_tokens CASCADE",
    "DROP TABLE IF EXISTS email_verification_tokens CASCADE",
    "ALTER TABLE users DROP COLUMN IF EXISTS backup_codes_hash",
    "ALTER TABLE users DROP COLUMN IF EXISTS mfa_enabled_at",
    "ALTER TABLE users DROP COLUMN IF EXISTS mfa_secret",
    "ALTER TABLE users DROP COLUMN IF EXISTS email_verified_at",
    "ALTER TABLE users DROP COLUMN IF EXISTS is_admin",
    "ALTER TABLE users DROP COLUMN IF EXISTS display_name",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
