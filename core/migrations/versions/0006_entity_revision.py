"""Recovery history for task/note edits (``entity_revision``).

A revision is a complete snapshot of a task or a note at a point in
time, together with the channel, actor and editing window that produced
it. Two design choices to make this compatible with autosaving without
exploding into one revision per keystroke:

* **Channel-aware coalescing**: call-grained channels (``mcp``, ``api``,
  ``worker``, ``cli``, ``telegram``) write a sealed-on-arrival revision.
  The keystroke-grained ``web`` channel coalesces into a single open
  revision per ``edit_session_id`` until 30s of idle pass or the client
  explicitly seals. A safety net job in the worker closes orphans
  >60s old.
* **Full snapshot, not delta**: a snapshot is 1-3 kB JSONB; restore and
  diff stay trivial. Storage cost is negligible for a per-edit table.

Polymorphic on ``entity_kind`` ∈ {``task``, ``note``} so the service,
indices, RLS and UI are shared (the two entities have the same mixins,
the same ``optimistic_update`` write path, and the same autosave shape
in the SPA). Cross-entity FK integrity comes from a pair of AFTER
DELETE triggers, since Postgres has no native polymorphic FK.

Sealed revisions are immutable: a BEFORE UPDATE trigger raises on any
attempt to mutate a row whose ``sealed_at`` is non-NULL, so coalescing
can only update the row currently open. Activity-log stays the
security/audit ledger (append-only); entity_revision is the
recovery/UX ledger (sealable, restorable). They are intentionally
ortho-purpose.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "entity_revision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_kind", sa.String(16), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "changed_fields",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_kind", sa.String(40), nullable=False),
        sa.Column("actor_subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("edit_session_id", sa.Text(), nullable=True),
        sa.Column("version_from", sa.BigInteger(), nullable=False),
        sa.Column("version_to", sa.BigInteger(), nullable=False),
        sa.Column("edit_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_edit_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sealed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("restored_from", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "entity_kind IN ('task','note')",
            name="ck_entity_revision_entity_kind",
        ),
        sa.CheckConstraint(
            "channel IN ('web','mcp','api','worker','cli','telegram','restore','system')",
            name="ck_entity_revision_channel",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('human_direct','human_api','human_telegram',"
            "'agent_run','mcp_token','system')",
            name="ck_entity_revision_actor_kind",
        ),
        sa.CheckConstraint(
            "version_to >= version_from",
            name="ck_entity_revision_version_monotonic",
        ),
        sa.CheckConstraint(
            "edit_count >= 1",
            name="ck_entity_revision_edit_count_positive",
        ),
        sa.ForeignKeyConstraint(
            ["restored_from"],
            ["entity_revision.id"],
            ondelete="SET NULL",
            name="fk_entity_revision_restored_from_entity_revision",
        ),
    )

    # Timeline view: most recent first. NULLS FIRST puts the open
    # revision (sealed_at IS NULL) at the head of the list, which is
    # also where the client wants it to display "editing in progress".
    op.execute(
        "CREATE INDEX ix_entity_revision_entity_timeline "
        "ON entity_revision(entity_kind, entity_id, "
        "                   COALESCE(sealed_at, last_edit_at) DESC)"
    )
    # Partial index for the open-revision lookup. Hit on every
    # coalescing append; cardinality stays tiny (one row per active
    # editor per task/note) so the index is small and selective.
    op.execute(
        "CREATE UNIQUE INDEX uq_entity_revision_open "
        "ON entity_revision(entity_kind, entity_id, channel, "
        "                   COALESCE(edit_session_id, ''), "
        "                   COALESCE(actor_id::text, '')) "
        "WHERE sealed_at IS NULL"
    )
    op.create_index(
        "ix_entity_revision_org_id",
        "entity_revision",
        ["org_id"],
    )

    # Sealed revisions are immutable. Coalescing path may only update
    # the row that is currently open (sealed_at IS NULL); once sealed,
    # it's frozen --- restoring produces a NEW revision with
    # ``restored_from`` set, never an in-place mutation.
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
    op.execute(
        "CREATE TRIGGER trg_entity_revision_no_update_sealed "
        "BEFORE UPDATE ON entity_revision "
        "FOR EACH ROW EXECUTE FUNCTION entity_revision_no_update_sealed()"
    )

    # Polymorphic cascade: Postgres has no native polymorphic FK, so
    # we delete dependent revisions in an AFTER DELETE trigger on each
    # source table. The function reads the entity kind from the
    # trigger argument so the same function serves both tables.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION entity_revision_cascade()
        RETURNS TRIGGER AS $$
        BEGIN
          DELETE FROM entity_revision
            WHERE entity_kind = TG_ARGV[0]
              AND entity_id = OLD.id
              AND org_id = OLD.org_id;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_task_revision_cascade "
        "AFTER DELETE ON tasks "
        "FOR EACH ROW EXECUTE FUNCTION entity_revision_cascade('task')"
    )
    op.execute(
        "CREATE TRIGGER trg_note_revision_cascade "
        "AFTER DELETE ON notes "
        "FOR EACH ROW EXECUTE FUNCTION entity_revision_cascade('note')"
    )

    # RLS: standard tenant predicate. FORCE so even the table owner
    # cannot bypass --- matches activity_log/agent_runs (the other
    # cross-tenant-sensitive logs).
    op.execute("ALTER TABLE entity_revision ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entity_revision FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_entity_revision ON entity_revision "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity_revision TO flow_app")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_note_revision_cascade ON notes")
    op.execute("DROP TRIGGER IF EXISTS trg_task_revision_cascade ON tasks")
    op.execute("DROP FUNCTION IF EXISTS entity_revision_cascade()")
    op.execute("DROP TRIGGER IF EXISTS trg_entity_revision_no_update_sealed ON entity_revision")
    op.execute("DROP FUNCTION IF EXISTS entity_revision_no_update_sealed()")
    op.execute("DROP POLICY IF EXISTS p_entity_revision ON entity_revision")
    op.execute("DROP INDEX IF EXISTS ix_entity_revision_org_id")
    op.execute("DROP INDEX IF EXISTS uq_entity_revision_open")
    op.execute("DROP INDEX IF EXISTS ix_entity_revision_entity_timeline")
    op.drop_table("entity_revision")
