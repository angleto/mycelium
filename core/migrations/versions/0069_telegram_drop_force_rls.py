"""Drop FORCE ROW LEVEL SECURITY on telegram_link_codes + telegram_links
so the SECURITY DEFINER webhook helpers can read/write them.

Same root cause and same fix as migration 0068 (agent_tokens +
ai_assistants), now hit for the first time because the Telegram bot
was only configured in prod after #125 P2 shipped:

``consume_telegram_link_code`` and ``resolve_telegram_chat`` (both
created in 0055, SECURITY DEFINER) run from ``admin_session`` with NO
``app.current_org`` / ``app.current_user`` GUC set, BEFORE any tenant
context exists (the webhook carries no Flow auth). The tables they
touch were created in 0055 with ``FORCE ROW LEVEL SECURITY``:

- ``telegram_link_codes``: ``USING (org_id = current_org)`` → with the
  GUC unset the policy is ``org_id = NULL`` → NULL → every row hidden,
  so the ``UPDATE ... RETURNING`` inside ``consume_telegram_link_code``
  affects zero rows, the function returns no row, and the webhook
  replies "link code invalid or expired" — the link is never created.
- ``telegram_links``: ``USING (user_id = current_user)`` → same, so
  ``resolve_telegram_chat`` finds nothing for an already-linked chat.

With FORCE set, even the table owner is subject to RLS, which is what
blocks the SECURITY DEFINER functions. Migration 0067 tried re-owning
the functions to a BYPASSRLS role, but managed Postgres doesn't grant
BYPASSRLS to the migration role by default; 0068 settled on dropping
FORCE instead. Dropping FORCE restores the canonical Postgres
behavior: the table owner bypasses RLS, and the SECURITY DEFINER
function (owned by the same role as the table, both from 0055)
bypasses on its behalf. RLS stays ENABLED and keeps scoping every
other role (flow_app, the app runtime), so org/user isolation on the
authenticated read paths (create_link_code, get_link_status, unlink)
is unchanged.

``telegram_updates`` is intentionally NOT touched: its policy is
``USING (true)``, so the admin_session dedupe path already works under
FORCE.

In dev / CI this is a behavioral no-op: tests run as the postgres
superuser, which bypasses RLS regardless of FORCE.

Revision: 0069
Down revision: 0068
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE telegram_link_codes NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE telegram_links NO FORCE ROW LEVEL SECURITY",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE telegram_links FORCE ROW LEVEL SECURITY",
    "ALTER TABLE telegram_link_codes FORCE ROW LEVEL SECURITY",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
