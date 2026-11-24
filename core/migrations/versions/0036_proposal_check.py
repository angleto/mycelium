"""An agent checks and revises a proposal; the person decides (ADR-0076).

``proposal_check``  one row per check of a generated note: who checked it
                    (identity, and the model it used), the version of the
                    note it read, a verdict, a summary, and one finding per
                    claim. A check decides nothing: it is evidence shown to
                    the person beside the proposal. Workspace RLS on
                    ``org_id``; the note and the workspace cascade, so a
                    proposal removed for good takes its checks with it. The
                    checker is SET NULL, so removing an identity keeps the
                    check and loses only its attribution.

``notes.revises_note_id``  on a revision, the proposal it replaces. SET
                    NULL, so removing the original for good leaves the
                    revision standing. Not a ``supersedes`` link: that kind
                    of link drives the maturity decay of its target
                    (ADR-0040), and a revision is a statement about
                    provenance, not about the graph.

``lock_timeout`` is the repo's ten seconds, for the same reason as 0035:
adding a column and a foreign key to ``notes`` takes a brief lock that must
fail and be retried rather than queue every write behind it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "notes",
        sa.Column("revises_note_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_notes_revises_note_id_notes",
        "notes",
        "notes",
        ["revises_note_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "proposal_check",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_version", sa.Integer(), nullable=False),
        sa.Column("checker_identity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("checker_model_id", sa.String(128), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("findings", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_check"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_proposal_check_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name="fk_proposal_check_note_id_notes", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["checker_identity_id"],
            ["identities.id"],
            name="fk_proposal_check_checker_identity_id_identities",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("verdict IN ('supported', 'issues')", name="ck_proposal_check_verdict"),
    )
    op.create_index("ix_proposal_check_note_created", "proposal_check", ["note_id", "created_at"])
    op.create_index("ix_proposal_check_org_id", "proposal_check", ["org_id"])
    op.execute("ALTER TABLE proposal_check ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE proposal_check FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_proposal_check ON proposal_check USING ({_ORG_PRED}) "
        f"WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "proposal_check")
    op.execute("GRANT SELECT, INSERT, DELETE ON TABLE proposal_check TO mycelium_app")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("REVOKE ALL ON TABLE proposal_check FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON proposal_check")
    op.execute("DROP POLICY IF EXISTS p_proposal_check ON proposal_check")
    op.drop_table("proposal_check")
    op.drop_constraint("fk_notes_revises_note_id_notes", "notes", type_="foreignkey")
    op.drop_column("notes", "revises_note_id")
