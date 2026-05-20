"""Backfill: every task with a project tag also carries its client tag.

The runtime (``services/tasks.create_task`` and ``attach_tag``) now
enforces ``task → client`` next to ``task → project``. Pre-existing
rows minted before this enforcement may be missing the client_tag_id
side of the link; this migration walks ``task_tags`` joined to
``project_profile`` and inserts the missing ``(task_id, client_tag_id)``
rows.

Idempotent: a NOT EXISTS guard skips tasks that already carry the
client tag (for instance because the SPA started emitting both in
v1.2.7). Safe to re-run on a clean DB.

Revision: 0058
Down revision: 0057
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A task may be linked to multiple projects (rare but allowed); the
# CROSS JOIN expands each project to its client_tag_id and the NOT
# EXISTS filter prevents duplicates against the (task_id, tag_id) PK.
# org_id taken from the project_profile row keeps the new task_tag in
# the correct tenant.
BACKFILL = """
INSERT INTO task_tags (org_id, task_id, tag_id)
SELECT pp.org_id, tt.task_id, pp.client_tag_id
  FROM task_tags tt
  JOIN tags t ON t.id = tt.tag_id AND t.kind = 'project'
  JOIN project_profile pp ON pp.tag_id = tt.tag_id
  WHERE pp.client_tag_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM task_tags x
        WHERE x.task_id = tt.task_id
          AND x.tag_id = pp.client_tag_id
    )
"""


UPGRADE: tuple[str, ...] = (BACKFILL,)


# Down: no-op. The forward migration is data-only and additive; rolling
# back would mean deciding "which client tags did this migration insert
# vs. which were always there?" — that's unrecoverable without a
# distinguishing marker. Leave the rows in place.
DOWNGRADE: tuple[str, ...] = ()


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
