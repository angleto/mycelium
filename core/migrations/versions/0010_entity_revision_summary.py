"""entity_revision.summary: short, editable, human-friendly label per revision.

A free-text column users can set to give a revision a "speaking name"
("renamed task, dropped cost", "switched to artifact link"). When NULL
the SPA falls back to the comma-joined ``changed_fields`` list. A
worker sweep populates it asynchronously via the open-model LLM
(llama3.2:3b via Ollama, in-cluster); users can also edit it manually
or trigger a regenerate.

The column is plain ``text`` (no length cap at the DB level; the
service trims to 200 chars before INSERT/UPDATE for sanity). The
sealed-immutability trigger from migration 0006 is replaced with a
column-aware version that lets ``summary`` change on a sealed row
while every other column still raises — the worker (and the user, via
PATCH) populates summaries post-seal.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE entity_revision ADD COLUMN summary text NULL")
    # Relax the sealed-immutability trigger from 0006: the only column
    # allowed to change on a sealed row is ``summary``. Every other
    # diff still raises. ``CREATE OR REPLACE FUNCTION`` updates the
    # existing function in place (the trigger keeps pointing at it).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION entity_revision_no_update_sealed()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.sealed_at IS NOT NULL THEN
            IF ROW(NEW.entity_kind, NEW.entity_id, NEW.snapshot,
                    NEW.changed_fields, NEW.channel, NEW.actor_id,
                    NEW.actor_kind, NEW.actor_subject_id,
                    NEW.edit_session_id, NEW.version_from,
                    NEW.version_to, NEW.edit_count,
                    NEW.started_at, NEW.last_edit_at,
                    NEW.sealed_at, NEW.restored_from, NEW.org_id)
               IS DISTINCT FROM
               ROW(OLD.entity_kind, OLD.entity_id, OLD.snapshot,
                    OLD.changed_fields, OLD.channel, OLD.actor_id,
                    OLD.actor_kind, OLD.actor_subject_id,
                    OLD.edit_session_id, OLD.version_from,
                    OLD.version_to, OLD.edit_count,
                    OLD.started_at, OLD.last_edit_at,
                    OLD.sealed_at, OLD.restored_from, OLD.org_id)
            THEN
              RAISE EXCEPTION
                'entity_revision % is sealed and cannot be updated', OLD.id
                USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    # Restore the original "any UPDATE on sealed raises" function,
    # then drop the column.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION entity_revision_no_update_sealed()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.sealed_at IS NOT NULL THEN
            RAISE EXCEPTION
              'entity_revision % is sealed and cannot be updated', OLD.id
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("ALTER TABLE entity_revision DROP COLUMN IF EXISTS summary")
