"""Actor handles: human-readable IDs for users / AI assistants / tasks.

Foundation of the "kill Executor" refactor (issue tracker #21).
``Executor`` table stays for now (and the scheduler / dispatch /
advisory keep reading from ``tasks.executor_user_id``); this migration
just adds the new addressable identity so the SPA can start surfacing
``@handle`` next to or instead of the UUID-driven picker.

What lands here:

- ``users.handle`` — slug derived from the email local part, unique
  globally. Empty string allowed temporarily for the back-compat
  default (the seeded admin and any future signup paths populate it
  via ``ensure_user_handle`` in the service layer).
- ``ai_assistants.handle`` — slug derived from the label, unique per
  ``(org_id, handle)``. Same back-compat sentinel.
- ``tasks.assignee_handle`` — the resolved handle of the task's
  current assignee. Backfilled from ``executor_user_id`` -> the user's
  handle. NULL when the task has no human executor (LLM agent or
  unassigned). Renamed assignees cascade via service helpers (next
  stage); for now the column is denormalised but stable.

Cross-source uniqueness (a user and an AI assistant minted in the
same workspace with the same handle) is NOT enforced here — Stage B
of #21 will introduce a ``workspace_handles`` materialised view or a
trigger. Until then a collision in the SPA picker is a UX glitch, not
a data-integrity bug; the resolver walks the three sources in a
documented order.

Revision: 0060
Down revision: 0059
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Slug helper inlined as SQL: lowercase, replace anything non-[a-z0-9]
# with '-', strip leading / trailing dashes, cap at 38 chars (leaving
# room for a dedupe suffix in the service layer if ever needed).
_SLUG_USER = """
UPDATE users SET handle = COALESCE(
  NULLIF(
    btrim(
      substr(
        regexp_replace(
          lower(split_part(email, '@', 1)),
          '[^a-z0-9]+',
          '-',
          'g'
        ),
        1,
        38
      ),
      '-'
    ),
    ''
  ),
  '_u_' || substr(replace(id::text, '-', ''), 1, 8)
)
WHERE handle = '';
"""

_SLUG_ASSISTANT = """
UPDATE ai_assistants SET handle = COALESCE(
  NULLIF(
    btrim(
      substr(
        regexp_replace(
          lower(label),
          '[^a-z0-9]+',
          '-',
          'g'
        ),
        1,
        38
      ),
      '-'
    ),
    ''
  ),
  '_a_' || substr(replace(id::text, '-', ''), 1, 8)
)
WHERE handle = '';
"""

_BACKFILL_TASK_ASSIGNEE = """
UPDATE tasks t
   SET assignee_handle = u.handle
  FROM users u
 WHERE t.executor_user_id = u.id
   AND t.assignee_handle IS NULL
   AND u.handle <> '';
"""


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN handle varchar(40) NOT NULL DEFAULT ''",
    "ALTER TABLE ai_assistants ADD COLUMN handle varchar(40) NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN assignee_handle varchar(40)",
    _SLUG_USER,
    _SLUG_ASSISTANT,
    _BACKFILL_TASK_ASSIGNEE,
    # Partial unique indexes: empty-string defaults are tolerated as a
    # one-off seed sentinel and are NOT counted toward uniqueness. The
    # service layer fills them in on next write of any row that still
    # has the sentinel; from there forward inserts go through the slug
    # mint helper.
    "CREATE UNIQUE INDEX uq_users_handle ON users (handle) WHERE handle <> ''",
    "CREATE UNIQUE INDEX uq_ai_assistants_org_handle "
    "ON ai_assistants (org_id, handle) WHERE handle <> ''",
    "CREATE INDEX ix_tasks_assignee_handle ON tasks (assignee_handle) "
    "WHERE assignee_handle IS NOT NULL",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_tasks_assignee_handle",
    "DROP INDEX IF EXISTS uq_ai_assistants_org_handle",
    "DROP INDEX IF EXISTS uq_users_handle",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS assignee_handle",
    "ALTER TABLE ai_assistants DROP COLUMN IF EXISTS handle",
    "ALTER TABLE users DROP COLUMN IF EXISTS handle",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
