"""Google Calendar subscriptions + external provenance on events
(epic #125 P1, Google OAuth + ingest).

Adds the ``google_calendar_subscriptions`` table -- a per-user binding
between an internal ``working_calendar`` and a remote Google calendar.
The ``refresh_token_encrypted`` column carries a Fernet envelope (never
plaintext, ADR-0006). Org-scoped + RLS exactly like the other tenant
tables: the policy and ``flow_app`` grants use the canonical org
predicate (``app.current_org``), copied verbatim from the 0046/0048
plain-table create pattern. ``google_calendar_status`` is a NEW native
enum (active|error|disabled), mirroring ``email_account_status``.

Also augments ``events`` with provenance columns so the ingest is
idempotent and reversible:

  - ``external_provider`` short slug (currently "google"; future-proof
    for ical, outlook, ...);
  - ``external_id`` the provider-side id (Google's event id);
  - ``external_subscription_id`` FK to the owning subscription, ON
    DELETE SET NULL so the historical event is kept when the
    subscription is removed (provenance fades but the row survives);
  - a UNIQUE ``(external_subscription_id, external_id)`` index makes
    upserts trivially safe -- "same Google event ingested twice = one
    row" is enforced at the DB layer.

``events`` is already org-scoped + RLS, so the plain column adds
inherit the table's policy/grants (same as the 0029/0037/0042/0044
column-add migrations). Downgrade is symmetric: drop the index then
the columns, then the new table, then the enum type.

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

UPGRADE: tuple[str, ...] = (
    (
        "CREATE TYPE google_calendar_status AS ENUM "
        "('active', 'error', 'disabled')"
    ),
    """
    CREATE TABLE google_calendar_subscriptions (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      our_calendar_id uuid NOT NULL REFERENCES working_calendars(id) ON DELETE CASCADE,
      google_calendar_id varchar(320) NOT NULL,
      refresh_token_encrypted text NOT NULL,
      status google_calendar_status NOT NULL DEFAULT 'active',
      last_sync_at timestamptz,
      last_error text,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_google_calendar_subscriptions PRIMARY KEY (id)
    )
    """,
    (
        "CREATE INDEX ix_google_calendar_subscriptions_org_id "
        "ON google_calendar_subscriptions (org_id)"
    ),
    (
        "CREATE INDEX ix_google_calendar_subscriptions_user_id "
        "ON google_calendar_subscriptions (user_id)"
    ),
    (
        "CREATE INDEX ix_google_calendar_subscriptions_our_calendar_id "
        "ON google_calendar_subscriptions (our_calendar_id)"
    ),
    "ALTER TABLE google_calendar_subscriptions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE google_calendar_subscriptions FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_google_calendar_subscriptions ON google_calendar_subscriptions "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON google_calendar_subscriptions TO flow_app"
    ),
    # Provenance on events: idempotent ingest by (subscription, external_id).
    "ALTER TABLE events ADD COLUMN external_provider varchar(20)",
    "ALTER TABLE events ADD COLUMN external_id varchar(255)",
    (
        "ALTER TABLE events ADD COLUMN external_subscription_id uuid "
        "REFERENCES google_calendar_subscriptions(id) ON DELETE SET NULL"
    ),
    (
        "CREATE UNIQUE INDEX uq_events_external_subscription_id_external_id "
        "ON events (external_subscription_id, external_id) "
        "WHERE external_subscription_id IS NOT NULL"
    ),
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS uq_events_external_subscription_id_external_id",
    "ALTER TABLE events DROP COLUMN IF EXISTS external_subscription_id",
    "ALTER TABLE events DROP COLUMN IF EXISTS external_id",
    "ALTER TABLE events DROP COLUMN IF EXISTS external_provider",
    "DROP TABLE IF EXISTS google_calendar_subscriptions CASCADE",
    "DROP TYPE IF EXISTS google_calendar_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
