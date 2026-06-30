"""Seed an enabled email notification channel by default.

Without a ``notification_prefs`` row, ``scan_reminders`` skips the user
(no enabled channel) and ``dispatch_pending`` fails closed, so a brand-new
workspace silently never delivers a reminder. Nothing seeded a channel at
onboarding, so notifications were opt-in and invisible.

Two parts:

1. ``provision_organization`` (the SECURITY DEFINER seam that already seeds
   the default workflow + calendar) also seeds an enabled ``email`` pref
   with the owner's email as the target. It runs with ``app.current_org``
   set to the new org, so the insert satisfies the RLS WITH CHECK.

2. Backfill for existing workspaces -- but ONLY for members who have NO
   active channel at all, so a user who already chose Telegram/email is
   left untouched (no surprise duplicate channel). FORCE RLS with no GUC
   fails closed, so we drop FORCE on the read (memberships) and write
   (notification_prefs) tables for the duration (migration 0011/0023
   pattern), then restore it.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# provision_organization WITH the default-email-pref seed (everything else
# identical to the 0001 baseline definition).
_PROVISION_WITH_SEED = """
CREATE OR REPLACE FUNCTION public.provision_organization(p_name text, p_user_id uuid)
    RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_org uuid := gen_random_uuid();
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_org', v_org::text, true);
  PERFORM set_config('app.current_user', p_user_id::text, true);
  INSERT INTO organizations (id, name) VALUES (v_org, p_name);
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  -- Seed an enabled email channel (target = owner's email) so reminders
  -- and notifications reach the owner out of the box. Idempotent; never
  -- clobbers an existing pref.
  INSERT INTO notification_prefs (org_id, user_id, channel, enabled, target)
  SELECT v_org, p_user_id, 'email', true, u.email
    FROM users u
   WHERE u.id = p_user_id AND u.email <> ''
  ON CONFLICT (org_id, user_id, channel) DO NOTHING;
  -- Restore caller's GUCs so a nested call (e.g. signup inside an
  -- outer tenant_session) does not leave app.current_org/_user
  -- pointing at the new org for the rest of the caller's transaction.
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
  RETURN v_org;
END
$$;
"""

# The original 0001 baseline definition (no seed) -- for downgrade.
_PROVISION_ORIGINAL = """
CREATE OR REPLACE FUNCTION public.provision_organization(p_name text, p_user_id uuid)
    RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_org uuid := gen_random_uuid();
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_org', v_org::text, true);
  PERFORM set_config('app.current_user', p_user_id::text, true);
  INSERT INTO organizations (id, name) VALUES (v_org, p_name);
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
  RETURN v_org;
END
$$;
"""

# Backfill: an enabled email pref for members with NO active channel at all.
# The NOT EXISTS guard leaves users who already picked a channel (e.g.
# Telegram) untouched, so no one gets a surprise second channel.
_BACKFILL = """
INSERT INTO notification_prefs (org_id, user_id, channel, enabled, target)
SELECT m.org_id, m.user_id, 'email', true, u.email
  FROM memberships m
  JOIN users u ON u.id = m.user_id
 WHERE u.email <> ''
   AND NOT EXISTS (
     SELECT 1 FROM notification_prefs np
      WHERE np.org_id = m.org_id
        AND np.user_id = m.user_id
        AND np.enabled = true
        AND np.target <> ''
   )
ON CONFLICT (org_id, user_id, channel) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(_PROVISION_WITH_SEED)
    # FORCE RLS + no GUC fails closed on both the read (memberships) and the
    # write (notification_prefs); drop FORCE for the backfill, then restore.
    op.execute("ALTER TABLE notification_prefs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships NO FORCE ROW LEVEL SECURITY")
    try:
        op.execute(_BACKFILL)
    finally:
        op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE notification_prefs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Restore the seed-free function. The backfilled rows are left in place:
    # deleting them blindly could remove prefs a user has since relied on.
    op.execute(_PROVISION_ORIGINAL)
