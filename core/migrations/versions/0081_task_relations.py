"""Symmetric "related task" link (no scheduling semantics).

Unlike ``task_dependencies`` (directional FS/SS/FF/SF edges that feed the
scheduler and have cycle rules), ``task_relations`` is a plain
many-to-many "this is related to that" set used for navigation in the UI.
Symmetry is encoded at storage time: the pair is canonicalised so the
lower UUID lives in ``task_a_id``, which gives a single canonical row per
unordered pair and lets a unique index dedupe natively.

RLS posture matches the post-#48 default (ENABLE, not FORCE): the table
is touched only via tenant sessions, no SECURITY DEFINER reads.

Revision: 0081
Down revision: 0080
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE task_relations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_a_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      task_b_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_task_relations_ordered CHECK (task_a_id < task_b_id),
      CONSTRAINT uq_task_relations_pair UNIQUE (task_a_id, task_b_id)
    )
    """,
    "CREATE INDEX ix_task_relations_org_id ON task_relations (org_id)",
    "CREATE INDEX ix_task_relations_task_b_id ON task_relations (task_b_id)",
    "ALTER TABLE task_relations ENABLE ROW LEVEL SECURITY",
    (
        f"CREATE POLICY p_task_relations ON task_relations "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON task_relations TO flow_app",
)


DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS task_relations CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
