"""Identity lifecycle triggers (docs/adr/0028).

Auto-populate ``identities`` whenever a membership or an ai_assistant
is inserted, so the addressing surface stays in sync **by
construction** without service-layer hooks scattered across signup,
invite, assistant-create, etc. SECURITY DEFINER lets the triggers
bypass the ``identities`` RLS policy when they fire from the
provisioning paths (which run under ``admin_session``).

Conflict handling: ``ON CONFLICT (org_id, handle) DO NOTHING``. A
user joining a workspace where a homonymous assistant already has
the same handle simply keeps the existing identity row (an edge
case; handles are user-owned and rare).

Update propagation (handle changes) is intentionally not handled
here: handles are rarely renamed in Flow today, and a robust rename
is a separate ``services/identities.rename`` operation. The trigger
covers INSERT only.

Revision: 0085
Down revision: 0084
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION sync_identity_on_membership_insert()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_handle text;
    BEGIN
      SELECT handle INTO v_handle FROM users WHERE id = NEW.user_id;
      IF v_handle IS NOT NULL AND v_handle <> '' THEN
        INSERT INTO identities (org_id, kind, handle, user_id)
        VALUES (NEW.org_id, 'user', v_handle, NEW.user_id)
        ON CONFLICT (org_id, handle) DO NOTHING;
      END IF;
      RETURN NEW;
    END
    $fn$
    """,
    """
    CREATE TRIGGER trg_sync_identity_on_membership_insert
    AFTER INSERT ON memberships
    FOR EACH ROW EXECUTE FUNCTION sync_identity_on_membership_insert()
    """,
    """
    CREATE OR REPLACE FUNCTION sync_identity_on_ai_assistant_insert()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    BEGIN
      IF NEW.handle IS NOT NULL AND NEW.handle <> '' THEN
        INSERT INTO identities (org_id, kind, handle, ai_assistant_id)
        VALUES (NEW.org_id, 'ai_assistant', NEW.handle, NEW.id)
        ON CONFLICT (org_id, handle) DO NOTHING;
      END IF;
      RETURN NEW;
    END
    $fn$
    """,
    """
    CREATE TRIGGER trg_sync_identity_on_ai_assistant_insert
    AFTER INSERT ON ai_assistants
    FOR EACH ROW EXECUTE FUNCTION sync_identity_on_ai_assistant_insert()
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_sync_identity_on_ai_assistant_insert ON ai_assistants",
    "DROP TRIGGER IF EXISTS trg_sync_identity_on_membership_insert ON memberships",
    "DROP FUNCTION IF EXISTS sync_identity_on_ai_assistant_insert()",
    "DROP FUNCTION IF EXISTS sync_identity_on_membership_insert()",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
