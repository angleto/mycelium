"""A person's pinned notes.

The notes someone keeps at hand, listed at the top of /notes above the
recent ones, the same way ``task_pins`` (0027) does it for tasks. A pin
belongs to the person and not to the note: two people pin different things
in the same workspace, and pinning changes nothing about the note itself. No
``updated_at`` bump, which would reorder the Recent list and move the
``updated_since`` sync clock; no revision; and no activity row, because the
garden reads a note's liveness from the activity log (``note_touch``) and a
pin is keeping a note in sight, not working on it: a pinned note must still
age like any other.

``note_pins``  one row per (person, note). Workspace RLS on ``org_id``; the
               note and the person cascade, so a purged note or a removed
               account leaves no pin behind.

A note that goes back to review (``review_state`` becomes ``'proposed'``,
today when an atom is withdrawn to bring its sources back up) leaves the
read path: it is listed nowhere, the pinned list included, so its pins could
be neither seen nor cleared, and would hold a place under the per-person cap
until the purge. A trigger drops them in the same statement, so every path
that demotes a note, present or future, does it. A note in the bin keeps its
pins: restoring it brings it back to the pinned list.

The index the notes list needs to read that clock note by note is not here:
it is built concurrently, outside any transaction, in 0034.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # The three foreign keys take a SHARE ROW EXCLUSIVE lock on users, notes
    # and organizations. Behind an open transaction on one of them the
    # migration would wait, and every write to that table would queue behind
    # it (measured on 0026: 23s behind one open UPDATE on users). Ten
    # seconds, then fail; a failed migration is retried, a stalled table is
    # an outage.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "note_pins",
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column(
            "pinned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "note_id", name="pk_note_pins"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_note_pins_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name="fk_note_pins_note_id_notes", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_note_pins_org_id_organizations",
            ondelete="CASCADE",
        ),
    )
    # The read is always "this person's pins in this workspace, newest
    # first"; the primary key leads with the person, the index adds the
    # workspace and the order. The note index serves the cascade on purge.
    op.create_index(
        "ix_note_pins_org_user_pinned_at", "note_pins", ["org_id", "user_id", "pinned_at"]
    )
    op.create_index("ix_note_pins_note_id", "note_pins", ["note_id"])
    op.execute("ALTER TABLE note_pins ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_pins FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_note_pins ON note_pins USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "note_pins")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_pins TO mycelium_app")
    # SECURITY INVOKER on purpose: it runs as the session that demoted the
    # note, under that session's workspace, which is the note's workspace.
    op.execute(
        """
        CREATE FUNCTION note_pins_drop_on_review() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM note_pins WHERE note_id = NEW.id;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_note_pins_drop_on_review
        AFTER UPDATE OF review_state ON notes
        FOR EACH ROW
        WHEN (NEW.review_state = 'proposed' AND OLD.review_state IS DISTINCT FROM 'proposed')
        EXECUTE FUNCTION note_pins_drop_on_review()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_note_pins_drop_on_review ON notes")
    op.execute("DROP FUNCTION IF EXISTS note_pins_drop_on_review()")
    op.execute("REVOKE ALL ON TABLE note_pins FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON note_pins")
    op.execute("DROP POLICY IF EXISTS p_note_pins ON note_pins")
    op.drop_index("ix_note_pins_note_id", table_name="note_pins")
    op.drop_index("ix_note_pins_org_user_pinned_at", table_name="note_pins")
    op.drop_table("note_pins")
