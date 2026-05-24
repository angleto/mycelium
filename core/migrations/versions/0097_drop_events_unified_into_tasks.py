"""Drop the legacy ``events`` and ``event_participants`` tables; move
the Google Calendar sync state (``external_provider``, ``external_id``,
``external_subscription_id``) onto ``tasks`` so the SPA / MCP / GCal
adapter all converge on the unified appointment-task model
(migrations 0094 + 0095 + 0096, ADR-0008 addendum).

Why now: every consumer that used Event was already migrated in the
preceding work — SPA EventsRoute reads /tasks (commit 5039964),
scheduler keys hard constraints off ``task_participants`` (commits
44fb189 + f523d55), advisory and taxonomy got rewritten in this PR.
The only remaining hook was the Google Calendar ingest path; it
ingests / pushes appointment-tasks now.

The user confirmed no live ``events`` rows on any deployed DB
(personal solo deploy), so this migration drops the tables outright
without a data backfill. If a future deploy needs to migrate live
event rows, the backfill is a one-shot:
``INSERT INTO tasks(...) SELECT ... FROM events`` mirroring the
columns.

Revision: 0097
Down revision: 0096
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    # External-sync provenance moved from events to tasks. NULL on
    # native appointment-tasks; non-NULL when the row mirrors a remote
    # provider (currently "google"). The partial UNIQUE enforces
    # idempotent ingest under the same natural key the events table
    # used.
    "ALTER TABLE tasks ADD COLUMN external_provider varchar(20)",
    "ALTER TABLE tasks ADD COLUMN external_id varchar(255)",
    "ALTER TABLE tasks ADD COLUMN external_subscription_id uuid",
    """
    CREATE UNIQUE INDEX ux_tasks_external_sync
      ON tasks (external_subscription_id, external_id)
      WHERE external_subscription_id IS NOT NULL AND external_id IS NOT NULL
    """,
    # Drop the legacy tables. CASCADE removes FK-bound rows in
    # event_participants if the events drop were attempted in the
    # wrong order; both go.
    "DROP TABLE IF EXISTS event_participants CASCADE",
    "DROP TABLE IF EXISTS events CASCADE",
)


DOWNGRADE: tuple[str, ...] = (
    # Re-create the bare minimum to satisfy a manual rollback. The
    # CHECK + indexes / RLS are intentionally light: a rollback is
    # only useful for migration tests, not for restoring data
    # (events on prod were empty when this migration ran).
    """
    CREATE TABLE events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL,
      project_tag_id uuid,
      client_tag_id uuid,
      title varchar(300) NOT NULL,
      start_at timestamptz NOT NULL,
      end_at timestamptz NOT NULL,
      location varchar(200),
      external_provider varchar(20),
      external_id varchar(255),
      external_subscription_id uuid,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_events_interval CHECK (end_at > start_at)
    )
    """,
    """
    CREATE TABLE event_participants (
      event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      org_id uuid NOT NULL,
      CONSTRAINT pk_event_participants PRIMARY KEY (event_id, user_id)
    )
    """,
    "ALTER TABLE events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE events FORCE ROW LEVEL SECURITY",
    "ALTER TABLE event_participants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE event_participants FORCE ROW LEVEL SECURITY",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON events TO flow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON event_participants TO flow_app",
    "DROP INDEX IF EXISTS ux_tasks_external_sync",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS external_subscription_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS external_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS external_provider",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
