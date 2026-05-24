"""Backfill empty actor handles and materialise the identity rows that
the 0084 / 0085 chain skipped.

Migration 0060 introduced ``users.handle`` and ``ai_assistants.handle``
with an empty-string sentinel. Service-layer code was supposed to mint
the slug on next write, but signup never called ``mint_user_handle``
and ``create_assistant`` never called ``mint_assistant_handle`` — so
rows created after 0060 stayed at ``handle = ''``.

Migration 0084 then backfilled the ``identities`` table from
memberships and ai_assistants — but only ``WHERE handle <> ''``.
0085 added INSERT triggers that auto-create identity rows on
membership / assistant insert — but they short-circuit on the empty
handle sentinel.

Net effect: any user signed up between 0060 and now, and any AI
assistant created between 0060 and now, has an empty handle and no
identity row. The assignee picker (``GET /actors``) sources from the
source tables (filtered to ``handle <> ''``) so empty-handle rows are
invisible there. And on PATCH /tasks ``assignee_handle`` is resolved
against the ``identities`` table, so even the rare row that did get a
handle but no identity (e.g. user backfilled by 0060 whose membership
predated identities) hits ``DomainError(DOMAIN_ERROR)``.

This migration re-runs the 0060 slug logic for any still-empty handle
and re-runs the 0084 identity backfill for everyone, idempotently.

Revision: 0099
Down revision: 0098
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Backfill empty handles with a UUID-derived sentinel. We can't reuse
# 0060's email/label slug here because rows minted between 0060 and
# now would now collide with any pretty handle already taken by an
# earlier user/assistant. The UUID form is collision-free by
# construction; the user can rename later through the SPA.
_SLUG_USER = """
UPDATE users
   SET handle = '_u_' || substr(replace(id::text, '-', ''), 1, 8)
 WHERE handle = '';
"""

_SLUG_ASSISTANT = """
UPDATE ai_assistants
   SET handle = '_a_' || substr(replace(id::text, '-', ''), 1, 8)
 WHERE handle = '';
"""

# Mirror of the 0084 identity backfill. Idempotent via ON CONFLICT.
_BACKFILL_USER_IDENTITIES = """
INSERT INTO identities (org_id, kind, handle, user_id)
SELECT m.org_id, 'user', u.handle, u.id
FROM memberships m
JOIN users u ON u.id = m.user_id
WHERE u.handle <> ''
ON CONFLICT (org_id, handle) DO NOTHING
"""

_BACKFILL_ASSISTANT_IDENTITIES = """
INSERT INTO identities (org_id, kind, handle, ai_assistant_id)
SELECT a.org_id, 'ai_assistant', a.handle, a.id
FROM ai_assistants a
WHERE a.handle <> ''
ON CONFLICT (org_id, handle) DO NOTHING
"""


UPGRADE: tuple[str, ...] = (
    _SLUG_USER,
    _SLUG_ASSISTANT,
    _BACKFILL_USER_IDENTITIES,
    _BACKFILL_ASSISTANT_IDENTITIES,
)


# No-op downgrade: undoing a backfill would invalidate the FK targets
# that the assigned tasks already point at.
DOWNGRADE: tuple[str, ...] = ()


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
