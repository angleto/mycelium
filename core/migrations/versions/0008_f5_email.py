"""F5 (additive): email connector. ``email_accounts`` (opaque secret
stored as a Fernet envelope, ADR-0006) and ``email_messages`` with a
per-account idempotency key. RLS+FORCE + flow_app grants, same patterns
as 0007 (docs/adr/0023, FR-7).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

_RLS_TABLES = ("email_accounts", "email_messages")

UPGRADE: tuple[str, ...] = (
    "CREATE TYPE email_provider AS ENUM ('gmail', 'imap_generic', 'proton_bridge')",
    "CREATE TYPE email_account_status AS ENUM ('active', 'error', 'disabled')",
    """
    CREATE TABLE email_accounts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      provider email_provider NOT NULL,
      email_address varchar(320) NOT NULL,
      display_name varchar(200),
      imap_host varchar(255),
      imap_port integer,
      smtp_host varchar(255),
      smtp_port integer,
      secret_encrypted text NOT NULL,
      status email_account_status NOT NULL DEFAULT 'active',
      last_sync_at timestamptz,
      last_error text,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_email_accounts_org_id UNIQUE (org_id, email_address)
    )
    """,
    "CREATE INDEX ix_email_accounts_org_id ON email_accounts (org_id)",
    """
    CREATE TABLE email_messages (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      account_id uuid NOT NULL
        REFERENCES email_accounts(id) ON DELETE CASCADE,
      provider_message_id varchar(255) NOT NULL,
      thread_id varchar(255),
      message_id varchar(998),
      in_reply_to varchar(998),
      from_addr varchar(320) NOT NULL,
      to_addrs text NOT NULL,
      subject text,
      body_text text,
      snippet varchar(500),
      received_at timestamptz NOT NULL,
      is_read boolean NOT NULL DEFAULT false,
      raw_size integer,
      linked_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_email_messages_account_id
        UNIQUE (account_id, provider_message_id)
    )
    """,
    "CREATE INDEX ix_email_messages_org_id ON email_messages (org_id)",
    "CREATE INDEX ix_email_messages_account_id ON email_messages (account_id)",
    "CREATE INDEX ix_email_messages_thread_id ON email_messages (thread_id)",
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
    op.execute("DROP TABLE IF EXISTS email_messages CASCADE")
    op.execute("DROP TABLE IF EXISTS email_accounts CASCADE")
    op.execute("DROP TYPE IF EXISTS email_account_status")
    op.execute("DROP TYPE IF EXISTS email_provider")
