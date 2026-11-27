"""A notification preference's destination is one its person proved.

Until now ``notification_prefs.target`` was free text written by the request,
and any member could write any member's row. The service now derives the
destination and accepts writes only for the caller (see
``services.notifications._proved_destination`` and
api/tests/test_notification_pref_ownership.py). That holds for every write
from here on; this migration makes it hold for the rows already stored, which
were written under the old rule and may point anywhere:

- email: rewritten to the owner's account address;
- telegram: rewritten to the chat bound by the owner's link, or switched off
  with an empty destination when the owner has no link;
- webpush: emptied, its targets are the device subscriptions.

Only rows that differ are touched. ``enabled`` is preserved except for a
telegram row with no link, which could not be delivered to a proved chat.

Not reversible: the free-text destinations are exactly what must not come
back, so downgrade is a no-op.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EMAIL = """
        UPDATE notification_prefs AS p
           SET target = u.email, version = p.version + 1, updated_at = now()
          FROM users AS u
         WHERE u.id = p.user_id
           AND p.channel = 'email'
           AND p.target IS DISTINCT FROM u.email
        """

TELEGRAM_LINKED = """
        UPDATE notification_prefs AS p
           SET target = l.chat_id::text, version = p.version + 1, updated_at = now()
          FROM telegram_links AS l
         WHERE l.user_id = p.user_id
           AND p.channel = 'telegram'
           AND p.target IS DISTINCT FROM l.chat_id::text
        """

TELEGRAM_UNLINKED = """
        UPDATE notification_prefs AS p
           SET target = '', enabled = false, version = p.version + 1, updated_at = now()
         WHERE p.channel = 'telegram'
           AND NOT EXISTS (SELECT 1 FROM telegram_links AS l WHERE l.user_id = p.user_id)
           AND (p.target <> '' OR p.enabled)
        """

WEBPUSH = """
        UPDATE notification_prefs AS p
           SET target = '', version = p.version + 1, updated_at = now()
         WHERE p.channel = 'webpush'
           AND p.target <> ''
        """

# Module constants so the backfill test runs the exact statements on real rows.
STATEMENTS = (EMAIL, TELEGRAM_LINKED, TELEGRAM_UNLINKED, WEBPUSH)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Deliberately empty: restoring free-text destinations would restore the
    # defect. Nothing in the schema changed, so there is nothing else to undo.
    pass
