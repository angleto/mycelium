"""Make resolve_telegram_chat set app.current_user so it can read the
linked user's memberships under FORCE RLS.

Second layer of the same #125 webhook bug fixed in 0069. After 0069
dropped FORCE on the telegram tables, ``consume_telegram_link_code``
works and the link is created, but the first plain message to the bot
replies "Your Flow account has no workspace yet": ``resolve_telegram_chat``
(migration 0055, SECURITY DEFINER, run from admin_session with no GUC)
finds the user in ``telegram_links`` but its inline subquery on
``memberships`` returns NULL.

``memberships`` has FORCE ROW LEVEL SECURITY and we deliberately do NOT
drop it (unlike the telegram tables): it is a core tenant table, and
0051/0053 established the no-BYPASSRLS convention for it. The org-scoped
policy ``p_memberships`` keys on ``app.current_org`` (unknown here —
it's exactly what we're resolving), but 0051 also added
``p_memberships_self_read`` (``FOR SELECT USING (user_id = current_user)``).
So the fix mirrors the 0053 workspace functions: resolve the user_id
from telegram_links first (no FORCE since 0069, owner reads it), then
``set_config('app.current_user', v_user_id, true)`` so the self-read
policy authorises the membership lookup, all without BYPASSRLS.

The GUC is transaction-local: the webhook calls this inside a dedicated
admin_session transaction that ends right after, so nothing leaks into
the note/task creation that follows (those open their own tenant_session).

The function is no longer STABLE (it now calls set_config). Signature /
OUT params are unchanged, so CREATE OR REPLACE preserves the existing
GRANT EXECUTE to flow_app.

No-op in dev/CI (postgres superuser bypasses RLS regardless).

Revision: 0070
Down revision: 0069
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION resolve_telegram_chat(
      p_chat_id bigint,
      OUT out_user_id uuid,
      OUT out_default_org_id uuid
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
      -- telegram_links lost FORCE RLS in 0069, so the function owner
      -- reads the link row directly by chat_id.
      SELECT l.user_id INTO v_user_id
      FROM telegram_links l
      WHERE l.chat_id = p_chat_id;

      IF v_user_id IS NULL THEN
        RETURN;
      END IF;

      -- memberships keeps FORCE RLS. Satisfy p_memberships_self_read
      -- (USING user_id = current_user) so this SECURITY DEFINER body can
      -- read the user's own membership rows without BYPASSRLS. Local to
      -- the current (admin_session) transaction.
      PERFORM set_config('app.current_user', v_user_id::text, true);

      SELECT m.org_id INTO v_org_id
      FROM memberships m
      WHERE m.user_id = v_user_id
      ORDER BY m.created_at ASC
      LIMIT 1;

      out_user_id := v_user_id;
      out_default_org_id := v_org_id;
      RETURN NEXT;
    END
    $fn$
    """,
)


DOWNGRADE: tuple[str, ...] = (
    # Restore the original 0055 body (STABLE, inline subquery, no GUC).
    """
    CREATE OR REPLACE FUNCTION resolve_telegram_chat(
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
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
