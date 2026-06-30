"""System-actor enumeration policies for the worker (root-cause fix).

Worker jobs enumerate every workspace with the no-tenant ``admin_session``
(``select(Organization)`` then ``select(Membership)`` to find the owner;
the Google Calendar job reads ``google_calendar_subscriptions``). That
session sets ``app.current_actor_kind='system'`` but no ``app.current_org``
GUC. Under FORCE row-level security the org-scoped policies key off
``app.current_org`` and therefore match nothing: as the ``mycelium_app``
runtime role (no BYPASSRLS) every enumeration returns zero rows, so the
reminders/dispatch/calendar/revisions/embedding sweeps are silent no-ops
in production. CI/dev never saw it because the test role is BYPASSRLS.

Fix: add a permissive ``FOR SELECT`` policy on each table the worker
enumerates, active ONLY for a system session that is enumerating, i.e.

    app.current_actor_kind = 'system'  AND  app.current_org IS NULL

The ``current_org IS NULL`` guard is the safety belt: once a job enters
``tenant_session(org, owner, actor_kind='system')`` the org GUC is set,
this policy goes dormant, and the normal per-org policy is the only one
in force. So the cross-tenant read is scoped to the brief enumeration
window and never widens isolation during the per-org work itself. Only
org metadata (id/name), membership rows and calendar subscriptions are
exposed this way; tasks, notifications and every other org-scoped table
keep the unchanged per-org policy.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A system session that has not yet narrowed to a single org. Mirrors the
# GUCs ``admin_session(actor_kind="system")`` sets (actor_kind only; never
# current_org). ``current_setting(..., true)`` is missing-safe (returns
# NULL when unset), and ``NULLIF(..., '')`` collapses the empty-string
# default to NULL so the guard reads naturally.
_SYSTEM_ENUM = (
    "current_setting('app.current_actor_kind', true) = 'system' "
    "AND NULLIF(current_setting('app.current_org', true), '') IS NULL"
)

# (table, policy) pairs. Each table the worker enumerates before entering a
# tenant_session. Permissive FOR SELECT policies OR with the existing
# per-org / self-read policies, so this only ever ADDS visibility for the
# enumerating system session.
_TABLES: tuple[tuple[str, str], ...] = (
    ("organizations", "p_organizations_system_read"),
    ("memberships", "p_memberships_system_read"),
    ("google_calendar_subscriptions", "p_google_calendar_subscriptions_system_read"),
)


def upgrade() -> None:
    for table, policy in _TABLES:
        op.execute(f"CREATE POLICY {policy} ON {table} FOR SELECT USING ({_SYSTEM_ENUM})")


def downgrade() -> None:
    for table, policy in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
