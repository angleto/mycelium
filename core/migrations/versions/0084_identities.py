"""Identity table: polymorphic addressing for users and AI assistants
(docs/adr/0028).

One row per ``(org x user-membership)`` and one per ``ai_assistant``.
Backfilled from the existing ``memberships`` (filtered to ``active``
status) and ``ai_assistants`` rows. UNIQUE(org_id, handle) keeps
handles unambiguous within a workspace.

Empty handles (the per-tables default at row insert before the user
chooses one) are skipped in the backfill: an identity with an empty
handle is meaningless and would violate the partial-unique constraint
that already enforces handle uniqueness on the source tables.

RLS posture is ENABLE (post-#48 default).

Revision: 0084
Down revision: 0083
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TYPE identity_kind AS ENUM ('user', 'ai_assistant')
    """,
    """
    CREATE TABLE identities (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      kind identity_kind NOT NULL,
      handle varchar(40) NOT NULL,
      user_id uuid REFERENCES users(id) ON DELETE CASCADE,
      ai_assistant_id uuid REFERENCES ai_assistants(id) ON DELETE CASCADE,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_identities_exactly_one_subject CHECK (
        (user_id IS NOT NULL) <> (ai_assistant_id IS NOT NULL)
      ),
      CONSTRAINT uq_identities_org_handle UNIQUE (org_id, handle)
    )
    """,
    "CREATE INDEX ix_identities_org_id ON identities (org_id)",
    "CREATE INDEX ix_identities_user_id ON identities (user_id)",
    "CREATE INDEX ix_identities_ai_assistant_id ON identities (ai_assistant_id)",
    "ALTER TABLE identities ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_identities ON identities "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON identities TO flow_app",
    # Backfill: one identity per (org x user-membership) pair with a
    # non-empty handle. ``memberships`` has no ``status`` column; all
    # rows are active.
    """
    INSERT INTO identities (org_id, kind, handle, user_id)
    SELECT m.org_id, 'user', u.handle, u.id
    FROM memberships m
    JOIN users u ON u.id = m.user_id
    WHERE u.handle <> ''
    ON CONFLICT (org_id, handle) DO NOTHING
    """,
    # Backfill: one identity per ai_assistant with a non-empty handle.
    """
    INSERT INTO identities (org_id, kind, handle, ai_assistant_id)
    SELECT a.org_id, 'ai_assistant', a.handle, a.id
    FROM ai_assistants a
    WHERE a.handle <> ''
    ON CONFLICT (org_id, handle) DO NOTHING
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE IF EXISTS identities CASCADE",
    "DROP TYPE IF EXISTS identity_kind",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
