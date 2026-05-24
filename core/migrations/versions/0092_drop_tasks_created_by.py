"""Drop ``tasks.created_by`` — collapse "creator" to ``created_by_identity_id``.

Migration 0091 added ``tasks.created_by_identity_id`` (FK to
``identities``) as the single source of truth for "who created the
task" (polymorphic user | ai_assistant). The legacy ``tasks.created_by``
column (FK to ``users``) carried only the human under the action, so
for an AI-created task it pointed to the user that owned the agent
token rather than to the AI itself — a duplication of the information
already encoded in ``identities`` (a kind=user identity has user_id,
a kind=ai_assistant identity has ai_assistant_id → users via the
assistant's owner).

Accountability (the human responsible for the task) is preserved by
``tasks.owner_id`` (FK to ``users``, NOT NULL, ON DELETE RESTRICT,
populated since migration 0086). Notifications that previously read
``task.created_by`` now read ``task.owner_id``; AI authorship is
queryable via ``created_by_identity_id → identities.kind``.

Last-mile safety: backfill any task whose ``created_by_identity_id``
is still NULL from the soon-to-be-dropped ``created_by`` column so we
don't lose attribution on the way out.

Revision: 0092
Down revision: 0091
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    # Last-mile backfill: any task still missing the identity link
    # gets the user identity of its (about-to-be-dropped) ``created_by``.
    """
    UPDATE tasks t
    SET created_by_identity_id = i.id
    FROM identities i
    WHERE i.org_id = t.org_id
      AND i.user_id = t.created_by
      AND i.kind = 'user'
      AND t.created_by IS NOT NULL
      AND t.created_by_identity_id IS NULL
    """,
    "ALTER TABLE tasks DROP COLUMN created_by",
)


DOWNGRADE: tuple[str, ...] = (
    # Re-create the column and best-effort backfill from the identity.
    """
    ALTER TABLE tasks
      ADD COLUMN created_by uuid REFERENCES users(id) ON DELETE SET NULL
    """,
    """
    UPDATE tasks t
    SET created_by = i.user_id
    FROM identities i
    WHERE i.id = t.created_by_identity_id
      AND i.kind = 'user'
    """,
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
