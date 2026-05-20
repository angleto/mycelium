"""Telegram bot integration: link codes, per-user link, update dedupe.

Epic #125 P2 backend (Telegram bot). Three new tables:

- ``telegram_link_codes``: short-lived single-use deep-link codes,
  org-scoped (the workspace the user was in when they pressed
  "Link Telegram"), RLS-keyed on ``app.current_org`` like every
  other tenant table.
- ``telegram_links``: per-user single Telegram identity,
  ``user_id`` PK + ``chat_id`` UNIQUE. Not org-scoped (the link
  belongs to the human, not to a workspace). The RLS policy keys on
  the caller's ``app.current_user`` GUC so a user only sees their
  own link, mirroring ``p_memberships_self_read`` from 0051.
- ``telegram_updates``: ``update_id`` PK seen-set for exactly-once
  webhook delivery. RLS-enabled with an unrestricted policy: the
  bot webhook runs as ``flow_app`` (admin_session, no tenant GUC)
  and the seen-set is global by design (update_ids come from
  Telegram and are monotonically increasing across the bot, not
  per-workspace).

Two SECURITY DEFINER helpers let the webhook do its job without a
tenant GUC (the webhook carries no Flow auth context):

- ``consume_telegram_link_code(code, chat_id, chat_username)``
  atomically redeems a code + upserts the link.
- ``resolve_telegram_chat(chat_id)`` returns the link's user_id +
  their earliest membership org_id for routing incoming messages.

Note on numbering: the workspace-funcs fix landed 0053 on this branch
ahead of this commit (the parallel P1 agent's 0054 migration was not
present in the working tree at write time), so this migration chains
directly onto 0053 but is numbered 0055 to leave room for P1 to slot
0054 ahead of us; if P1 lands first, this file's ``down_revision``
must be bumped to ``"0054"`` and renumbered accordingly.

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"
_USER = "nullif(current_setting('app.current_user', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE telegram_link_codes (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      code varchar(32) NOT NULL,
      expires_at timestamptz NOT NULL,
      consumed_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_telegram_link_codes PRIMARY KEY (id),
      CONSTRAINT uq_telegram_link_codes_code UNIQUE (code),
      CONSTRAINT ck_telegram_link_codes_length
        CHECK (length(code) BETWEEN 6 AND 32)
    )
    """,
    "CREATE INDEX ix_telegram_link_codes_org_id ON telegram_link_codes (org_id)",
    "CREATE INDEX ix_telegram_link_codes_user_id ON telegram_link_codes (user_id)",
    (
        "CREATE INDEX ix_telegram_link_codes_user_pending "
        "ON telegram_link_codes (user_id) WHERE consumed_at IS NULL"
    ),
    "ALTER TABLE telegram_link_codes ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE telegram_link_codes FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_telegram_link_codes ON telegram_link_codes "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_link_codes TO flow_app",
    """
    CREATE TABLE telegram_links (
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id bigint NOT NULL,
      chat_username varchar(64),
      linked_at timestamptz NOT NULL DEFAULT now(),
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_telegram_links PRIMARY KEY (user_id),
      CONSTRAINT uq_telegram_links_chat_id UNIQUE (chat_id)
    )
    """,
    "ALTER TABLE telegram_links ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE telegram_links FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_telegram_links_self ON telegram_links "
        f"USING (user_id = {_USER}) WITH CHECK (user_id = {_USER})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_links TO flow_app",
    """
    CREATE TABLE telegram_updates (
      update_id bigint NOT NULL,
      received_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_telegram_updates PRIMARY KEY (update_id)
    )
    """,
    "ALTER TABLE telegram_updates ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE telegram_updates FORCE ROW LEVEL SECURITY",
    "CREATE POLICY p_telegram_updates ON telegram_updates USING (true) WITH CHECK (true)",
    "GRANT SELECT, INSERT ON telegram_updates TO flow_app",
    # OUT-parameter return shape avoids the PL/pgSQL variable vs
    # output column ambiguity (asyncpg surfaces it as AmbiguousColumnError).
    """
    CREATE FUNCTION consume_telegram_link_code(
      p_code text,
      p_chat_id bigint,
      p_chat_username text,
      OUT out_user_id uuid,
      OUT out_org_id uuid
    )
    RETURNS SETOF record
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_user_id uuid;
      v_org_id uuid;
    BEGIN
      UPDATE telegram_link_codes c
      SET consumed_at = now()
      WHERE c.code = p_code
        AND c.consumed_at IS NULL
        AND c.expires_at > now()
      RETURNING c.user_id, c.org_id
      INTO v_user_id, v_org_id;

      IF v_user_id IS NULL THEN
        RETURN;
      END IF;

      INSERT INTO telegram_links (user_id, chat_id, chat_username, linked_at)
      VALUES (v_user_id, p_chat_id, p_chat_username, now())
      ON CONFLICT (user_id) DO UPDATE
        SET chat_id = EXCLUDED.chat_id,
            chat_username = EXCLUDED.chat_username,
            linked_at = now(),
            version = telegram_links.version + 1,
            updated_at = now();

      out_user_id := v_user_id;
      out_org_id := v_org_id;
      RETURN NEXT;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION consume_telegram_link_code(text, bigint, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION consume_telegram_link_code(text, bigint, text) TO flow_app",
    """
    CREATE FUNCTION resolve_telegram_chat(
      p_chat_id bigint,
      OUT out_user_id uuid,
      OUT out_default_org_id uuid
    )
    RETURNS SETOF record
    LANGUAGE plpgsql
    STABLE
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    BEGIN
      RETURN QUERY
        SELECT l.user_id,
               (
                 SELECT m.org_id FROM memberships m
                 WHERE m.user_id = l.user_id
                 ORDER BY m.created_at ASC
                 LIMIT 1
               )
        FROM telegram_links l
        WHERE l.chat_id = p_chat_id;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION resolve_telegram_chat(bigint) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION resolve_telegram_chat(bigint) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS resolve_telegram_chat(bigint)",
    "DROP FUNCTION IF EXISTS consume_telegram_link_code(text, bigint, text)",
    "DROP TABLE IF EXISTS telegram_updates CASCADE",
    "DROP TABLE IF EXISTS telegram_links CASCADE",
    "DROP TABLE IF EXISTS telegram_link_codes CASCADE",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
