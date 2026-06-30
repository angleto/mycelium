"""Rename the event_outbox NOTIFY channel flow.event -> mycelium.event.

Part of the Flow -> Mycelium rebrand. Migration 0049 created
``notify_event_outbox()`` emitting ``pg_notify('flow.event', ...)``; the
event_bus listener now expects ``mycelium.event``. The deferred constraint
trigger ``trg_event_outbox_notify`` calls the function by name, so a
``CREATE OR REPLACE FUNCTION`` is enough -- the trigger itself is untouched.
No data change: the channel is a transient wake-up signal, the outbox rows
are unaffected.

Revision ID: 0058
Revises: 0057
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_event_outbox() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM pg_notify('mycelium.event', NEW.id::text);
          RETURN NULL;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_event_outbox() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM pg_notify('flow.event', NEW.id::text);
          RETURN NULL;
        END
        $$;
        """
    )
